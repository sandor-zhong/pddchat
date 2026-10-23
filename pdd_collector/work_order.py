"""Capture the visible after-sales work-order area and extract its visible rows."""
from datetime import date, datetime
import hashlib
from pathlib import Path
import time
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .models import CollectionError
from .scraper import Collector, CollectionResult


WORK_ORDER_URL = "https://mms.pinduoduo.com/aftersales/work_order/list?msfrom=mms_sidenav"
WORK_ORDER_SELECTOR = "#mf-mms-aftersales-container"

WORK_ORDER_CAPTURE_BOX_JS = r"""() => {
    const visible = el => !!el && el.getClientRects().length > 0 &&
        getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
    const root = document.querySelector('#mf-mms-aftersales-container');
    if (!visible(root)) return null;
    const candidates = [
        ...root.querySelectorAll('[class*="Spn_container_"]'),
        root,
    ];
    const container = candidates.find(candidate =>
        [...candidate.children].filter(el => el.tagName === 'DIV' && visible(el)).length >= 4);
    if (!container) return {child_count: 0};
    const children = [...container.children]
        .filter(el => el.tagName === 'DIV' && visible(el)).slice(0, 4);
    if (children.length < 4) return {child_count: children.length};
    const rects = children.map(el => el.getBoundingClientRect());
    const left = Math.min(...rects.map(rect => rect.left));
    const top = Math.min(...rects.map(rect => rect.top));
    const right = Math.max(...rects.map(rect => rect.right));
    const bottom = Math.max(...rects.map(rect => rect.bottom));
    return {child_count: children.length, x: left, y: top,
        width: right - left, height: bottom - top};
}"""

WORK_ORDER_SNAPSHOT_JS = r"""() => {
    const text = el => (el?.innerText ?? el?.textContent ?? '').replace(/\s+/g, ' ').trim();
    const visible = el => !!el && el.getClientRects().length > 0 &&
        getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
    const root = document.querySelector('#mf-mms-aftersales-container');
    if (!visible(root)) return null;
    const titles = [...root.querySelectorAll('[class*="BeastBlockTitle___wrapper___"]')].filter(visible);
    const result = titles.find(block => text(block.querySelector('[class*="BeastBlockTitle___title___"]')) === '查询结果');
    const resultText = text(result?.querySelector('[class*="BeastBlockTitle___subContent___"]'));
    const match = resultText.match(/共查询到\s*([\d,]+)\s*个工单/);
    return {work_order_count: match ? Number(match[1].replace(/,/g, '')) : null,
        result_text: resultText, root_text: text(root)};
}"""


class WorkOrderLoginRequired(CollectionError):
    pass


def parse_work_order_count(snapshot):
    if not snapshot:
        raise CollectionError("未找到售后工单内容区域，页面结构可能已变化。")
    count = snapshot.get("work_order_count")
    if not isinstance(count, int) or count < 0:
        raise CollectionError("未识别到“查询结果”中的工单数量，页面结构可能已变化。")
    return count


class WorkOrderCollector(Collector):
    def __init__(self, *args, screenshot_dir: Path, start_date=None, end_date=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.screenshot_dir = Path(screenshot_dir)
        today = date.today()
        self.start_date = self._date(start_date) if start_date else today.replace(day=1)
        self.end_date = self._date(end_date) if end_date else today
        if self.start_date > self.end_date:
            raise CollectionError("售后工单截图的开始日期不能晚于结束日期。")

    @staticmethod
    def _date(value):
        try:
            return value if isinstance(value, date) else date.fromisoformat(str(value))
        except ValueError as exc:
            raise CollectionError(f"售后工单截图日期无效：{value}") from exc

    def check_login(self):
        self.check()
        if "/login" in urlparse(self.page.url).path or self.page.locator("#usernameId:visible").count():
            raise WorkOrderLoginRequired("登录已失效，请在店铺管理中重新登录。")

    def _capture_visible_region(self, path):
        root = self.page.locator(WORK_ORDER_SELECTOR).first
        root.scroll_into_view_if_needed(timeout=5000)
        self._dismiss_screenshot_popups()
        box = self.page.evaluate(WORK_ORDER_CAPTURE_BOX_JS)
        viewport = self.page.viewport_size or {"width": 1440, "height": 1000}
        if not box or box.get("child_count", 0) < 4:
            raise CollectionError("售后工单内容区域未找到前四个区块，无法截图。")
        left, top = max(0, box["x"]), max(0, box["y"])
        right = min(float(viewport["width"]), box["x"] + box["width"])
        bottom = min(float(viewport["height"]), box["y"] + box["height"])
        if right <= left or bottom <= top:
            raise CollectionError("售后工单内容区域不在当前可见窗口内，无法截图。")
        blocked = self.page.evaluate(r"""box => {
            // Restrict this check to actual dialogs and the known PDD message box.
            // Generic fixed/absolute elements include normal navigation and caused false failures.
            const overlays = document.querySelectorAll(
                '#umd-msgbox-handler, [role="dialog"], [aria-modal="true"], ' +
                '[data-testid="beast-core-modal"], [data-testid="beast-core-modal-mask"]');
            return [...overlays].some(el => {
                const style = getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' ||
                    Number(style.opacity) === 0 || !el.getClientRects().length) return false;
                const r = el.getBoundingClientRect();
                return r.width > 20 && r.height > 20 &&
                    r.right > box.left && r.left < box.right &&
                    r.bottom > box.top && r.top < box.bottom;
            });
        }""", dict(left=left, top=top, right=right, bottom=bottom))
        if blocked:
            raise CollectionError("截图区域仍有弹窗或消息浮层遮挡，请关闭后重试。")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), clip={"x": left, "y": top,
            "width": right - left, "height": bottom - top}, timeout=10000)

    def _dismiss_screenshot_popups(self):
        """Dismiss overlays through normal UI actions, never hide page content."""
        message_box = self.page.locator('#umd-msgbox-handler:visible').first
        if message_box.count():
            close = message_box.locator(
                '[class*="ImportantList_msgbox-header__"] '
                'i[class*="ImportantList_close__"]').first
            try:
                close.click(timeout=2000)
                message_box.wait_for(state='hidden', timeout=3000)
            except PlaywrightTimeout as exc:
                raise CollectionError('新消息弹窗关闭失败，请点击右上角 X 后重试截图。') from exc
        self.page.mouse.move(0, 0)
        self.page.keyboard.press('Escape')
        for _ in range(3):
            self.check_login()
            close = self.page.locator(
                '[role="dialog"] [aria-label="关闭"]:visible, '
                '[role="dialog"] [aria-label="Close"]:visible, '
                '[data-testid="beast-core-modal-close"]:visible, '
                '[data-testid="beast-core-modal-close-icon"]:visible, '
                '[role="dialog"] button:visible').filter(
                    has_not=self.page.locator('input'))
            clicked = False
            for index in range(close.count()):
                button = close.nth(index)
                label = button.get_attribute('aria-label') or button.inner_text().strip()
                testid = button.get_attribute('data-testid') or ''
                if label not in ('关闭', 'Close', '×', '取消') and 'modal-close' not in testid:
                    continue
                try:
                    button.click(timeout=700)
                    clicked = True
                    break
                except PlaywrightTimeout:
                    continue
            if not clicked:
                break
            self.page.wait_for_timeout(200)
        # Clicking a non-interactive heading dismisses the header message dropdown.
        title = self.page.locator(
            WORK_ORDER_SELECTOR + ' [class*="BeastBlockTitle___title___"]'
        ).filter(has_text='工单查询').first
        if title.count():
            try:
                title.click(timeout=1000)
            except PlaywrightTimeout:
                pass
        self.page.mouse.move(0, 0)
        self.page.wait_for_timeout(400)

    def _calendar_months(self, picker):
        return picker.evaluate(r"""root => {
            const heads = [...root.querySelectorAll('[data-testid="beast-core-monthRangePicker-year-header"]')];
            return heads.map(head => {
                const year = (head.querySelector('[data-testid="beast-core-select-htmlInput"]')?.value || '').match(/\d{4}/)?.[0];
                const month = (head.querySelector('[class*="RPR_dateText_"]')?.textContent || '').match(/\d{1,2}/)?.[0];
                return year && month ? Number(year) * 100 + Number(month) : null;
            });
        }""")

    def _pick_calendar_day(self, picker, target):
        wanted = target.year * 100 + target.month
        for _ in range(30):
            months = self._calendar_months(picker)
            if wanted in months:
                index = months.index(wanted)
                table = picker.locator('[data-testid="beast-core-rangePicker-table"]').nth(index)
                day = table.locator(
                    f'td[role="date-cell"]:not([class*="disabled"]):not([class*="outOfMonth"]) '
                    f'div[title="{target.day}"]').first
                if not day.count():
                    raise CollectionError(f"日期选择器不能选择 {target.isoformat()}。")
                day.click(timeout=5000)
                return
            present = [month for month in months if month is not None]
            if not present:
                raise CollectionError("无法识别售后工单日期选择器显示的月份。")
            headers = picker.locator('[data-testid="beast-core-monthRangePicker-year-header"]')
            if wanted < min(present):
                headers.first.locator('[data-testid="beast-core-icon-left"]').first.click(timeout=5000)
            else:
                headers.last.locator('[data-testid="beast-core-icon-right"]').last.click(timeout=5000)
            self.wait(0.1)
        raise CollectionError(f"日期选择器无法定位到 {target.isoformat()}。")

    def select_date_range_and_query(self):
        """Use the supplied Beast Core range picker, then submit the work-order filters."""
        root = self.page.locator(WORK_ORDER_SELECTOR).first
        label = root.locator('[data-testid="beast-core-grid-col-wrapper"][label="创建时间"]:visible').first
        row = label.locator("xpath=..")
        range_box = row.locator('[data-testid="beast-core-rangePicker-input"]').first
        try:
            range_box.wait_for(state="visible", timeout=10000)
            range_box.click(timeout=5000)
            picker = self.page.locator(
                '[data-testid="beast-core-rangePicker-dropdown-contentRoot"]:visible').first
            picker.wait_for(state="visible", timeout=5000)
            self._pick_calendar_day(picker, self.start_date)
            self._pick_calendar_day(picker, self.end_date)
            picker.get_by_role("button", name="确认", exact=True).click(timeout=5000)
        except PlaywrightTimeout as exc:
            raise CollectionError("售后工单创建时间选择器操作失败，页面结构可能已变化。") from exc

        expected = f"{self.start_date.isoformat()} ~ {self.end_date.isoformat()}"
        value = range_box.locator('[data-testid="beast-core-rangePicker-htmlInput"]').input_value().strip()
        if value.replace("  ", " ") != expected:
            raise CollectionError(f"售后工单日期范围未正确设置，页面当前显示：{value or '空'}。")
        query = root.locator('[class*="Grid_filterWrapper_"] button[type="submit"]').filter(
            has_text="查询").first
        if not query.count():
            raise CollectionError("未找到售后工单“查询”按钮，页面结构可能已变化。")
        self.log(f"已选择创建时间 {expected}，正在查询……")
        pending = set()
        observed = set()
        last_finished = [0.0]

        def started(request):
            parsed = urlparse(request.url)
            if request.resource_type in ("xhr", "fetch") and parsed.hostname == "mms.pinduoduo.com":
                pending.add(request)
                observed.add(request)

        def finished(request):
            if request in pending:
                pending.discard(request)
                last_finished[0] = time.monotonic()

        self.page.on("request", started)
        self.page.on("requestfinished", finished)
        self.page.on("requestfailed", finished)
        try:
            query.click(timeout=5000)
            started_at = time.monotonic()
            deadline = started_at + 20
            while time.monotonic() < deadline:
                self.check()
                self.page.wait_for_timeout(100)
                try:
                    parse_work_order_count(self.page.evaluate(WORK_ORDER_SNAPSHOT_JS))
                    count_ready = True
                except CollectionError:
                    count_ready = False
                now = time.monotonic()
                requests_settled = bool(observed) and not pending and now - last_finished[0] >= 0.4
                # Routed offline pages have no XHR; a stable visible count is sufficient there.
                no_request_fallback = not observed and now - started_at >= 1.5
                if count_ready and (requests_settled or no_request_fallback):
                    return
            if pending:
                raise CollectionError("售后工单查询请求仍未结束，请检查网络或页面提示后重试。")
            raise CollectionError("查询后未读取到“共查询到 N 个工单”，页面结构可能已变化。")
        finally:
            self.page.remove_listener("request", started)
            self.page.remove_listener("requestfinished", finished)
            self.page.remove_listener("requestfailed", finished)

    def collect(self):
        self.check_login()
        try:
            self.page.locator(WORK_ORDER_SELECTOR).first.wait_for(state="visible", timeout=15000)
            self.page.locator('[class*="listTable_wrap__"], [data-testid="beast-core-pagination"]').first.wait_for(
                state="visible", timeout=15000)
        except PlaywrightTimeout as exc:
            self.check_login()
            raise CollectionError("页面未显示售后工单列表，页面结构可能已变化。") from exc

        shop = self.read_shop()
        self.select_date_range_and_query()
        self.page.locator(WORK_ORDER_SELECTOR).first.scroll_into_view_if_needed(timeout=5000)
        snapshot = self.page.evaluate(WORK_ORDER_SNAPSHOT_JS)
        work_order_count = parse_work_order_count(snapshot)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        shop_folder = hashlib.sha256(self.shop_key.encode("utf-8")).hexdigest()[:24]
        screenshot_path = self.screenshot_dir / shop_folder / f"售后工单_{stamp}.png"
        self._capture_visible_region(screenshot_path)

        self.db.bind_shop(shop)
        self.db.set_login_status(self.shop_key, "verified")
        self.db.save_work_order_snapshot(self.shop_key, work_order_count, self.page.url,
            str(screenshot_path.resolve()),
            self.start_date.isoformat(), self.end_date.isoformat())
        self.progress(1, work_order_count)
        return CollectionResult("completed", 1, work_order_count,
            f"截图完成：{self.start_date.isoformat()} 至 {self.end_date.isoformat()}，"
            f"查询到 {work_order_count} 个工单并已保存内部前四个区块的可见区域。")
