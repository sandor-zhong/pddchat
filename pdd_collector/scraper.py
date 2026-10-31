"""DOM-only collection; selectors are based on the HTML supplied with the project."""
import json
import re
import time
from dataclasses import dataclass
from threading import Event
from urllib.parse import urlparse

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from .database import Database
from .models import Cancelled, CollectionError, Shop, parse_rows

TARGET_URL = "https://mms.pinduoduo.com/aftersales/customer_complain_appeal?from=list"
LIST_API_PATH = "/api/colombo/tuju/appealList"

# Use semantic/test-id selectors instead of the changing Beast Core version suffixes.
SNAPSHOT_JS = r"""() => {
    const txt = el => (el?.innerText ?? el?.textContent ?? '').trim();
    const visible = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
    const wrappers = [...document.querySelectorAll('[data-testid="beast-core-table"]')];
    const wrapper = wrappers.find(el => visible(el) && txt(el).includes('订单编号') && txt(el).includes('处理状态'));
    if (!wrapper) return null;
    const body = wrapper.querySelector('[data-testid="beast-core-table-middle-tbody"]') || wrapper.querySelector('tbody');
    const head = wrapper.querySelector('[data-testid="beast-core-table-middle-thead"]') || wrapper.querySelector('thead');
    const headers = [...(head?.querySelectorAll('th') || [])].map(txt);
    const goodsIndex = headers.findIndex(h => h.replace(/\s/g, '') === '商品信息');
    const issueIndex = headers.findIndex(h => h.replace(/\s/g, '') === '问题类型');
    const rows = [...(body?.querySelectorAll('tr[data-testid="beast-core-table-body-tr"]') || [])].map(row => {
        const cells = [...row.querySelectorAll(':scope > td')];
        const goods = cells[goodsIndex];
        const idMatch = txt(goods).match(/ID\s*[:：]\s*(\d+)/i);
        const name = goods?.querySelector('[class*="elli_outerWrapper"], [class*="GoodInfo_right"] > span');
        const issue = cells[issueIndex]?.querySelector(':scope > div > div');
        const detail = [...row.querySelectorAll('a[href]')].find(el => txt(el) === '查看详情');
        const href = detail?.getAttribute('href') || '';
        return {
            cells: cells.map(txt),
            goods: {id: idMatch?.[1] || '', name: txt(name), image: goods?.querySelector('img')?.src || ''},
            issue_type: txt(issue),
            detail_url: href && !href.startsWith('javascript:') && href !== '#' ? detail.href : ''
        };
    });
    const pgt = wrapper.querySelector('[data-testid="beast-core-pagination"]')
        || [...document.querySelectorAll('[data-testid="beast-core-pagination"]')].find(visible);
    const total = txt(pgt).match(/(?:共有|共|总共|总计)\s*([\d,]+)\s*条/);
    const bodyWrapper = wrapper.querySelector('[data-testid="beast-core-table-middle-body"]');
    const empty = !!body && !body.querySelector('tr') &&
        [...(bodyWrapper?.querySelectorAll('[class*="TB_emptyData"]') || [])]
            .some(el => visible(el) && txt(el) === '暂无申诉记录');
    const state = (pgt?.getAttribute('data-status') || '').match(/pagination-(\d+)-(\d+)$/);
    // Spin is also a permanent container (e.g. the header search box), not a loading signal.
    const loadingScope = wrapper.closest('[data-testid="beast-core-spin"]') || wrapper;
    const visiblyActive = el => {
        if (!visible(el)) return false;
        for (let parent=el; parent; parent=parent.parentElement) {
            const style = getComputedStyle(parent);
            if (style.opacity === '0' || style.visibility === 'hidden' || style.display === 'none') return false;
        }
        return true;
    };
    const busy = [...document.querySelectorAll('[aria-busy="true"]')]
        .some(el => (wrapper.contains(el) || el.contains(wrapper)) && visiblyActive(el)) ||
        [...loadingScope.querySelectorAll('[data-testid="beast-core-loading-spinner"], [data-testid="beast-core-spin-mask"]')]
            .some(visiblyActive);
    // Beast Core omits pagination for an explicit empty result. A blank/loading tbody alone is insufficient.
    return {headers, rows, total: total ? Number(total[1].replace(/,/g, '')) : empty ? 0 : null,
        empty,
        page_size: state ? Number(state[1]) : null, current_page: state ? Number(state[2]) : null,
        busy, pagination_text: txt(pgt)};
}"""

OBSERVE_JS = r"""() => {
    window.__pddCollectorObserver?.disconnect();
    window.__pddCollectorRevision = 0;
    const relevant = node => node instanceof Element && (
        node.matches('[data-testid="beast-core-table"], [data-testid="beast-core-pagination"]') ||
        node.closest('[data-testid="beast-core-table"], [data-testid="beast-core-pagination"]') ||
        node.querySelector('[data-testid="beast-core-table"], [data-testid="beast-core-pagination"]'));
    window.__pddCollectorObserver = new MutationObserver(records => {
        if (records.some(m => relevant(m.target.nodeType === 1 ? m.target : m.target.parentElement) ||
            [...m.addedNodes, ...m.removedNodes].some(relevant))) window.__pddCollectorRevision++;
    });
    window.__pddCollectorObserver.observe(document.body, {subtree:true, childList:true, characterData:true,
        attributes:true, attributeFilter:['data-status', 'aria-busy']});
}"""


@dataclass
class CollectionResult:
    status: str
    pages: int
    count: int
    message: str


class Collector:
    def __init__(self, page: Page, db: Database, shop_key: str, stop: Event,
                 log=lambda message: None, progress=lambda pages, count: None,
                 shop_callback=lambda shop, date_range: None, max_pages=0, timeout=30):
        self.page, self.db, self.shop_key, self.stop = page, db, shop_key, stop
        self.log, self.progress, self.shop_callback = log, progress, shop_callback
        self.max_pages, self.timeout = max_pages, timeout

    def check(self):
        if self.stop.is_set():
            raise Cancelled()
        if self.page.is_closed():
            raise CollectionError("浏览器页面已关闭。已入库的页面会保留，请重新打开浏览器。")

    def wait(self, seconds=0.1):
        self.check()
        self.page.wait_for_timeout(seconds * 1000)

    def dismiss_obstructions(self):
        """Close known non-authentication notices before interacting with page controls."""
        total = 0
        for _ in range(3):
            dismissed = self.page.evaluate(r"""() => {
                const visible = el => !!el && el.isConnected && el.getClientRects().length > 0 &&
                    getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
                const selectors = [
                    '#umd-msgbox-handler', '[role="dialog"]', '[aria-modal="true"]',
                    '[data-testid="beast-core-modal"]', '[data-testid="beast-core-modal-inner"]'
                ];
                const all = [...new Set(selectors.flatMap(selector =>
                    [...document.querySelectorAll(selector)]))].filter(visible);
                const panels = all.filter(panel => !all.some(other =>
                    other !== panel && other.contains(panel)));
                let count = 0;
                for (const panel of panels) {
                    const content = (panel.innerText || panel.textContent || '').replace(/\s+/g, ' ').trim();
                    const protectedText = /验证码|滑块|拼图|短信验证|手机验证|安全验证|扫码登录|账号登录|重新登录/;
                    const protectedControl = panel.querySelector(
                        '#slide-captcha-dialog, .captcha-wrapper__slider, #usernameId, input[type="password"], input[autocomplete="one-time-code"]');
                    if (panel.id !== 'umd-msgbox-handler' &&
                            (protectedText.test(content) || protectedControl)) continue;

                    let close = panel.querySelector(
                        '[class*="ImportantList_msgbox-header__"] i[class*="ImportantList_close__"], ' +
                        '[data-testid="beast-core-modal-icon-close"], [aria-label="关闭"], ' +
                        '[title="关闭"], [data-testid$="-close"]');
                    if (!close && panel.id === 'umd-msgbox-handler')
                        close = panel.firstElementChild?.querySelector('i[class*="close"], button, [role="button"]');
                    if (!close) close = [...panel.querySelectorAll('button, [role="button"]')]
                        .find(el => /^(关闭|知道了|我知道了|稍后再说)$/.test((el.innerText || el.textContent || '').trim()));
                    if (!close) close = [...panel.querySelectorAll('i')]
                        .find(el => /close/i.test(el.className || ''));
                    if (close && visible(close)) {
                        close.setAttribute('data-pdd-native-close', String(count));
                        count++;
                    }
                }
                return count;
            }""")
            for index in range(dismissed):
                target = self.page.locator(f'[data-pdd-native-close="{index}"]')
                if target.count() and target.is_visible():
                    target.click(timeout=3000)
            self.page.locator('[data-pdd-native-close]').evaluate_all(
                "els => els.forEach(el => el.removeAttribute('data-pdd-native-close'))")
            total += dismissed
            if not dismissed:
                break
            self.wait(.2)
        if total:
            self.log(f"已关闭 {total} 个页面提示弹窗。")
        return total

    def visible_dialog_titles(self):
        return self.page.evaluate(r"""() => {
            const visible = el => !!el && el.getClientRects().length > 0 &&
                getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
            return [...new Set(document.querySelectorAll(
                '#umd-msgbox-handler, [role="dialog"], [aria-modal="true"], ' +
                '[data-testid="beast-core-modal"], [data-testid="beast-core-modal-inner"]'))]
                .filter(visible).slice(0, 3).map(el =>
                    (el.innerText || el.textContent || '未知弹窗').replace(/\s+/g, ' ').trim().slice(0, 60));
        }""")

    def click_unobstructed(self, locator, description):
        for _ in range(3):
            self.dismiss_obstructions()
            try:
                locator.click(timeout=3000)
                return
            except PlaywrightTimeout:
                continue
        titles = self.visible_dialog_titles()
        detail = f"，当前可见弹窗：{'；'.join(titles)}" if titles else ""
        raise CollectionError(f"{description}仍被页面浮层遮挡{detail}，请关闭后重试。")

    def read_shop(self) -> Shop:
        info = self.page.locator('.mms-header__user-info:visible').filter(
            has=self.page.locator('.user-name-name .user-name-text')).first
        try:
            info.wait_for(state="visible", timeout=8000)
            name = info.locator('.user-name-name .user-name-text').first.inner_text().strip()
            account = info.locator('.user-name-id .user-name-text').first.inner_text().strip()
            avatar = info.locator('.avatar img').first
            avatar_url = avatar.evaluate("el => el.src") if avatar.count() else ""
        except PlaywrightTimeout as exc:
            raise CollectionError("未找到顶部店铺信息。请确认已登录，并展开右上角店铺信息后重试。") from exc
        if not name or not account:
            raise CollectionError("店铺名称或账号为空，无法确定数据归属。")
        return Shop(self.shop_key, name, account, avatar_url)

    def read_filters(self):
        date = self.page.locator('#createTime input').first
        order = self.page.locator('#orderSn input').first
        return (date.input_value().strip() if date.count() else "页面未显示时间范围",
                order.input_value().strip() if order.count() else "")

    def snapshot(self):
        return self.page.evaluate(SNAPSHOT_JS)

    @staticmethod
    def fingerprint(snapshot):
        return json.dumps(snapshot["rows"], ensure_ascii=False, sort_keys=True)

    def refreshed(self, action, previous=None):
        """Wait for a real refresh and settled table, not merely the old visible rows."""
        self.check()
        pending, succeeded, failed = set(), set(), set()

        def started(request):
            parsed = urlparse(request.url)
            if (request.resource_type in ("xhr", "fetch") and parsed.hostname == "mms.pinduoduo.com"
                    and parsed.path.rstrip('/') == LIST_API_PATH):
                pending.add(request)

        def finished(request):
            if request in pending:
                pending.discard(request)
                response = request.response()
                ok = bool(response and response.ok)
                if ok and "json" in response.headers.get("content-type", ""):
                    try:
                        body = response.json()
                        # Some backend errors use HTTP 200. Only inspect an explicit
                        # failure flag; do not invent endpoint-specific status codes.
                        if isinstance(body, dict) and body.get("success") is False:
                            ok = False
                    except (ValueError, TypeError):
                        pass
                (succeeded if ok else failed).add(request)

        def failure(request):
            if request in pending:
                pending.discard(request)
                failed.add(request)

        self.page.on("request", started)
        self.page.on("requestfinished", finished)
        self.page.on("requestfailed", failure)
        self.page.evaluate(OBSERVE_JS)
        try:
            action()
            deadline = time.monotonic() + self.timeout
            stable_at, last_signature = time.monotonic(), None
            while time.monotonic() < deadline:
                self.check()
                if failed and not pending:
                    raise CollectionError(f"申诉列表接口 {LIST_API_PATH} 请求失败，本页未入库，请查看网站提示后重试。")
                if not self.page.locator('#status').is_visible():
                    raise CollectionError("登录已失效或页面已离开申诉列表，请重新登录后采集。")
                snapshot = self.snapshot()
                revision = self.page.evaluate("() => window.__pddCollectorRevision || 0")
                if snapshot:
                    signature = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
                    if signature != last_signature or pending or snapshot["busy"]:
                        stable_at, last_signature = time.monotonic(), signature
                    observed = revision > 0 or bool(succeeded)
                    changed = previous is None or self.fingerprint(snapshot) != self.fingerprint(previous)
                    valid_empty = bool(snapshot["rows"]) or snapshot["total"] == 0
                    if observed and changed and valid_empty and not pending and not snapshot["busy"] and time.monotonic() - stable_at >= 1:
                        if failed:
                            raise CollectionError("查询或翻页期间有请求失败，本页未入库，请重试。")
                        return snapshot
                self.wait()
            raise CollectionError("查询/翻页后未能确认列表刷新完成（或翻页返回了相同数据）。请检查网络、登录状态及页面提示后重试。")
        finally:
            self.page.remove_listener("request", started)
            self.page.remove_listener("requestfinished", finished)
            self.page.remove_listener("requestfailed", failure)
            if not self.page.is_closed():
                self.page.evaluate("() => window.__pddCollectorObserver?.disconnect()")

    def select_passed(self):
        self.check()
        field = self.page.locator('#status')
        field.wait_for(state="visible", timeout=15000)
        field.locator('[data-testid="beast-core-select-header"]').click()
        options = [
            self.page.get_by_role("option", name="申诉通过", exact=True),
            self.page.locator('[data-testid*="select-option"]:visible').filter(has_text=re.compile(r"^申诉通过$")),
            self.page.locator('[role="listbox"]:visible').get_by_text("申诉通过", exact=True),
            # Beast Core versions without accessible option roles still expose a popup.
            self.page.locator('[class*="ST_dropdown"]:visible, [class*="ST_selectDropdown"]:visible').get_by_text("申诉通过", exact=True),
        ]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.check()
            for locator in options:
                for i in range(locator.count()):
                    item = locator.nth(i)
                    if item.is_visible():
                        item.click()
                        selected = field.locator('[data-testid="beast-core-select-htmlInput"]')
                        if selected.input_value() != "申诉通过":
                            raise CollectionError("处理状态未成功切换为“申诉通过”。")
                        return
            self.wait()
        raise CollectionError("找不到“申诉通过”下拉选项；页面组件可能已变化。")

    def next_button(self, current_page):
        pagination = self.page.locator('[data-testid="beast-core-pagination"]:visible').first
        if not pagination.count():
            return None
        candidates = [
            pagination.locator('[data-testid*="pagination-next"], [data-testid*="pagination"][data-testid*="next"], [class*="PGT_next"]'),
            pagination.get_by_role("button", name=re.compile(r"下一页|Next", re.I)),
            pagination.locator('[title="下一页"], [aria-label="下一页"], [title="Next Page"]'),
            pagination.get_by_text("下一页", exact=True),
        ]
        for candidate in candidates:
            for i in range(candidate.count()):
                item = candidate.nth(i)
                if not item.is_visible():
                    continue
                disabled = item.evaluate(r"""el => {
                    for (let n=el; n && n.tagName !== 'UL'; n=n.parentElement) {
                        if (n.disabled || n.getAttribute('aria-disabled') === 'true' ||
                            n.getAttribute('data-disabled') === 'true' || /disabled/i.test(n.className || '')) return true;
                    } return false;
                }""")
                if not disabled:
                    return item
        # Numbered pagination works even when a component changes its arrow markup.
        numbered = pagination.get_by_text(str(current_page + 1), exact=True)
        if numbered.count() == 1 and numbered.is_visible():
            return numbered
        return None

    def collect(self) -> CollectionResult:
        self.check()
        self.page.locator('#status').wait_for(state="visible", timeout=15000)
        shop = self.read_shop()
        filters = self.read_filters()
        if filters[1]:
            raise CollectionError("页面订单编号筛选不为空，请清空后重新开始，以免遗漏其他订单。")
        self.shop_callback(shop, filters[0])
        self.log(f"店铺：{shop.name} / {shop.account}；创建时间：{filters[0]}")
        self.select_passed()
        run_id = self.db.start_run(shop, filters[0], self.page.url)
        pages = count = 0
        try:
            self.log("已选择“申诉通过”，正在查询……")
            snapshot = self.refreshed(lambda: self.page.get_by_role("button", name="查询", exact=True).click())
            expected_total = snapshot["total"]
            if expected_total is None:
                raise CollectionError("未识别到列表总条数，无法确认完整性；本次未写入数据。")
            seen = set()
            while True:
                self.check()
                if snapshot["total"] != expected_total:
                    raise CollectionError("翻页过程中总条数发生变化，请重新采集以确保完整。")
                if self.read_filters() != filters:
                    raise CollectionError("采集期间筛选条件被修改，已停止。")
                if self.page.locator('#status [data-testid="beast-core-select-htmlInput"]').input_value() != "申诉通过":
                    raise CollectionError("采集期间处理状态被修改，已停止。")
                current_shop = self.read_shop()
                if (current_shop.name, current_shop.account) != (shop.name, shop.account):
                    raise CollectionError("采集期间店铺账号发生变化，已停止，避免数据混入。")
                page_number = snapshot["current_page"]
                if page_number is not None and page_number != pages + 1:
                    raise CollectionError(f"分页不连续：预期第 {pages + 1} 页，实际第 {page_number} 页。")
                records = parse_rows(snapshot)
                identities = [r.identity for r in records]
                if len(set(identities)) != len(identities) or any(key in seen for key in identities):
                    raise CollectionError("列表出现重复业务键，可能翻页未成功或记录需要申诉 ID 区分；已停止，避免误报完整。")
                if count + len(records) > expected_total:
                    raise CollectionError("采集记录数超过页面总条数，已停止。")
                if not records and expected_total > 0:
                    raise CollectionError("列表为空但总条数非零，已停止。")
                pages += 1
                count += len(records)
                self.db.save_page(shop.key, run_id, records, pages, count)
                seen.update(identities)
                self.progress(pages, count)
                self.log(f"第 {pages} 页已入库：{len(records)} 条；本次累计 {count} / {expected_total} 条。")
                if count == expected_total:
                    message = "采集完成：当前筛选条件下暂无申诉记录，共 0 条。" if count == 0 else f"采集完成：共 {pages} 页、{count} 条（重复采集会更新已有记录）。"
                    self.db.finish_run(run_id, "completed", message)
                    return CollectionResult("completed", pages, count, message)
                if self.max_pages and pages >= self.max_pages:
                    message = f"达到页数上限，仅采集 {pages} 页、{count} / {expected_total} 条。"
                    self.db.finish_run(run_id, "partial", message)
                    return CollectionResult("partial", pages, count, message)
                next_page = self.next_button(page_number or pages)
                if next_page is None:
                    raise CollectionError(f"还有未采集记录（{count}/{expected_total}），但未找到可用的下一页。")
                self.log("正在翻到下一页……")
                snapshot = self.refreshed(next_page.click, previous=snapshot)
        except Cancelled:
            self.db.finish_run(run_id, "cancelled", f"用户停止，已保留 {pages} 页、{count} 条。")
            raise
        except Exception as exc:
            self.db.finish_run(run_id, "failed", str(exc))
            raise
