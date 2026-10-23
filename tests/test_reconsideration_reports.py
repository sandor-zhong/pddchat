from __future__ import annotations

import csv
import os
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt5 import sip
    from openpyxl import load_workbook
    from PyQt5.QtChart import QChartView
    from PyQt5.QtCore import QCoreApplication, QEvent
    from PyQt5.QtWidgets import QApplication
except ImportError:
    raise unittest.SkipTest("需要 GUI venv 中的 PyQt5")
else:
    from gui.app.pages.reports_page import (
        _aftersale_metrics,
        _build_aftersale_rate_chart,
        _build_aftersale_status_chart,
        _build_aftersale_trend_chart,
        _build_reconsider_rate_chart,
        _build_reconsider_status_chart,
        _build_share_chart,
        _build_status_chart,
        _replace_chart,
        _reconsider_metrics,
        _status_metrics,
        ReportsPage,
    )
    from gui.app.services import export_service

import db

_QT_APP = QApplication.instance() or QApplication([])


class ReconsiderationReportRulesTest(unittest.TestCase):
    def test_replaced_chart_is_deleted_and_disposal_is_counted(self) -> None:
        old_chart = _build_status_chart([])
        view = QChartView(old_chart)
        new_chart = _build_status_chart([])
        with patch("gui.app.pages.reports_page.memory_monitor.increment_runtime_metric") as metric:
            _replace_chart(view, new_chart)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

        self.assertIs(view.chart(), new_chart)
        self.assertTrue(sip.isdeleted(old_chart))
        self.assertEqual(
            [call.args[0] for call in metric.call_args_list],
            [
                "report_charts_created",
                "report_charts_dispose_requested",
            ],
        )
        view.close()

    def test_report_pollers_only_run_while_report_page_is_active(self) -> None:
        with (
            patch("gui.app.pages.reports_page.db.list_appeal_report_rows", return_value=[]),
            patch("gui.app.pages.reports_page.db.list_reconsideration_report_rows", return_value=[]),
            patch("gui.app.pages.reports_page.db.list_aftersale_appeal_records", return_value=[]),
        ):
            page = ReportsPage()

        self.assertFalse(page._polling_active)
        self.assertFalse(page._poller._timer.isActive())
        self.assertFalse(page._reconsider_poller._timer.isActive())
        self.assertFalse(page._aftersale_poller._timer.isActive())

        pollers = (page._poller, page._reconsider_poller, page._aftersale_poller)
        start_patches = [patch.object(poller, "start") for poller in pollers]
        stop_patches = [patch.object(poller, "stop") for poller in pollers]
        starts = [item.start() for item in start_patches]
        stops = [item.start() for item in stop_patches]
        try:
            page.set_polling_active(True)
            self.assertTrue(all(mock.call_args.kwargs == {"immediate": True} for mock in starts))
            page.set_polling_active(False)
            self.assertTrue(all(mock.call_count == 1 for mock in stops))
        finally:
            for item in reversed(stop_patches + start_patches):
                item.stop()
            page.close()

    def test_aftersale_pass_rate_includes_partial_and_excludes_unfinished(self) -> None:
        rows = (
            [{"status": "申诉成功", "appeal_amount": "10.00"}] * 3
            + [{"status": "部分通过", "appeal_amount": "4.00"}] * 2
            + [{"status": "申诉失败", "appeal_amount": "8.00"}] * 5
            + [{"status": "审核中", "appeal_amount": "6.00"}] * 8
            + [{"status": "已撤回", "appeal_amount": "2.00"}] * 2
            + [{"status": "材料已准备", "appeal_amount": "99.00"}] * 11
        )

        metrics = _aftersale_metrics(rows)

        self.assertEqual(metrics["申诉总数"], 20)
        self.assertEqual(metrics["已出结果"], 10)
        self.assertEqual(metrics["售后通过率"], "50.0%")
        self.assertEqual(metrics["待出结果"], 8)
        self.assertEqual(metrics["成功申诉金额"], "¥30.00")
        self.assertEqual(metrics["部分通过返还金额"], "¥8.00")

        status_chart = _build_aftersale_status_chart(rows)
        self.assertIn("共 20 条", status_chart.title())
        self.assertEqual(
            status_chart.statuses,
            ["审核中", "申诉成功", "部分通过", "申诉失败", "已撤回"],
        )
        self.assertNotIn("材料已准备", status_chart.statuses)
        self.assertEqual(
            [label.text() for label in status_chart.value_labels],
            ["8 条", "3 条", "2 条", "5 条", "2 条"],
        )

        rate_chart = _build_aftersale_rate_chart(rows)
        self.assertIn("售后通过率 50.0%", rate_chart.title())
        self.assertIn("已出结果 10 条", rate_chart.title())
        self.assertEqual(rate_chart.rate_center_label.text(), "50.0%")

    def test_aftersale_trend_has_success_partial_and_failure_series(self) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        chart = _build_aftersale_trend_chart(
            [
                {"status": "申诉成功", "platform_appealed_at": timestamp},
                {"status": "部分通过", "platform_appealed_at": timestamp},
                {"status": "申诉失败", "platform_appealed_at": timestamp},
                {"status": "审核中", "platform_appealed_at": timestamp},
            ],
            7,
        )

        self.assertIn("阶段通过率 66.7%", chart.title())
        self.assertEqual(
            [series.name() for series in chart.series()],
            ["申诉成功", "部分通过", "申诉失败"],
        )
        self.assertTrue(all(series.count() == 7 for series in chart.series()))

    def test_aftersale_pass_rate_has_placeholder_without_result(self) -> None:
        metrics = _aftersale_metrics([{"status": "待处理"}, {"status": "结果待确认"}])

        self.assertEqual(metrics["售后通过率"], "—")
        self.assertEqual(metrics["已出结果"], 0)

    def test_bar_value_labels_follow_qt_visual_category_order(self) -> None:
        chart = _build_status_chart(
            [{"status": "submitted"}] * 5
            + [{"status": "review_unknown"}] * 10
            + [{"status": "approved"}] * 29
            + [{"status": "rejected"}] * 96
        )
        view = QChartView(chart)
        view.resize(800, 260)
        view.show()
        _QT_APP.processEvents()

        top_to_bottom = [
            label.text() for label in sorted(chart.value_labels, key=lambda label: label.y())
        ]
        view.close()

        self.assertEqual(top_to_bottom, ["96 条", "29 条", "10 条", "5 条"])

    def test_pass_rate_excludes_pending_and_reviewing_records(self) -> None:
        rows = (
            [
                {
                    "status": "申诉成功",
                    "refund_amount": "3.00",
                    "penalty_amount": "3.00",
                }
            ]
            * 11
            + [{"status": "申诉失败", "penalty_amount": "3.00"}] * 11
            + [{"status": "审核中", "penalty_amount": "3.00"}] * 89
            + [{"status": "待处理", "penalty_amount": "3.00"}] * 6
            + [{"status": "复议结果待确认", "penalty_amount": "3.00"}] * 3
        )
        metrics = _reconsider_metrics(rows)

        self.assertEqual(metrics["已提交"], 120)
        self.assertEqual(metrics["复议成功"], 11)
        self.assertEqual(metrics["复议失败"], 11)
        self.assertEqual(metrics["复议通过率"], "50.0%")
        self.assertEqual(metrics["复议失败率"], "50.0%")
        self.assertEqual(metrics["成功返还金额"], "¥33.00")
        self.assertEqual(metrics["待出结果金额"], "¥294.00")
        self.assertEqual(metrics["总复议金额"], "¥360.00")
        self.assertNotIn("失败复议金额", metrics)

        chart = _build_reconsider_rate_chart(rows)
        self.assertIn("复议通过率 50.0%", chart.title())
        self.assertIn("已出结果 22 条", chart.title())
        self.assertEqual(chart.rate_center_label.text(), "50.0%")
        status_chart = _build_reconsider_status_chart(rows)
        self.assertEqual(
            [label.text() for label in status_chart.value_labels],
            ["6 条", "89 条", "3 条", "11 条", "11 条"],
        )

    def test_first_appeal_report_only_emphasizes_post_submit_outcomes(self) -> None:
        rows = (
            [{"status": "approved", "compensation_amount": "3.00"}] * 11
            + [{"status": "rejected", "compensation_amount": "3.00"}] * 11
            + [{"status": "submitted", "compensation_amount": "3.00"}] * 78
            + [{"status": "review_unknown", "compensation_amount": "3.00"}]
            * 20
        )
        metrics = _status_metrics(rows)

        self.assertEqual(metrics["已提交"], 120)
        self.assertEqual(metrics["待出结果"], 98)
        self.assertEqual(metrics["申诉通过率"], "50.0%")
        self.assertEqual(metrics["申诉失败率"], "50.0%")
        self.assertEqual(metrics["通过涉及金额"], "¥33.00")
        self.assertEqual(metrics["待出结果金额"], "¥294.00")
        self.assertEqual(metrics["已提交涉及总金额"], "¥360.00")
        self.assertNotIn("申诉失败涉及金额", metrics)

        chart = _build_share_chart(rows)
        self.assertIn("申诉通过率 50.0%", chart.title())
        self.assertEqual(chart.rate_center_label.text(), "50.0%")
        status_chart = _build_status_chart(rows)
        self.assertEqual(
            [label.text() for label in status_chart.value_labels],
            ["78 条", "20 条", "11 条", "11 条"],
        )

    def test_reconsideration_csv_uses_date_only_and_current_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "reconsider.csv"
            export_service.export_reconsiderations_to_csv(
                [
                    {
                        "shop": "店铺A",
                        "order_no": "260001-1",
                        "status": "申诉成功",
                        "platform_reconsidered_at": "2026-08-26 14:32:52",
                        "result_checked_at": "2026-08-27T01:02:03+00:00",
                    }
                ],
                path,
            )
            with path.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.reader(file))

        self.assertIn("复议状态", rows[0])
        self.assertIn("平台复议时间", rows[0])
        data = dict(zip(rows[0], rows[1], strict=True))
        self.assertEqual(data["平台复议时间"], "2026-08-26")
        self.assertEqual(data["最后查询时间"], "2026-08-27")

    def test_reconsideration_excel_uses_date_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "reconsider.xlsx"
            export_service.export_reconsiderations_to_excel(
                [
                    {
                        "shop": "店铺A",
                        "order_no": "260001-1",
                        "platform_reconsidered_at": "2026-08-26 14:32:52",
                        "result_checked_at": "2026-08-27T01:02:03+00:00",
                    }
                ],
                path,
            )
            workbook = load_workbook(path, read_only=True)
            sheet = workbook.active
            headers = [cell.value for cell in sheet[1]]
            values = [cell.value for cell in sheet[2]]
            workbook.close()

        data = dict(zip(headers, values, strict=True))
        self.assertEqual(data["平台复议时间"], "2026-08-26")
        self.assertEqual(data["最后查询时间"], "2026-08-27")


class ReconsiderationReportDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._old_path = db.DB_PATH
        self._old_connection = db._connection
        db.DB_PATH = Path(self._temp_dir.name) / "test.db"
        db._connection = None

    def tearDown(self) -> None:
        if db._connection is not None:
            db._connection.close()
        db._connection = self._old_connection
        db.DB_PATH = self._old_path
        self._temp_dir.cleanup()

    def test_report_row_joins_original_order_and_keeps_reconsider_status(self) -> None:
        db.record_collected(
            "店铺A",
            "260001-1",
            issue_type="商品问题",
            goods_title="原订单商品",
        )
        db.record_ticket_sn("店铺A", "260001-1", "MP10001")
        db.record_order_review_status("店铺A", "260001-1", "申诉未通过已处罚")
        db.record_reconsideration_discovered(
            "店铺A",
            "260001-1",
            "record-1",
            issue_type="商品问题",
            penalty_amount="3.00",
            punished_at="2026-08-01 10:00:00",
        )
        db.record_reconsideration_review_result(
            "店铺A",
            "260001-1",
            "record-1",
            "申诉成功",
            result_text="申诉成功",
            platform_reconsidered_at="2026-08-26 14:32:52",
            refund_amount="3.00",
        )

        row = db.list_reconsideration_report_rows("店铺A")[0]
        self.assertEqual(row["original_appeal_status"], "申诉未通过已处罚")
        self.assertEqual(row["status"], "申诉成功")
        self.assertEqual(row["goods_title"], "原订单商品")
        self.assertEqual(row["ticket_sn"], "MP10001")

    def test_first_appeal_report_excludes_pre_submit_and_expired_records(self) -> None:
        for index, result_text in enumerate(
            ("已提交", "已通过", "已驳回-跳过", "已超时-跳过"), start=1
        ):
            order_no = f"260001-{index}"
            db.record_collected(
                "店铺A",
                order_no,
                issue_type="商品问题",
                goods_title=f"商品{index}",
                compensation_amount="3.00",
            )
            db.record_submit_result("店铺A", order_no, result_text)
            db.record_appeal_result(
                "店铺A",
                order_no,
                f"record-{index}",
                "商品问题",
                result_text,
            )

        rows = db.list_appeal_report_rows("店铺A")
        self.assertEqual(
            {row["status"] for row in rows},
            {"submitted", "approved", "rejected"},
        )
        self.assertTrue(all(row["goods_title"].startswith("商品") for row in rows))

    def test_first_appeal_report_uses_order_result_over_stale_record(self) -> None:
        db.record_collected("店铺A", "260001-9", issue_type="商品问题")
        db.record_submit_result("店铺A", "260001-9", "已提交")
        db.record_appeal_result(
            "店铺A", "260001-9", "record-9", "商品问题", "已提交"
        )
        db.record_order_review_status("店铺A", "260001-9", "申诉通过")

        rows = db.list_appeal_report_rows("店铺A")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "申诉通过")


if __name__ == "__main__":
    unittest.main()
