"""GUI orchestration of existing task stages, advancing only after thread exit."""
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QSpinBox, QPushButton, QTableWidget, QTableWidgetItem, QHeaderView

import scheduler_core
from ..services import msgbox
from ..services.project_paths import PROJECT_PATHS

STAGES = ("负向申诉", "发起复议", "售后申诉")


class PipelineMixin:
    def _build_pipeline_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("依次执行所选任务，各阶段独立；某阶段异常仍继续后续阶段。双击单元格查看完整错误。"))
        self._pipeline_options = []
        row = QHBoxLayout()
        for name in STAGES:
            check = QCheckBox(name)
            check.setChecked(True)
            limit = QSpinBox()
            limit.setRange(1, 10000)
            limit.setValue(20)
            limit.setSuffix(" 单/店")
            row.addWidget(check)
            row.addWidget(limit)
            self._pipeline_options.append((check, limit))
        layout.addLayout(row)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("浏览器并发数"))
        self._pipeline_pool = QSpinBox()
        self._pipeline_pool.setRange(1, 20)
        self._pipeline_pool.setValue(4)
        controls.addWidget(self._pipeline_pool)
        self._pipeline_start = QPushButton("启动流水线")
        self._pipeline_stop = QPushButton("停止流水线")
        self._pipeline_stop.setEnabled(False)
        self._pipeline_start.clicked.connect(self._start_pipeline)
        self._pipeline_stop.clicked.connect(self._stop_pipeline)
        controls.addStretch()
        controls.addWidget(self._pipeline_start)
        controls.addWidget(self._pipeline_stop)
        layout.addLayout(controls)
        self._pipeline_label = QLabel("尚未启动")
        layout.addWidget(self._pipeline_label)
        self._pipeline_table = QTableWidget(0, 4)
        self._pipeline_table.setHorizontalHeaderLabels(["店铺", *STAGES])
        self._pipeline_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._pipeline_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._pipeline_table.setWordWrap(True)
        self._pipeline_table.setTextElideMode(Qt.ElideNone)
        self._pipeline_table.cellDoubleClicked.connect(self._show_pipeline_details)
        self._pipeline_table.setMinimumHeight(130)
        layout.addWidget(self._pipeline_table)
        return tab

    def _start_pipeline(self):
        if self.is_running() or scheduler_core.active_operation_lock():
            msgbox.warning(self, "存在运行冲突", "请等待当前任务结束后启动流水线。")
            return
        shops = self._checked_shops()
        stages = [i for i, (check, _) in enumerate(self._pipeline_options) if check.isChecked()]
        if not shops or not stages:
            msgbox.warning(self, "请选择任务", "请至少选择一家店铺和一个阶段。")
            return
        problems = PROJECT_PATHS.validate()
        if problems:
            msgbox.critical(self, "环境配置有误", "\n".join(problems))
            return
        limits = [limit.value() for _, limit in self._pipeline_options]
        pool = self._pipeline_pool.value()
        details = " → ".join(f"{STAGES[i]}（每店最多{limits[i]}单）" for i in stages)
        if not msgbox.confirm_yes_no(self, "确认启动流水线",
            f"对 {len(shops)} 家店铺依次执行：\n{details}\n浏览器并发上限 {pool} 家，真实提交。\n"
            "连续三单失败或店铺异常时只结束该店铺当前阶段，后续勾选阶段仍独立执行。复议沿用现有材料复用规则。"):
            return
        self._pipeline_shops = list(shops)
        self._pipeline_stages = stages
        self._pipeline_limits = limits
        self._pipeline_pool_value = pool
        self._pipeline_index = -1
        self._pipeline_failures = {}
        self._pipeline_order_errors = {}
        self._pipeline_advance_pending = False
        self._pipeline_counts = {}
        self._pipeline_active = True
        self._pipeline_cancelled = False
        self._pipeline_table.setRowCount(len(shops))
        for row, shop in enumerate(shops):
            self._pipeline_table.setItem(row, 0, QTableWidgetItem(shop))
            for stage in range(3):
                self._pipeline_table.setItem(row, stage + 1, QTableWidgetItem("等待" if stage in stages else "未选择"))
        self._pipeline_start.setEnabled(False)
        self._pipeline_stop.setEnabled(True)
        for check, limit in self._pipeline_options:
            check.setEnabled(False)
            limit.setEnabled(False)
        self._pipeline_pool.setEnabled(False)
        for i in range(self._task_tabs.count()):
            self._task_tabs.setTabEnabled(i, i == self._pipeline_tab_index)
        self._pipeline_next()

    def _pipeline_cell(self, shop, stage):
        return self._pipeline_table.item(self._pipeline_shops.index(shop), stage + 1)

    def _pipeline_next(self):
        if not self._pipeline_active:
            return
        self._pipeline_advance_pending = False
        self._pipeline_index += 1
        shops = list(self._pipeline_shops)
        if self._pipeline_cancelled or self._pipeline_index >= len(self._pipeline_stages) or not shops:
            self._finish_pipeline()
            return
        stage = self._pipeline_stages[self._pipeline_index]
        self._pipeline_current_stage = stage
        self._pipeline_stage_shops = shops
        self._pipeline_completed = set()
        self._pipeline_phase_complete = False
        self._pipeline_thread = None
        self._pipeline_label.setText(f"当前阶段：{STAGES[stage]}（{self._pipeline_index + 1}/{len(self._pipeline_stages)}），{len(shops)} 家店铺")
        self._append_log(f"[流水线] 开始阶段：{STAGES[stage]}，{len(shops)} 家店铺")
        for shop in shops:
            self._pipeline_cell(shop, stage).setText("处理中")
        try:
            pool = min(self._pipeline_pool_value, len(shops))
            starters = [self._start_run, self._start_reconsider, self._start_aftersale]
            starters[stage](shops, pool, self._pipeline_limits[stage])
            worker = [self._worker, self._reconsider_worker, self._aftersale_worker][stage]
            if worker is None:
                raise RuntimeError("阶段启动失败，请查看运行日志")
        except Exception as exc:
            self._append_log(f"[流水线] 启动失败：{exc}")
            for shop in shops:
                self._pipeline_shop_result(shop, False, f"启动失败：{exc}")
            thread = self._pipeline_thread
            if thread is not None and thread.isRunning():
                # 启动后报错也先等待当前线程退出，再让下一阶段使用浏览器目录。
                stoppers = [self._request_stop, self._request_reconsider_stop, self._request_aftersale_stop]
                stoppers[stage](reason="当前阶段启动异常")
            else:
                self._pipeline_schedule_next()

    def _pipeline_schedule_next(self):
        if not self._pipeline_advance_pending:
            self._pipeline_advance_pending = True
            QTimer.singleShot(0, self._pipeline_next)

    def _pipeline_watch(self, thread):
        if self._pipeline_active:
            self._pipeline_thread = thread
            stage = self._pipeline_current_stage
            thread.finished.connect(lambda: self._pipeline_thread_finished(stage))

    def _pipeline_thread_finished(self, stage=None):
        if not self._pipeline_active or (stage is not None and stage != self._pipeline_current_stage):
            return
        # Existing handlers have now released the operation lock and worker references.
        for shop in self._pipeline_stage_shops:
            if shop not in self._pipeline_completed:
                if self._pipeline_cancelled:
                    cell = self._pipeline_cell(shop, self._pipeline_current_stage)
                    cell.setText("已停止：用户取消，当前阶段未完成")
                    cell.setToolTip(cell.text())
                else:
                    reason = (
                        "阶段已结束，但未返回该店铺的完成结果。"
                        if self._pipeline_phase_complete else
                        "工作线程异常结束，未收到阶段完成通知。"
                    )
                    self._pipeline_shop_result(shop, False, reason)
        self._pipeline_schedule_next()

    def _show_pipeline_details(self, row, column):
        cell = self._pipeline_table.item(row, column)
        if cell is not None and column > 0:
            shop = self._pipeline_table.item(row, 0).text()
            msgbox.information(self, f"{shop} · {STAGES[column - 1]}", cell.toolTip() or cell.text())

    def _pipeline_order(self, shop, result, order_no=""):
        if not self._pipeline_active or shop not in self._pipeline_stage_shops or shop in self._pipeline_completed:
            return
        stage = self._pipeline_current_stage
        key = (shop, stage)
        counts = self._pipeline_counts.setdefault(key, [0, 0, 0])
        counts[0] += 1
        if str(result).startswith(("失败", "分析失败")):
            counts[2] += 1
            errors = self._pipeline_order_errors.setdefault(key, [])
            errors.append(f"订单 {order_no}：{result}" if order_no else str(result))
            del errors[:-3]
        elif str(result).startswith("已提交"):
            counts[1] += 1
        text = f"处理{counts[0]}，提交{counts[1]}，失败{counts[2]}"
        errors = self._pipeline_order_errors.get(key, [])
        if errors:
            text += "\n最近失败：\n" + "\n".join(errors)
        cell = self._pipeline_cell(shop, stage)
        cell.setText(text)
        cell.setToolTip(text)
        self._pipeline_table.resizeRowToContents(self._pipeline_shops.index(shop))

    def _pipeline_shop_result(self, shop, success, message):
        if not self._pipeline_active or shop not in self._pipeline_stage_shops or shop in self._pipeline_completed:
            return
        stage = self._pipeline_current_stage
        key = (shop, stage)
        self._pipeline_completed.add(shop)
        counts = self._pipeline_counts.get(key, [0, 0, 0])
        message = str(message or "").strip()
        state = "已停止" if self._pipeline_cancelled else ("完成" if success else "异常")
        if success and "今日已完成" in message:
            state = "今日已完成，跳过"
        text = f"{state}：处理{counts[0]}，提交{counts[1]}，失败{counts[2]}"
        if not success:
            message = message or "本阶段执行失败，未返回错误详情，请查看本阶段运行日志。"
            self._pipeline_failures[key] = message
        if message:
            text += f"\n{'说明' if success else '错误'}：{message}"
        errors = self._pipeline_order_errors.get(key, [])
        if errors:
            text += "\n最近失败：\n" + "\n".join(errors)
        cell = self._pipeline_cell(shop, stage)
        cell.setText(text)
        cell.setToolTip(text)
        self._pipeline_table.resizeRowToContents(self._pipeline_shops.index(shop))
        self._append_log(f"[流水线][{STAGES[stage]}][{shop}] {text}")

    def _on_scheduler_error(self, message):
        reason = f"负向申诉调度线程异常：{message}"
        self._append_log(reason)
        if self._pipeline_active and self._pipeline_current_stage == 0:
            for shop in self._pipeline_stage_shops:
                self._pipeline_shop_result(shop, False, reason)

    def _pipeline_event(self, event):
        if not self._pipeline_active:
            return
        if event.type == "lock_conflict":
            reason = event.line or "运行锁冲突：其它任务正在使用店铺浏览器。"
            for shop in self._pipeline_stage_shops:
                self._pipeline_shop_result(shop, False, reason)
        elif event.type == "round_skip_done":
            for shop in event.data.get("shops", []):
                self._pipeline_shop_result(shop, True, "今日已完成，跳过本阶段")
        elif event.shop:
            if event.type in {"order_result", "order_error"}:
                result = event.data.get("result_text", "")
                if event.type == "order_error":
                    result = "失败：" + str(event.data.get("message") or event.line or "未返回订单错误详情")
                self._pipeline_order(event.shop, result, event.data.get("order_no", ""))
            elif event.type in {"worker_done", "worker_empty_queue"}:
                self._pipeline_shop_result(event.shop, True, event.line)
            elif event.type in {"worker_init_failed", "worker_comm_lost", "worker_too_many_failures"}:
                self._pipeline_shop_result(event.shop, False, event.line)

    def _stop_pipeline(self):
        self._pipeline_cancelled = True
        self._pipeline_stop.setEnabled(False)
        self._pipeline_label.setText("正在安全停止，后续阶段已取消")
        self._request_stop(reason="停止流水线")
        self._request_reconsider_stop(reason="停止流水线")
        self._request_aftersale_stop(reason="停止流水线")

    def _finish_pipeline(self):
        self._pipeline_active = False
        for row in range(self._pipeline_table.rowCount()):
            for col in range(1, 4):
                cell = self._pipeline_table.item(row, col)
                if cell.text() == "等待":
                    cell.setText("已取消" if self._pipeline_cancelled else "已跳过")
        self._pipeline_start.setEnabled(True)
        self._pipeline_stop.setEnabled(False)
        for check, limit in self._pipeline_options:
            check.setEnabled(True)
            limit.setEnabled(True)
        self._pipeline_pool.setEnabled(True)
        for i in range(self._task_tabs.count()):
            self._task_tabs.setTabEnabled(i, True)
        title = "流水线已停止" if self._pipeline_cancelled else "流水线已结束"
        totals = []
        for stage in self._pipeline_stages:
            counts = [values for (shop, task), values in self._pipeline_counts.items() if task == stage]
            totals.append(f"{STAGES[stage]}：处理{sum(v[0] for v in counts)}，提交{sum(v[1] for v in counts)}，失败{sum(v[2] for v in counts)}")
        if self._pipeline_failures:
            totals.append("阶段异常（其它勾选阶段独立执行）：")
            for (shop, stage), reason in self._pipeline_failures.items():
                totals.append(f"{shop} · {STAGES[stage]}：{reason}")
        else:
            totals.append("阶段异常：无")
        self._pipeline_label.setText(title)
        summary = "\n".join(totals)
        self._append_log(f"[流水线] {title}\n{summary}")
        msgbox.information(self, title, summary)
