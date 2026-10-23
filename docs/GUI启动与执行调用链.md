# GUI 启动与执行调用链

本文按当前代码的真实执行顺序，说明从启动 GUI，到点击“运行控制 → 启动”，再到每个店铺采集订单、分析聊天、填写并真实提交申诉、结果回传和收尾的完整调用链。

本文只描述 GUI 主流程。命令行直接运行 `main.py` 的分阶段模式不属于本文主线，但 GUI 为登录和查询申诉结果启动的两个辅助子流程也在文末说明。

## 1. 先理解三个执行层

GUI 运行时不是一个 Python 进程完成所有工作，而是分成三层：

```text
GUI 主进程（PyQt5 / Python 3.11）
├─ Qt 主线程：窗口、按钮、状态表格、日志显示
├─ SchedulerWorker 所在 QThread：运行 scheduler_core.Scheduler
└─ 每个店铺一个主项目子进程（Python 3.12）
   └─ main.py --shop <店铺> --serve --confirm
      └─ Playwright 浏览器 + 采集 + DeepSeek 分析 + 申诉提交
```

需要特别区分两个概念：

- “并发上限 4”表示最多同时保留 4 家店铺的浏览器和常驻子进程。
- 真正的订单操作仍由调度器一次只点名一家店铺执行一个 `next`。因此可以同时开着多个浏览器，但不会同时让多家店铺执行订单页面操作。

GUI 进程不直接导入 Playwright、OpenAI 等主项目依赖。涉及浏览器的操作全部通过主项目 Python 解释器启动 `main.py` 子进程完成。

## 2. GUI 程序如何启动

### 2.1 启动命令

开发环境的入口是：

```text
gui/README.md
  → uv run python -m app.main
```

安装版桌面快捷方式的入口是：

```text
packaging/pddchat.iss
  → runtime/gui/pythonw.exe -m app.main
```

两种方式最终都会进入同一个函数：

```text
gui/app/main.py
  → main()
```

### 2.2 `gui/app/main.py` 的执行顺序

`app.main` 被导入时，首先导入：

```text
gui/app/services/project_paths.py
  → _load_raw_config()
  → _resolve()
  → ProjectPaths.__init__()
```

这里读取 `gui/config.json`，得到：

- `pddchat_root`：主项目根目录；
- `pddchat_python`：运行主项目的 Python 3.12 解释器；
- `main_py`：主项目的 `main.py`。

随后把主项目根目录加入 `sys.path`，让 GUI 可以导入纯标准库实现的 `db.py` 和 `scheduler_core.py`。GUI 不会因此直接运行 `main.py` 或 Playwright。

然后进入 `gui/app/main.py → main()`：

1. `setup_logging()` 初始化 GUI 文件日志。
2. `QApplication(sys.argv)` 创建 Qt 应用。
3. `PROJECT_PATHS.validate()` 检查主项目目录、`main.py` 和主项目解释器是否存在。
4. `single_instance.try_acquire()` 获取 `state/gui_app.lock`，避免同时打开两个 GUI。
5. 创建 `MainWindow()`。
6. `window.show()` 显示窗口。
7. `app.exec_()` 进入 Qt 事件循环。
8. GUI 退出时在 `finally` 中执行 `single_instance.release()`。

### 2.3 主窗口和四个页面

调用链如下：

```text
gui/app/main.py
  → MainWindow()
     文件：gui/app/main_window.py
     → DashboardPage()
     → ShopsPage()
     → RunPage()
     → ReportsPage()
```

`MainWindow.__init__()` 会一次性创建“总览仪表盘、店铺管理、运行控制、报表/导出”四页，再放入 `QStackedWidget`。切换左侧导航只是切换已经创建好的页面，不会重新创建页面。

各页面需要的数据库数据主要通过下面的后台轮询链路刷新：

```text
DashboardPage / ShopsPage / RunPage / ReportsPage
  → DbPoller.start()
     文件：gui/app/workers/db_poll_worker.py
  → DbPoller._tick()
  → _PollTask.run()                 # 在线程池中查询 SQLite
  → data_ready 信号                 # 回到 GUI 线程更新控件
```

默认每 4 秒刷新一次，周期性 SQLite 查询不放在 Qt 主线程中，避免数据库正在被店铺子进程写入时卡住界面。`ReportsPage.__init__()` 为了首次显示会先通过 `_reload()` 同步查询一次，之后才由 `DbPoller` 后台刷新；用户主动切换筛选条件或点击刷新时也会同步调用 `_reload()`。

## 3. 手动点击“启动”后的 GUI 层流程

入口位于：

```text
gui/app/pages/run_page.py
  → RunPage._on_start_clicked()
```

执行顺序如下：

1. `RunPage._checked_shops()` 读取当前勾选的店铺。
2. 检查是否正在执行“查询申诉结果”。
3. 如果启用了每日自动运行，检查当前时间是否在允许窗口内。
4. 再次执行 `PROJECT_PATHS.validate()`。
5. 读取界面上的浏览器并发上限。
6. 调用 `confirm_real_submit()` 弹出真实提交二次确认框。
7. 用户确认后调用 `RunPage._start_run(shops, pool_size)`。

GUI 的“启动”固定使用以下参数，运营界面不能临时更改：

| 参数 | GUI 固定行为 |
|---|---|
| 提交方式 | 真实提交，传入 `--confirm` |
| AI 分析 | 不传 `--reanalyze`，已有完整分析结果则复用 |
| 验证码 | 不传 `--manual-captcha`，优先自动处理 |
| 调度轮次 | `run_forever=False`，正常情况下只跑一轮 |
| 连续失败阈值 | 默认 3 单 |
| 子进程响应超时 | 默认 300 秒 |

`RunPage._start_run()` 接着执行：

```text
RunPage._start_run()
  → 创建 QThread
  → 创建 SchedulerWorker
     文件：gui/app/workers/scheduler_thread.py
  → SchedulerWorker.moveToThread(QThread)
  → QThread.started 连接 SchedulerWorker.start
  → SchedulerWorker.event_received 连接 RunPage._on_event
  → SchedulerWorker.stopped 连接 RunPage._on_stopped
  → QThread.start()
```

从这里开始，耗时调度工作在独立 `QThread` 中执行，Qt 主线程只处理界面和信号。

## 4. SchedulerWorker 如何进入调度器

调用链：

```text
gui/app/workers/scheduler_thread.py
  → SchedulerWorker.start()
  → scheduler_core.Scheduler(...)
  → Scheduler.run_once()
```

因为 GUI 传入 `run_forever=False`，所以调用的是 `Scheduler.run_once()`，不是 `run_forever()`。

继续进入：

```text
scheduler_core.py
  → Scheduler.run_once()
  → Scheduler._run(loop_forever=False)
  → Scheduler._acquire_lock()
  → Scheduler._run_loop(loop_forever=False)
```

`Scheduler._acquire_lock()` 使用 `state/scheduler.lock` 防止 GUI 调度器和命令行调度器同时运行。这个锁与防止重复打开 GUI 的 `state/gui_app.lock` 是两把不同的锁。

## 5. 一轮调度如何选择和启动店铺

`Scheduler._run_loop()` 的店铺准备顺序如下：

1. 调用 `db.get_shop_daily_status(shop)`。
2. 当天状态已经是 `done` 的店铺直接跳过，不打开浏览器。
3. 其余店铺进入 `pending`。
4. `Scheduler._order_pending_by_priority()` 先按“连续多少天没获得并发名额”排序，同优先级内随机。
5. `Scheduler._fill_pool()` 依次启动店铺，直到达到并发上限。

注意：`_fill_pool()` 虽然是在填充并发池，但启动和初始化店铺是严格逐家执行的，不会同时对多家店铺发送 `init`。

每家店铺通过下面的调用链启动：

```text
scheduler_core.py
  → Scheduler._fill_pool()
  → Scheduler._start_worker(shop)
  → ShopWorker.__init__()
  → subprocess.Popen(...)
```

GUI 主流程实际启动的命令等价于：

```text
<主项目 Python 3.12> main.py --shop <店铺名> --serve --confirm
```

`ShopWorker.__init__()` 同时启动两个后台读取线程：

- `ShopWorker._read_stdout()`：读取子进程 stdout 中的 JSON Lines 协议消息；
- `ShopWorker._read_stderr()`：持续排空并保存普通运行日志和异常诊断。

协议通道和普通日志被刻意分开：

| 通道 | 内容 |
|---|---|
| stdin | 调度器发给店铺子进程的 `init / next / stop` |
| stdout | 店铺子进程返回的 `ready / result / queue_empty / error` JSON |
| stderr | 采集、分析、填表等人类可读日志和 traceback |

## 6. 店铺子进程启动后先执行什么

子进程从主项目 `main.py` 开始。

### 6.1 店铺环境和路径初始化

`main.py` 顶层最先执行：

```text
main.py
  → _bootstrap_shop_env()
  → 把 --shop 后的店铺名写入 PDDCHAT_SHOP
  → 导入 paths.py
  → paths.py 根据店铺名确定 PROFILE_DIR、DEBUG_DIR、OUTPUT_DIR 等目录
```

这一步必须发生在导入浏览器和业务模块之前，因为各店铺的浏览器用户目录和证据目录需要先固定下来。

随后进入：

```text
main.py
  → main()
  → parse_args()
  → args.serve 分支
```

serve 分支会：

1. 把 stdin、stdout、stderr 编码固定为 UTF-8。
2. 保留原始 stdout 为 `real_stdout`，专门发送 JSON 协议。
3. 把普通 `print()` 重定向到 stderr。
4. 调用 `set_auto_solve_captcha(True)`。
5. 进入 `sync_playwright()`。
6. 调用 `run_serve_mode(playwright, real_stdout, confirm=True, reanalyze=False)`。

此时子进程只是等待命令，还没有立即打开浏览器。

## 7. `init`：打开浏览器、检查登录、采集订单

调度器完成 `ShopWorker` 创建后执行：

```text
scheduler_core.py
  → Scheduler._start_worker()
  → ShopWorker.send({"cmd": "init"})
  → ShopWorker.wait_response()
```

店铺子进程在 `main.py → run_serve_mode()` 中收到 `init` 后，按以下顺序执行。

### 7.1 创建持久化浏览器

```text
main.py → run_serve_mode() 的 init 分支
  → stealth.py → create_stealth_context()
  → playwright.chromium.launch_persistent_context()
```

使用该店铺自己的 `PROFILE_DIR`，登录 Cookie、localStorage 等都保存在这个店铺目录中。

### 7.2 检查登录状态

```text
main.py → run_serve_mode()
  → login.py → wait_for_login(interactive=False)
  → login.py → open_chat_page()
  → login.py → is_chat_page_ready()
```

serve 模式不会等待人工在终端按回车。如果检测不到有效登录态，`wait_for_login()` 直接抛异常；`run_serve_mode()` 把它转换成：

```json
{"type":"error","stage":"init","message":"..."}
```

调度器收到后关闭该店铺并调用 `db.mark_shop_daily_failed()`，再启动排队中的下一家店铺补位。

登录成功后还会调用 `set_give_up_after_auto_solve_fails(True)`：后续无人值守操作遇到验证码时先自动处理，连续自动处理失败后放弃当前任务，不无限等待人工。

### 7.3 Phase 1：采集本轮订单队列

调用链：

```text
main.py
  → run_serve_mode() 的 init 分支
  → run_phase1(page)
     → run_appeal_batch.py → open_appeal_list_page()
     → run_appeal_batch.py → open_appealable_orders_table()
     → run_appeal_batch.py → collect_all_appealable_order_numbers()
        → extract_order_info_from_current_page()
        → 必要时 click_next_page()
        → 必要时 wait_for_orders_table_updated()
     → run_appeal_batch.py → record_collected_orders()
        → db.py → record_collected()
```

采集结果包括订单号、问题类型、商品信息、补偿金额和创建时间。新订单写为 `pending`；已存在订单只刷新本次采到的列表字段，不把已经分析或提交过的状态退回 `pending`。

未配置店铺处理上限时，默认只采集第一页。配置了 `max_orders` 时，`run_phase1()` 会放宽翻页安全上限，并按订单数量精确截断。

采集完成后，子进程发送：

```json
{"type":"ready","queue_size":20}
```

调度器收到 `ready` 后，把 `queue_size` 保存到 `ShopWorker.remaining`。如果为 0，则关闭浏览器并把该店铺当天标记为 `done`；大于 0 才加入 `Scheduler.active`。

## 8. `next`：调度器按订单随机轮换店铺

当并发池中存在可用店铺时，`Scheduler._run_loop()` 执行：

```text
scheduler_core.py
  → 从 active 中随机选择一家店铺
     （尽量不连续两次选择同一家）
  → Scheduler._emit("order_turn", ...)
  → ShopWorker.send({"cmd": "next"})
  → ShopWorker.wait_response()
```

这就是 GUI 日志中下面这类信息的来源：

```text
[调度器] 轮到店铺 [...] 处理下一单（剩余约 N 单，当前并发 X/Y）
```

店铺子进程的 `main.py → run_serve_mode()` 收到 `next` 后：

1. 从内存中的 `order_numbers[next_index]` 取出下一订单号。
2. 立即把 `next_index` 加一。
3. 从第二单开始增加 1～3 秒随机延迟。
4. 调用 `process_and_submit_one_order()`。

## 9. 单个订单：先分析，再提交

核心入口：

```text
main.py
  → process_and_submit_one_order()
```

它把一个订单分成两个连续阶段：

```text
是否需要分析
  → 需要：抓聊天 + AI 分类 + AI 生成文案 + 写数据库
  → 不需要：复用数据库已有分析
然后
  → 搜索该订单的全部申诉记录 + 逐条填表并提交
```

### 9.1 判断是否重新分析

调用链：

```text
main.py → process_and_submit_one_order()
  → main.py → _order_has_analysis(order_no)
  → db.py → has_analysis(shop, order_no)
```

GUI 不传 `--reanalyze`。只要数据库中同时存在非空的 `classification` 和 `appeal_text`，就跳过聊天抓取和 DeepSeek 调用，直接进入提交阶段。

如果没有完整分析结果，则调用：

```text
main.py
  → run_appeal_batch.py → process_single_order()
```

### 9.2 抓取聊天记录

`process_single_order()` 的主要调用链：

```text
run_appeal_batch.py → process_single_order()
  → login.py → open_chat_page()
  → pdd_chat.py → query_chat_records()
     → select_order_search_mode()
     → fill_order_number()
     → get_query_button()
     → start_result_observer()
     → blockers.py → safe_click()
     → blockers.py → clear_blockers()
     → wait_for_query_result()
     → collect_all_chat_records()
        → is_chat_records_empty()
        → save_no_chat_record_screenshot()，或
        → save_page_evidence_screenshot()
        → extract_current_page_records()
        → 必要时 click_next_page() / wait_for_next_page_loaded()
  → pdd_chat.py → build_chat_record_dict()
```

聊天存在时，每页先保存凭证截图，再提取消息。所有消息及截图路径最后组成内存字典，不再另外写 `chat_<订单号>.json`。

### 9.3 无聊天记录分支

如果 `query_chat_records()` 返回空记录：

```text
run_appeal_batch.py → process_single_order()
  → 使用 NO_CHAT_RECORD_LABEL
  → 使用 NO_CHAT_RECORD_APPEAL_TEXT
  → db.py → record_analyzed()
```

这个分支不调用 DeepSeek，但仍然会把固定分类、固定文案、消息数和“暂无聊天记录”截图写入数据库。

### 9.4 有聊天记录的 DeepSeek 分析分支

调用链：

```text
run_appeal_batch.py → process_single_order()
  → appeal_classifier.py → compact_chat_json()
  → appeal_classifier.py → classify_appeal_evidence()
     → get_client()
     → DeepSeek chat.completions.create()      # 第一次：选申诉凭证类型
  → appeal_classifier.py → generate_appeal_text()
     → get_client()
     → DeepSeek chat.completions.create()      # 第二次：生成申诉文案
  → db.py → record_analyzed()
```

`get_client()` 从项目根目录 `.env` 读取 `DEEPSEEK_API_KEY`，连接 `https://api.deepseek.com`，使用 `deepseek-v4-flash`。

分析成功后，订单状态写为 `analyzed`，并保存分类、申诉文案、完整消息、消息数量和凭证截图路径。

### 9.5 分析失败分支

`process_single_order()` 本身让异常继续向上抛，由 `main.py → process_and_submit_one_order()` 捕获：

```text
main.py → process_and_submit_one_order()
  → screenshots.py → save_error_screenshot()
  → db.py → record_analysis_failed()
  → 返回 "分析失败: <异常>"
```

分析失败后不会调用 `submit_single_order()`，因此该订单不会进入填表和提交阶段。

## 10. 提交一个订单号下的全部申诉记录

分析成功或成功复用旧分析后，进入：

```text
main.py
  → submit_single_order()
```

一个订单号可能对应多条独立申诉记录，所以这里不是“找到第一条就结束”，而是搜索一次后逐条处理。

### 10.1 搜索订单记录

```text
main.py → submit_single_order()
  → appeal_submit.py → open_appeal_search_page()
  → appeal_submit.py → search_and_get_rows()
  → appeal_submit.py → get_result_rows()
```

没有搜索结果时：

```text
db.py → record_submit_result(..., "未找到申诉记录")
```

有结果时，循环读取每一行：

```text
appeal_submit.py → get_row_record_key()
appeal_submit.py → get_row_issue_type()
main.py → _process_one_appeal_record()
```

`record_key` 用来区分同一订单号下的多条记录。

### 10.2 打开详情并判断状态

每条记录的调用链：

```text
main.py → _process_one_appeal_record()
  → appeal_submit.py → get_row_detail_link()
  → appeal_submit.py → open_row_detail_page()
  → appeal_submit.py → detect_detail_page_status()
```

如果检测到已经申诉、申诉超时或已经驳回等终态，则不再填表，直接：

```text
db.py → record_appeal_result()
```

否则读取分析结果：

```text
appeal_submit.py → load_order_classification()
  → db.py → get_order()
```

### 10.3 填写申诉表单

调用入口：

```text
main.py → _process_one_appeal_record()
  → appeal_submit.py → fill_appeal_modal()
```

`fill_appeal_modal()` 内部按页面实际形态分流：

- 服务问题：`fill_appeal_form_service()`；
- 商品问题状态 A：`fill_appeal_form_status_a()`；
- 商品问题 A2 变体：`fill_appeal_form_status_a2()`；
- 商品问题 A3 变体：`fill_appeal_form_status_a3()`；
- 商品问题状态 B：`fill_appeal_form_status_b()`；
- 商品问题状态 C 独立页面：`fill_appeal_form_status_c()`。

填表前会调用 `collect_evidence_files(order_no)` 收集聊天凭证图片。填表结束后会保存 `debug/appeal_form_filled_<订单号>.png`。

### 10.4 真实提交

因为 GUI 总是传 `--confirm`，所以 `_process_one_appeal_record()` 最终调用：

```text
appeal_submit.py → click_appeal_form_submit()
```

- A/B/服务问题模态框在详情页中提交；
- C 状态在新打开的独立表单页提交；
- 提交后如果出现验证码，进入 `_wait_for_submit_captcha()`，再交给验证码模块处理。

提交成功后：

```text
db.py → record_appeal_result(..., "已提交")
```

当一个订单号下的所有记录都处理完后：

```text
main.py → _summarize_results()
  → db.py → record_submit_result()
```

因此数据库有两个粒度：

| 表 | 粒度 | 主要用途 |
|---|---|---|
| `orders` | 店铺 + 订单号 | 采集、分析和订单总体状态 |
| `appeal_records` | 店铺 + 订单号 + record_key | 同一订单号下每条申诉记录的独立结果 |

## 11. 订单结果如何回到 GUI

`process_and_submit_one_order()` 返回后，店铺子进程发送：

```json
{"type":"result","order_no":"...","result":"已提交"}
```

完整回传链路：

```text
main.py → run_serve_mode.emit(result)
  → 子进程 stdout
  → scheduler_core.py → ShopWorker._read_stdout()
  → ShopWorker.out_queue
  → ShopWorker.wait_response()
  → Scheduler._run_loop()
  → Scheduler._emit("order_result", ...)
  → gui/app/workers/scheduler_thread.py → SchedulerWorker._on_event()
  → event_received Qt 信号
  → gui/app/pages/run_page.py → RunPage._on_event()
     → RunPage._append_log()
     → RunPage._update_status_row()
```

`RunPage._append_log()` 同时把内容追加到界面日志框和 GUI 文件日志。

调度器收到 `result` 后会：

1. 将该店铺 `remaining` 减一；
2. 如果结果以“失败”或“分析失败”开头，连续失败数加一；
3. 正常业务结果会把连续失败数清零；
4. 连续失败达到 3 单时，关闭该店铺、标记当天 `failed`，并让排队店铺补位；
5. 等待 1～3 秒后再随机选择下一家店铺发送 `next`。

当子进程已经没有下一单时，会在下一次收到 `next` 后返回：

```json
{"type":"queue_empty"}
```

调度器随后关闭该店铺，调用 `db.mark_shop_daily_done()`，再调用 `_fill_pool()` 让尚未启动的店铺补位。

## 12. 一轮结束和资源清理

当活动池清空后，`Scheduler._run_loop()`：

1. 对本轮一直没获得并发名额的店铺调用 `db.mark_shop_daily_skipped()`；
2. 发送 `round_complete` 事件；
3. GUI 的 `run_once` 模式发送 `once_stop` 并退出循环。

之后一定进入 `Scheduler._run()` 的 `finally`：

```text
scheduler_core.py
  → Scheduler._close_all_active()
  → ShopWorker.stop()
     → 发送 {"cmd":"stop"}
     → 等待子进程退出
     → 超时则 terminate，再超时则 kill
  → Scheduler._release_lock()
```

子进程的 `main.py → run_serve_mode()` 收到 `stop` 后跳出协议循环并关闭 Playwright context。

调度线程结束后：

```text
SchedulerWorker.start() 的 finally
  → stopped Qt 信号
  → RunPage._on_stopped()
  → 恢复启动表单和按钮
```

## 13. 优雅停止、关闭 GUI 和自动休息

### 13.1 点击“优雅停止”

调用链：

```text
RunPage._on_stop_clicked()
  → RunPage._request_stop()
  → SchedulerWorker.request_stop()
  → Scheduler.request_stop()
  → threading.Event.set()
```

调度循环和等待响应逻辑检测到停止事件后进入统一收尾，再由 `ShopWorker.stop()` 先发 `stop`、等待，必要时使用终止兜底。

按当前实现，店铺子进程在执行一条 `next` 时是单线程的，只有当前订单函数返回后才能读取后续 `stop`。`ShopWorker.stop()` 默认等待 10 秒；如果当前订单长时间没有返回，之后可能进入 `terminate/kill` 兜底。因此“优雅停止”不是无限期等待当前订单完成。

### 13.2 关闭 GUI 窗口

```text
gui/app/main_window.py → MainWindow.closeEvent()
  → RunPage.is_running()
  → RunPage.begin_shutdown_stop()
  → 每 500ms 轮询任务是否停止
```

最多先等待 30 秒。仍未停止时，GUI 会询问“强制关闭”还是“继续等待”。

### 13.3 自动休息

`Scheduler._run_loop()` 在每单结束后检查连续运行时间。达到随机休息触发点后：

```text
Scheduler._emit("rest_start")
  → Scheduler._close_all_active()
  → 等待随机休息时长
  → Scheduler._emit("rest_end")
  → 重新开始一轮并恢复未完成店铺
```

虽然 GUI 使用 `run_once()`，由自动休息提前切断的轮次不会被当作本次运行结束；休息完成后仍会继续。

## 14. 每日自动运行

`RunPage.__init__()` 创建一个每 30 秒触发一次的 `QTimer`：

```text
RunPage._check_auto_schedule()
```

在时间窗口内且当天尚未自动启动时：

```text
RunPage._check_auto_schedule()
  → RunPage._start_run()
```

它复用与人工点击完全相同的调度链路。区别是打开“每日自动运行”开关时已经做过一次真实提交确认，到点启动时不再弹确认框。

离开时间窗口且任务仍在运行时：

```text
RunPage._check_auto_schedule()
  → RunPage._request_stop(reason="定时任务到点")
```

## 15. GUI 的两个辅助子流程

### 15.1 新增店铺或重新登录

入口链路：

```text
gui/app/pages/shops_page.py
  → ShopsPage._on_add_shop_clicked()，或重新登录菜单
  → gui/app/dialogs/add_shop_dialog.py
     → AddShopDialog._on_start_clicked()
  → gui/app/workers/login_worker.py
     → LoginWorker.start()
  → gui/app/workers/base_process_worker.py
     → ProcessLineIO.start()
  → 子进程：main.py --shop <店铺> --login-only
  → main.py → run_login_only_mode()
  → stealth.py → create_stealth_context()
  → login.py → wait_for_login_polling()
```

登录检测成功后子进程发送 `login_result`；`LoginWorker._on_line()` 收到后向子进程发送 `close`，让 Playwright context 干净关闭并保存登录态。

这个流程不经过 `Scheduler`，也不会采集、分析或提交订单。

### 15.2 查询申诉结果

入口链路：

```text
gui/app/pages/run_page.py
  → RunPage._on_check_results_clicked()
  → gui/app/workers/appeal_result_check_worker.py
     → AppealResultCheckWorker.start()
     → AppealResultCheckWorker._run_next()
  → ProcessLineIO.start()
  → 子进程：main.py --shop <店铺> --check-appeal-results --since-days 3
  → main.py → run_check_appeal_results_mode()
  → check_appeal_results.py → check_appeal_results()
  → db.py → record_appeal_result()
  → db.py → record_submit_result()
```

这个 Worker 按店铺严格串行执行，一家结束后才调用 `_run_next()` 启动下一家。它不使用 `init / next / stop` 调度协议，也不进入正常批量申诉流程。

## 16. 最短主调用链索引

只看正常成功路径时，可以压缩为下面这一条：

```text
gui/app/main.py
  main()
  → gui/app/main_window.py: MainWindow.__init__()
  → gui/app/pages/run_page.py: RunPage._on_start_clicked()
  → RunPage._start_run()
  → gui/app/workers/scheduler_thread.py: SchedulerWorker.start()
  → scheduler_core.py: Scheduler.run_once()
  → Scheduler._run()
  → Scheduler._run_loop()
  → Scheduler._fill_pool()
  → Scheduler._start_worker()
  → scheduler_core.py: ShopWorker.__init__()
  → 子进程 main.py: main()
  → main.py: run_serve_mode()
  → 收到 init
  → stealth.py: create_stealth_context()
  → login.py: wait_for_login()
  → main.py: run_phase1()
  → run_appeal_batch.py: collect_all_appealable_order_numbers()
  → run_appeal_batch.py: record_collected_orders()
  → 返回 ready
  → 调度器发送 next
  → main.py: process_and_submit_one_order()
  → run_appeal_batch.py: process_single_order()
  → pdd_chat.py: query_chat_records()
  → appeal_classifier.py: classify_appeal_evidence()
  → appeal_classifier.py: generate_appeal_text()
  → db.py: record_analyzed()
  → main.py: submit_single_order()
  → main.py: _process_one_appeal_record()
  → appeal_submit.py: fill_appeal_modal()
  → appeal_submit.py: click_appeal_form_submit()
  → db.py: record_appeal_result()
  → db.py: record_submit_result()
  → 子进程返回 result
  → Scheduler._emit()
  → SchedulerWorker._on_event()
  → RunPage._on_event()
```

## 17. 主要文件职责速查

| 文件 | 职责 |
|---|---|
| `gui/app/main.py` | GUI 应用入口、路径校验、单实例和 Qt 事件循环 |
| `gui/app/main_window.py` | 主窗口、四页导航、关闭时的停止协调 |
| `gui/app/pages/run_page.py` | 启动参数、真实提交确认、线程创建、日志和状态展示 |
| `gui/app/workers/scheduler_thread.py` | 把纯 Python 调度器适配成 Qt Worker 和信号 |
| `scheduler_core.py` | 多店铺池、随机逐单调度、子进程协议、失败阈值和收尾 |
| `main.py` | 店铺子进程入口、serve 协议、单订单“分析后提交”编排 |
| `run_appeal_batch.py` | 可申诉订单采集、聊天分析阶段编排 |
| `pdd_chat.py` | 聊天订单查询、分页提取和证据截图 |
| `appeal_classifier.py` | DeepSeek 分类和申诉文案生成 |
| `appeal_submit.py` | 申诉搜索、详情状态识别、表单填写、上传凭证和提交 |
| `captcha.py` / `blockers.py` | 验证码处理和页面遮挡/风控弹层处理 |
| `db.py` | SQLite 数据持久化、订单/记录/店铺每日状态 |
| `stealth.py` | 持久化浏览器上下文和拟人化操作工具 |
| `login.py` | 登录状态检查和聊天页面入口 |
