import html
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import Error, sync_playwright

import aftersale_appeal as appeal


class CascaderOptionTests(unittest.TestCase):
    def test_option_is_exactly_located_inside_menu(self):
        page, menu = MagicMock(), MagicMock()
        option = menu.get_by_text.return_value
        option.count.return_value = 1
        with patch.object(appeal, "stealth_safe_click") as click:
            appeal._click_cascader_option(page, menu, appeal.GOODS_PREFERRED_REASON)
        menu.get_by_text.assert_called_once_with(appeal.GOODS_PREFERRED_REASON, exact=True)
        click.assert_called_once_with(page, option, f"申诉原因：{appeal.GOODS_PREFERRED_REASON}")

    def test_missing_or_duplicate_option_does_not_click(self):
        for count in (0, 2):
            with self.subTest(count=count):
                menu = MagicMock()
                menu.get_by_text.return_value.count.return_value = count
                with patch.object(appeal, "stealth_safe_click") as click:
                    with self.assertRaisesRegex(RuntimeError, f"匹配数量异常：{count}"):
                        appeal._click_cascader_option(MagicMock(), menu, "原因")
                click.assert_not_called()


class CascaderBrowserTests(unittest.TestCase):
    """离线页面复现鼠标悬停新增同名 portal，不访问平台或提交订单。"""

    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(headless=True)
        except Error as error:
            cls.pw.stop()
            raise unittest.SkipTest(f"本机没有可用的测试浏览器：{error}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)

    def make_form(self, reasons, appeal_items="货款申诉、纠纷退款率申诉", second_reason=False):
        checked = ''.join(
            f'<label data-testid="beast-core-checkbox" data-checked="true">{name}</label>'
            for name in ('货款申诉', '运费申诉', '纠纷退款率申诉')
            if name in appeal_items
        )
        items = "".join(
            '<li data-testid="beast-core-cascader-list-item" data-disabled="false">'
            '<div data-testid="beast-core-ellipsis"><div>'
            '<style>.ellipsis{white-space:nowrap}</style>'
            f'{html.escape(reason)}</div></div></li>' for reason in reasons
        )
        self.page.set_content('''
            <div id="modal">
            <div data-testid="beast-core-form-item"><span>申诉订单号</span><a>260902-651511733860402</a></div>
            <div data-testid="beast-core-form-item"><span>申诉项</span><div id="subInfoVOS">''' + checked + '''</div></div>
            <div data-testid="beast-core-form-item"><span>申诉原因</span><div id="subInfoVOS[0].reason">
                <input data-testid="beast-core-cascader-htmlInput" readonly>
            </div></div>''' + (
                '<div id="subInfoVOS[1].reason"><input data-testid="beast-core-cascader-htmlInput" readonly></div>'
                if second_reason else ''
            ) + '''</div>
            <div id="menu" data-testid="beast-core-portal" style="display:none">
                <ul data-testid="beast-core-cascader-list-wrapper-0">''' + items + '''</ul>
            </div>
            <div data-testid="beast-core-portal" style="display:none">hidden portal</div>
            <script>
                (() => {
                let input = document.querySelector('input');
                const menu = document.querySelector('#menu');
                document.querySelectorAll('input').forEach(field => field.onclick = () => {
                    input = field;
                    menu.style.display = 'block';
                });
                function wire(item, child) {
                    item.onmouseenter = () => {
                        document.querySelector('#tooltip')?.remove();
                        const tip = document.createElement('div');
                        tip.id = 'tooltip';
                        tip.dataset.testid = 'beast-core-portal';
                        tip.style.cssText = 'position:fixed;right:0;top:0';
                        tip.textContent = item.innerText;
                        document.body.append(tip);
                    };
                    item.onclick = () => {
                        if (item.innerText === '其他' && !child) {
                            if (!menu.querySelector('[data-testid="beast-core-cascader-list-wrapper-1"]')) {
                                const list = document.createElement('ul');
                                list.dataset.testid = 'beast-core-cascader-list-wrapper-1';
                                const option = document.createElement('li');
                                option.dataset.testid = 'beast-core-cascader-list-item';
                                option.textContent = '非以上申诉原因';
                                list.append(option);
                                menu.append(list);
                                wire(option, true);
                            }
                        } else {
                            input.value = child ? '其他 / ' + item.innerText : item.innerText;
                            menu.style.display = 'none';
                            document.querySelector('#tooltip')?.remove();
                        }
                    };
                }
                menu.querySelectorAll('li').forEach(item => wire(item, false));
                })();
            </script>''')
        return self.page.locator('#modal')

    def hover_then_click(self, page, locator, description):
        locator.hover(timeout=1500)
        if description.startswith("申诉原因："):
            page.locator('#tooltip').wait_for(state="visible", timeout=1500)
            # 同名提示框已出现；旧定位此时找不到列表，而新定位仍匹配一项。
            old = page.locator('[data-testid="beast-core-portal"]:visible').last
            self.assertEqual(old.locator('ul').count(), 0)
            self.assertEqual(locator.count(), 1)
        locator.click(timeout=1500)

    def test_goods_and_combined_reasons_survive_new_hover_tooltip(self):
        cases = (
            (appeal.GOODS_APPEAL_ITEM, appeal.GOODS_PREFERRED_REASON),
            ("纠纷退款率申诉、货款申诉", appeal.GOODS_FALLBACK_REASON),
            ("货款申诉、运费申诉", appeal.GOODS_FALLBACK_REASON),
            ("运费申诉、货款申诉、纠纷退款率申诉", appeal.GOODS_FALLBACK_REASON),
        )
        for item, expected in cases:
            with self.subTest(item=item):
                modal = self.make_form([
                    "消费者反馈商品错发，但实际未错发",
                    appeal.GOODS_PREFERRED_REASON, appeal.GOODS_FALLBACK_REASON,
                ], appeal_items=item)
                with patch.object(appeal, "stealth_safe_click", side_effect=self.hover_then_click):
                    result = appeal.select_appeal_reason(self.page, modal, item)
                self.assertEqual(result, expected)
                self.assertEqual(modal.locator('input').input_value(), expected)

    def test_goods_and_dispute_choose_fallback_without_quality_option(self):
        modal = self.make_form([appeal.GOODS_FALLBACK_REASON])
        with patch.object(appeal, "stealth_safe_click", side_effect=self.hover_then_click):
            result = appeal.select_appeal_reason(self.page, modal, "货款申诉、纠纷退款率申诉")
        self.assertEqual(result, appeal.GOODS_FALLBACK_REASON)

    def test_goods_and_dispute_keep_preselected_fallback_without_opening_menu(self):
        modal = self.make_form([])
        modal.locator('input').evaluate('(input, value) => input.value = value', appeal.GOODS_FALLBACK_REASON)
        with patch.object(appeal, "stealth_safe_click") as click:
            result = appeal.select_appeal_reason(self.page, modal, "纠纷退款率申诉、货款申诉")
        self.assertEqual(result, appeal.GOODS_FALLBACK_REASON)
        click.assert_not_called()

    def test_goods_and_dispute_replace_preselected_quality_reason(self):
        modal = self.make_form([appeal.GOODS_PREFERRED_REASON, appeal.GOODS_FALLBACK_REASON])
        modal.locator('input').evaluate('(input, value) => input.value = value', appeal.GOODS_PREFERRED_REASON)
        with patch.object(appeal, "stealth_safe_click", side_effect=self.hover_then_click):
            result = appeal.select_appeal_reason(self.page, modal, "货款申诉、纠纷退款率申诉")
        self.assertEqual(result, appeal.GOODS_FALLBACK_REASON)
        self.assertEqual(modal.locator('input').input_value(), appeal.GOODS_FALLBACK_REASON)

    def test_fallback_reason_survives_tooltip(self):
        modal = self.make_form(["集运仓/二段物流长时间未退货", appeal.GOODS_FALLBACK_REASON])
        with patch.object(appeal, "stealth_safe_click", side_effect=self.hover_then_click):
            result = appeal.select_appeal_reason(self.page, modal, appeal.GOODS_APPEAL_ITEM)
        self.assertEqual(result, appeal.GOODS_FALLBACK_REASON)

    def test_freight_parent_and_child_survive_tooltip(self):
        modal = self.make_form([appeal.FREIGHT_REASON_PARENT])
        with patch.object(appeal, "stealth_safe_click", side_effect=self.hover_then_click):
            result = appeal.select_appeal_reason(self.page, modal, appeal.FREIGHT_APPEAL_ITEM)
        self.assertEqual(result, appeal.FREIGHT_REASON)

    def test_combined_two_fields_use_goods_leaf_and_freight_two_level_path(self):
        modal = self.make_form([appeal.GOODS_FALLBACK_REASON, '其他'], appeal_items='货款申诉、运费申诉', second_reason=True)
        with patch.object(appeal, 'stealth_safe_click', side_effect=self.hover_then_click):
            result = appeal.select_appeal_reason(self.page, modal, '运费申诉、货款申诉')
        self.assertEqual(result, '非以上申诉原因')
        self.assertEqual(modal.locator('[id="subInfoVOS[0].reason"] input').input_value(), '非以上申诉原因')
        self.assertEqual(modal.locator('[id="subInfoVOS[1].reason"] input').input_value(), '其他 / 非以上申诉原因')

    def test_zero_matches_collect_actual_item_and_available_reasons(self):
        modal = self.make_form([appeal.GOODS_PREFERRED_REASON])
        with tempfile.TemporaryDirectory() as folder, patch.object(appeal, "DEBUG_DIR", Path(folder)), \
                patch.object(appeal, "stealth_safe_click", side_effect=self.hover_then_click), \
                redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(RuntimeError, "匹配数量异常：0"):
                appeal.select_appeal_reason(self.page, modal, "纠纷退款率申诉、货款申诉")
            reports = list(Path(folder).rglob('*.html'))
            self.assertEqual(len(reports), 1)
            data = json.loads(reports[0].with_suffix('.json').read_text(encoding='utf-8'))
            self.assertEqual(data['appeal_item'], "纠纷退款率申诉、货款申诉")
            self.assertEqual(data['order_no'], "260902-651511733860402")
            self.assertIn('subInfoVOS', data['appeal_item_fields'][0]['html'])
            self.assertIn('货款申诉', data['appeal_item_fields'][0]['text'])
            self.assertIn('beast-core-cascader-htmlInput', data['reason_fields'][0]['html'])
            self.assertEqual(data['portals'][0]['menus'][0]['options'][0]['text'], appeal.GOODS_PREFERRED_REASON)
            self.assertEqual(data['capture_errors'], {})
            self.assertIn(str(reports[0]), output.getvalue())
            self.assertIn('&lt;div', reports[0].read_text(encoding='utf-8'))


class ReasonDiagnosticFailureTests(unittest.TestCase):
    def test_success_has_no_diagnostics(self):
        with patch.object(appeal, '_select_appeal_reason', return_value='原因'), \
                patch.object(appeal, 'save_reason_selection_diagnostics') as save:
            self.assertEqual(appeal.select_appeal_reason(MagicMock(), MagicMock(), '货款申诉'), '原因')
        save.assert_not_called()

    def test_all_selection_errors_are_captured_without_replacing_exception(self):
        for error in (RuntimeError('匹配数量异常：0'), RuntimeError('读回校验失败'), Error('page closed')):
            with self.subTest(error=str(error)), \
                    patch.object(appeal, '_select_appeal_reason', side_effect=error), \
                    patch.object(appeal, 'save_reason_selection_diagnostics') as save:
                page, modal = MagicMock(), MagicMock()
                with self.assertRaises(type(error)) as caught:
                    appeal.select_appeal_reason(page, modal, '货款申诉')
                self.assertIs(caught.exception, error)
                save.assert_called_once_with(page, modal, '货款申诉', error)

    def test_disk_failure_preserves_original_selection_error(self):
        error = RuntimeError('no matching reason')
        with patch.object(appeal, '_select_appeal_reason', side_effect=error), \
                patch.object(appeal, 'save_reason_selection_diagnostics', side_effect=PermissionError('read only')), \
                redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(RuntimeError) as caught:
                appeal.select_appeal_reason(MagicMock(), MagicMock(), '货款申诉')
        self.assertIs(caught.exception, error)
        self.assertIn('read only', output.getvalue())

    def test_unavailable_page_still_saves_metadata_and_capture_errors(self):
        page, modal = MagicMock(), MagicMock()
        page.url = 'https://example.test/detail?orderSn=260902-651511733860402'
        page.locator.return_value.evaluate_all.side_effect = Error('page closed')
        modal.get_by_text.return_value.locator.return_value.evaluate_all.side_effect = Error('modal gone')
        with tempfile.TemporaryDirectory() as folder, patch.object(appeal, 'DEBUG_DIR', Path(folder)), \
                redirect_stdout(io.StringIO()):
            report = appeal.save_reason_selection_diagnostics(page, modal, '货款申诉', Error('original'))
            data = json.loads(report.with_suffix('.json').read_text(encoding='utf-8'))
            self.assertEqual(data['original_error'], 'original')
            self.assertIn('page closed', data['capture_errors']['portals'])
            self.assertIn('modal gone', data['capture_errors']['appeal_item_fields'])
            self.assertTrue(report.is_file())
