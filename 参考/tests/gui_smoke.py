"""Preview 80 sample shops in a temporary database. Never writes production records."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "windows" if os.name == "nt" else "offscreen")

from PyQt5.QtCore import Qt, QTimer, QPoint
from PyQt5.QtWidgets import QApplication, QLineEdit, QPushButton, QMessageBox
from PyQt5.QtTest import QTest
from playwright.sync_api import sync_playwright

from pdd_collector.database import Database
from pdd_collector.gui import MainWindow, ShopSelectionDialog
from pdd_collector.models import Shop, parse_rows
from pdd_collector.scraper import SNAPSHOT_JS
from pdd_collector.account_dialog import AccountsDialog
from pdd_collector.dialogs import ShopDialog


def main():
    output = Path("test-output")
    output.mkdir(exist_ok=True)
    # GUI behavior is the subject of this smoke test; use reversible test-only bytes instead of
    # depending on whether the non-interactive test process can access Windows DPAPI.
    protect_patch = patch('pdd_collector.database.protect', lambda value: b'test:' + value.encode('utf-8'))
    unprotect_patch = patch('pdd_collector.database.unprotect', lambda value: bytes(value)[5:].decode('utf-8'))
    protect_patch.start()
    unprotect_patch.start()
    with tempfile.TemporaryDirectory() as folder:
        data_dir = Path(folder)
        db = Database(data_dir / "appeals.sqlite3")
        groups = ["直营一组", "直营二组", "品牌组", "数码组"]
        db.add_shops([dict(shop_key=f"sample-{i:03d}", display_name=f"样例店铺 {i:02d}",
                          group_name=groups[(i - 1) // 20], enabled=i <= 76, max_pages=0 if i % 3 else 10)
                      for i in range(1, 81)])
        for i in range(1, 71):
            db.set_login_status(f"sample-{i:03d}", "verified" if i <= 65 else "needs_login")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel=os.environ.get("TEST_BROWSER_CHANNEL") or None)
            page = browser.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content((Path(__file__).parent / "fixtures" / "table.html").read_text(encoding="utf-8-sig"))
            records = parse_rows(page.evaluate(SNAPSHOT_JS))
            browser.close()
        run = db.start_run(Shop("sample-001", "神***店", "神***"), "2026-08-28 ~ 2026-09-03", "离线样例")
        db.save_page("sample-001", run, records, 1, 8)
        db.finish_run(run, "completed", "仅用于界面验证")
        db.save_service_status('sample-001', [dict(account=f'样例客服{i}', status=status, reply_rate='--%' if i == 3 else '100%',
            consultation_count=str(i * 10), raw_status={}) for i, status in enumerate(
            ('电脑在线', '电脑、手机都在线', '手机在线', '电脑、手机都离线'))], '离线样例')
        status_job = db.create_jobs('online-preview', ['sample-001'], 'service_status')[0]
        db.update_job(status_job['job_id'], status='completed', count=4, message='查询完成，4 个客服账号。')
        jobs = db.create_jobs("preview", [f"sample-{i:03d}" for i in range(1, 77)])
        for i, job in enumerate(jobs):
            status = "failed" if i in (1, 6) else "needs_login" if i == 4 else "partial" if i == 9 else "completed"
            db.update_job(job["job_id"], status="running")
            db.update_job(job["job_id"], status=status, pages=1 if status == "completed" else 0,
                          count=8 if i == 0 else 0, message={"failed": "样例：查询请求失败，可重试",
                          "needs_login": "样例：登录过期，请重新登录", "partial": "样例：达到采集页数上限",
                          "completed": "样例：采集完成"}[status])
        app = QApplication([])
        window = MainWindow(data_dir)
        window.setWindowTitle("多店铺控制台 · 离线界面验证（80 家样例店铺）")
        window.controller.batch_id = "preview"
        window.render_jobs()
        window.show()
        failures = []

        def verify():
            try:
                assert '任务未在当前程序版本中注册' in MainWindow.launch_error_message(
                    ValueError('login_check'), 'login_check')
                assert '数据库正在被其他程序占用' in MainWindow.launch_error_message(
                    RuntimeError('database is locked'), 'login_check')
                assert window.table.rowCount() == 8
                assert window.shop_table.rowCount() == 80
                assert window.metrics["shops"].text() == "80"
                assert window.nav.item(4).text() == '店铺在线状态查询'
                assert not hasattr(window, 'online_shops')
                assert '共 80 家店铺' in window.global_selection_label.text()
                assert window.online_results.rowCount() == 4
                assert window.batch_metrics['total'].text() == '76'
                assert window.batch_metrics['success'].text() == '72'
                assert window.batch_metrics['failed'].text() == '3'
                assert window.batch_metrics['login'].text() == '1'
                assert not window.batch_result_panel.isHidden()
                preflight_jobs = db.create_jobs(
                    'preflight-progress', ['sample-001', 'sample-002', 'sample-003'], 'login_check')
                db.update_job(preflight_jobs[0]['job_id'], status='authenticated', message='检查通过')
                db.update_job(preflight_jobs[1]['job_id'], status='needs_login', message='登录已失效')
                db.update_job(preflight_jobs[2]['job_id'], status='running', message='正在打开页面')
                window.controller.batch_id = 'preflight-progress'
                window.controller.mode = 'login_check'
                window.controller.current = preflight_jobs[2]
                window.execution_changed()
                assert not window.shop_login_check_status.isHidden()
                assert '已检查 2/3 家' in window.shop_login_check_status.text()
                assert '通过 1 家' in window.shop_login_check_status.text()
                assert '未通过 1 家' in window.shop_login_check_status.text()
                assert '样例店铺 03' in window.shop_login_check_status.text()
                assert window.progress_bar.value() == 67
                check_rows = {
                    window.shop_table.item(row, 0).data(Qt.UserRole): row
                    for row in range(window.shop_table.rowCount())}
                assert window.shop_table.item(check_rows['sample-001'], 5).text() == '√'
                assert window.shop_table.item(check_rows['sample-002'], 5).text() == '×'
                assert window.shop_table.item(check_rows['sample-003'], 5).text() == '检查中…'
                assert window.shop_table.item(check_rows['sample-002'], 6).text() != '—'
                assert window.shop_table.item(check_rows['sample-003'], 6).text() != '—'
                window.controller.batch_id = 'preview'
                window.controller.mode = 'collect'
                window.controller.current = None
                window.execution_changed()
                assert window.shop_login_check_status.isHidden()
                picker = ShopSelectionDialog(window.shops, {'sample-001'}, window)
                picker.search.setText('样例店铺 01')
                assert picker.table.rowCount() == 1
                picker._select_filtered()
                assert picker.selected_keys == {'sample-001'}
                picker.group_filter.setCurrentIndex(picker.group_filter.findData('直营二组'))
                picker.search.clear()
                assert picker.table.rowCount() == 20
                picker._select_filtered()
                assert len(picker.selected_keys) == 21
                picker.reject()
                window.online_search.setText('样例客服1')
                assert window.online_results.rowCount() == 1
                assert window.online_results.item(0, 2).text() == '电脑、手机都在线'
                window.online_search.clear()
                window.feature_selector.setCurrentIndex(window.feature_selector.findData('service_status'))
                assert '客服账号' in window.feature_description.text()
                window.feature_selector.setCurrentIndex(0)
                shop_buttons = {b.text() for b in window.stack.widget(1).findChildren(QPushButton)}
                assert not shop_buttons.intersection({'批量设置页数上限', '店铺标识导入', '备份选中店铺',
                                                     '启用', '停用', '重试失败登录', '手动登录'})
                assert '删除店铺' in shop_buttons
                assert '停止批量登录' in shop_buttons
                window.controller.busy = True
                window.controller.mode = 'account_login'
                window.controller.stopping = False
                window.execution_changed()
                assert window.shop_stop_login_button.isEnabled()
                window.controller.busy = False
                window.controller.mode = 'collect'
                window.execution_changed()
                assert not window.shop_stop_login_button.isEnabled()
                assert [window.shop_table.horizontalHeaderItem(i).text() for i in range(window.shop_table.columnCount())] == [
                    '选择', '店铺名称', '登录账号', '密码', '所属组', '最近登录检查', '预检时间', '启用状态']
                assert any(window.jobs_table.item(row, 2).text() == "采集完成，0 条记录"
                           for row in range(window.jobs_table.rowCount()))
                window.shop_table.item(0, 0).setCheckState(Qt.Checked)
                assert len(window.selected_keys) == 1
                window.group_filter.setCurrentIndex(window.group_filter.findData("直营一组"))
                assert window.shop_table.rowCount() == 20
                window.select_visible(True)
                assert len(window.selected_keys) == 21  # The earlier selection in another group is retained.
                window.shop_search.setText("样例店铺 01")
                assert window.shop_table.rowCount() == 1
                assert len(window.selected_keys) == 21
                window.shop_search.clear()
                window.group_filter.setCurrentIndex(0)
                previous_selection = set(window.selected_keys)
                window.execution_search.setText('样例店铺 60')
                assert window.execution_shops.rowCount() == 1
                window.select_execution_filtered()
                assert 'sample-060' in window.selected_keys
                assert previous_selection <= window.selected_keys
                window.execution_shops.item(0, 0).setCheckState(Qt.Unchecked)
                assert window.selected_keys == previous_selection
                window.execution_search.clear()
                assert window.execution_shops.rowCount() == 80
                window.feature_buttons.button(1).click()
                assert window.feature_selector.currentIndex() == 1
                assert set(window.selected_feature_modes()) == {
                    window.feature_selector.itemData(0), window.feature_selector.itemData(1)}
                assert window.start_button.text() == '执行已选功能（2 项）'
                launched = []
                real_launch = window.launch
                window.launch = lambda keys, mode='collect', chained=False, navigate=True: (
                    launched.append((list(keys), mode, chained, navigate)) or True)
                window._begin_feature_queue(
                    ['sample-001', 'sample-080', 'sample-001'],
                    [window.feature_selector.itemData(0)])
                assert launched[-1] == (
                    ['sample-001'], window.feature_selector.itemData(0), True, True)
                launched.clear()
                window.start_selected()
                assert [item[1] for item in launched] == [window.feature_selector.itemData(0)]
                assert window.pending_feature_modes == [window.feature_selector.itemData(1)]
                real_refresh_all = window.refresh_all
                real_auto_export = window.auto_export_finished_batch
                window.refresh_all = lambda: None
                window.auto_export_finished_batch = lambda: None
                window.controller.mode = launched[0][1]
                window.on_execution_finished()
                app.processEvents()
                assert [item[1] for item in launched] == [
                    window.feature_selector.itemData(0), window.feature_selector.itemData(1)]
                assert all(item[2] for item in launched)
                previous_selection = set(window.selected_keys)
                window.selected_keys = {'sample-001', 'sample-080'}
                window.check_selected_logins()
                assert launched[-1] == (['sample-001'], 'login_check', False, False)
                window.selected_keys = previous_selection
                window.launch = real_launch
                window.refresh_all = real_refresh_all
                window.auto_export_finished_batch = real_auto_export
                window.pending_feature_modes.clear()
                window.feature_run_total = 0
                window.feature_selector.setCurrentIndex(0)
                assert window.feature_buttons.button(0).isChecked()
                window.feature_buttons.button(1).click()
                assert window.selected_feature_modes() == [window.feature_selector.itemData(0)]
                window.search.setText("260828-076346826250895")
                window.search_records()
                assert window.table.rowCount() == 1
                window.search.clear()
                window.search_records()
                window.refresh_all()
                today = window.work_order_end.date()
                assert window.work_order_start.date().day() == 1
                assert (window.work_order_start.date().year(), window.work_order_start.date().month()) == (today.year(), today.month())
                window.statusBar().showMessage("离线界面验证：80 家样例店铺；所有样例仅存在临时数据库。")
                for index, name in ((1, "shops"), (2, "execution"), (0, "overview"), (3, "reports"),
                                    (4, "online-status"), (5, "customer-service"), (6, "appeal-amount"),
                                    (7, "work-order")):
                    window.nav.setCurrentRow(index)
                    app.processEvents()
                    assert window.grab().save(str(output / f"gui-{name}.png"))
                window.nav.setCurrentRow(1)
                window.resize(1210, 850)
                app.processEvents()
                assert window.grab().save(str(output / "gui-compact.png"))
                account_db = Database(data_dir / "accounts-test.sqlite3")
                dialog = AccountsDialog(account_db, window)
                dialog.show()
                clipboard = SimpleNamespace(text=lambda: "账号\t密码\t店铺名\t分组\nsample-login-001\tSample-only-001\t样例旗舰店\t直营组\nsample-login-002\tSample-only-002")
                with patch('pdd_collector.account_dialog.QApplication.clipboard', return_value=clipboard):
                    dialog.paste()
                app.processEvents()
                assert dialog.table.rowCount() == 2
                assert dialog.table.item(0, 1).data(Qt.DisplayRole) == "Sample-only-001"
                assert dialog.table.item(0, 1).data(Qt.EditRole) == "Sample-only-001"
                dialog.password_toggle.click()
                assert dialog.table.item(0, 1).data(Qt.DisplayRole) == '••••••••'
                assert dialog.table.item(0, 1).data(Qt.EditRole) == 'Sample-only-001'
                dialog.password_toggle.click()
                assert dialog.grab().save(str(output / "gui-accounts.png"))
                dialog.table.setCurrentCell(0, 1)
                dialog.table.editItem(dialog.table.item(0, 1))
                app.processEvents()
                editor = QApplication.focusWidget()
                assert isinstance(editor, QLineEdit)
                assert editor.echoMode() == QLineEdit.Normal
                editor.setText("Edited-test-only")
                save = next(button for button in dialog.findChildren(QPushButton) if button.text() == "保存列表")
                QTest.mouseClick(save, Qt.LeftButton)
                app.processEvents()
                assert len(dialog.keys) == 2
                assert account_db.get_credentials(dialog.keys[0])[1] == "Edited-test-only"
                assert not dialog.login_after_save
                unnamed = next(s for s in account_db.managed_shops() if s['shop_key'] == dialog.keys[1])
                assert unnamed['display_name'] == 'sample-login-002'
                single = ShopDialog(window)
                single.username.setText('sample-login-single')
                single.password.setText('single-test-only')
                single.validate()
                assert single.result() == single.Accepted
                assert single.record()['display_name'] == 'sample-login-single'
                assert single.password.echoMode() == QLineEdit.Normal
                assert single.layout().labelForField(single.username).text() == '登录账号'
                assert not hasattr(single, 'key')
                window.db.add_shops([single.record()])
                window.refresh_all()
                window.shop_search.setText('sample-login-single')
                assert window.shop_table.rowCount() == 1
                assert window.shop_table.item(0, 3).text() == 'single-test-only'
                saved = next(s for s in window.shops if s['username'] == 'sample-login-single')
                edit = ShopDialog(window, {**saved, 'password': window.passwords[saved['shop_key']]})
                assert edit.password.text() == 'single-test-only'
                assert edit.record()['password'] == ''  # An unchanged visible password is not rewritten.
                edit.password_eye.trigger()
                assert edit.password.echoMode() == QLineEdit.Password
                edit.password_eye.trigger()
                assert edit.password.echoMode() == QLineEdit.Normal
                header = window.shop_table.horizontalHeader()
                eye_position = QPoint(header.sectionViewportPosition(3) + 12, header.height() // 2)
                QTest.mouseClick(header.viewport(), Qt.LeftButton, pos=eye_position)
                assert window.shop_table.item(0, 3).text() == '••••••••'
                assert window.shop_table.item(0, 3).toolTip() == '••••••••'
                window.refresh_all()
                assert window.shop_table.item(0, 3).text() == '••••••••'
                QTest.mouseClick(header.viewport(), Qt.LeftButton, pos=eye_position)
                assert window.shop_table.item(0, 3).text() == 'single-test-only'
                window.selected_keys = {saved['shop_key']}
                position = window.shop_table.visualItemRect(window.shop_table.item(0, 0)).center()
                for label, enabled in (('停用', 0), ('启用', 1)):
                    with patch('pdd_collector.gui.QMenu.exec_', lambda menu, *args: next(a for a in menu.actions() if a.text().startswith(label))):
                        window.shop_context_menu(position)
                    assert next(s for s in window.shops if s['shop_key'] == saved['shop_key'])['enabled'] == enabled
                with patch.object(window, 'launch') as relogin_launch:
                    with patch('pdd_collector.gui.QMenu.exec_', lambda menu, *args:
                               next(a for a in menu.actions() if a.text() == '重新登录此店铺')):
                        window.shop_context_menu(position)
                    relogin_launch.assert_called_once_with(
                        [saved['shop_key']], 'account_login', navigate=False)
                with patch('pdd_collector.gui.QMessageBox.exec_', return_value=QMessageBox.Yes):
                    window.delete_selected_shops()
                assert saved['shop_key'] not in {s['shop_key'] for s in window.shops}
                assert saved['shop_key'] not in window.selected_keys
                # The feature page shares the shop selection and launches the correct task.
                window.selected_keys = {'sample-001'}
                with patch.object(window, 'launch') as launch:
                    window.query_online_status()
                    launch.assert_called_once_with(['sample-001'], 'service_status')
                db.update_job(status_job['job_id'], status='failed', message='样例查询失败，保留旧数据')
                window.refresh_online_status()
                assert window.online_results.rowCount() == 4
                assert all(window.online_results.item(row, 6).text() == '失败' for row in range(4))
                # Reopening the database demonstrates list persistence without putting secrets in its listing.
                assert len(Database(account_db.path).managed_shops()) == 2
                assert "Edited-test-only" not in str(account_db.managed_shops())
                print("Account GUI OK: optional names, visible passwords in list and editor, committed edits, encrypted persistence")
                print("Shop management OK: toolbar removals, eye toggles, right-click enable/disable, deletion")
                print("GUI smoke OK: shared shop scope, group filters, batch summary, result actions, page screenshots")
            except Exception as exc:
                failures.append(exc)
            finally:
                window.close()
                app.quit()

        QTimer.singleShot(250, verify)
        app.exec_()
        if failures:
            raise failures[0]
    unprotect_patch.stop()
    protect_patch.stop()


if __name__ == "__main__":
    main()
