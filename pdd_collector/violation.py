"""Collect the complete 90-day after-sales violation list for one shop."""
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from .models import CollectionError, Shop
from .scraper import Collector, OBSERVE_JS


VIOLATION_URL = "https://mms.pinduoduo.com/aftersales/aftersale_violation/list"


VIOLATION_SNAPSHOT_JS = r"""() => {
    const text = el => (el?.innerText ?? el?.textContent ?? '').trim();
    const visible = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
    const normalized = value => value.replace(/\s/g, '');
    const wrappers = [...document.querySelectorAll('[data-testid="beast-core-table"]')];
    const wrapper = wrappers.find(el => {
        if (!visible(el)) return false;
        const headers = [...el.querySelectorAll('th')].map(th => normalized(text(th)));
        return headers.includes('违规编号') && headers.includes('违规内容') && headers.includes('违规时间');
    });
    if (!wrapper) return null;
    const head = wrapper.querySelector('[data-testid="beast-core-table-middle-thead"]') || wrapper.querySelector('thead');
    const body = wrapper.querySelector('[data-testid="beast-core-table-middle-tbody"]') || wrapper.querySelector('tbody');
    const headers = [...(head?.querySelectorAll('th') || [])].map(text);
    const rows = [...(body?.querySelectorAll('tr[data-testid="beast-core-table-body-tr"], :scope > tr') || [])]
        .filter(visible).map(row => {
            const cells = [...row.querySelectorAll(':scope > td')];
            const detail = [...row.querySelectorAll('a')].find(el => text(el) === '查看详情');
            const href = detail?.getAttribute('href') || '';
            return {cells: cells.map(text),
                detail_url: href && href !== '#' && !href.startsWith('javascript:') ? detail.href : ''};
        });
    const pagination = wrapper.querySelector('[data-testid="beast-core-pagination"]') ||
        [...document.querySelectorAll('[data-testid="beast-core-pagination"]')].find(visible);
    const totalMatch = text(pagination).match(/(?:共有|共|总共|总计)\s*([\d,]+)\s*条/);
    const pageMatch = (pagination?.getAttribute('data-status') || '').match(/pagination-(\d+)-(\d+)$/);
    const bodyWrapper = wrapper.querySelector('[data-testid="beast-core-table-middle-body"]') || wrapper;
    const empty = !rows.length && [...bodyWrapper.querySelectorAll('[class*="emptyData"], [data-testid*="empty"]')]
        .some(el => visible(el) && /暂无违规记录|暂无数据|没有违规/.test(text(el)));
    const busy = [...wrapper.querySelectorAll('[aria-busy="true"], [data-testid="beast-core-loading-spinner"], [data-testid="beast-core-spin-mask"]')]
        .some(visible);
    return {headers, rows, empty, busy,
        total: totalMatch ? Number(totalMatch[1].replace(/,/g, '')) : empty ? 0 : null,
        page_size: pageMatch ? Number(pageMatch[1]) : null,
        current_page: pageMatch ? Number(pageMatch[2]) : null};
}"""


@dataclass(frozen=True)
class ViolationCollection:
    records: list[dict]
    pages: int
    date_range: str


class ViolationLoginRequired(CollectionError):
    pass


def parse_violation_rows(snapshot):
    headers = [re.sub(r"\s+", "", value) for value in snapshot["headers"]]
    required = ["违规编号", "违规内容", "处理措施", "处理状态", "相关订单", "违规时间", "操作"]
    if not all(name in headers for name in required):
        raise CollectionError(f"违规列表表头发生变化，无法安全采集：{snapshot['headers']}")
    indexes = {name: headers.index(name) for name in required}
    records = []
    for row in snapshot["rows"]:
        cells = row["cells"]
        if len(cells) != len(snapshot["headers"]):
            raise CollectionError("违规列表列数不一致，本次查询未保存。")
        values = {name: cells[index].strip() for name, index in indexes.items()}
        if not values["违规编号"] or not values["违规内容"] or not values["违规时间"]:
            raise CollectionError("违规编号、违规内容或违规时间为空，本次查询未保存。")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", values["违规时间"]):
            raise CollectionError(f"无法识别违规时间：{values['违规时间']!r}")
        records.append({
            "violation_sn": values["违规编号"],
            "content": values["违规内容"],
            "measure": values["处理措施"],
            "status": values["处理状态"],
            "order_sn": values["相关订单"],
            "violation_time": values["违规时间"],
            "operation": values["操作"],
            "detail_url": row.get("detail_url", ""),
            "raw_columns": dict(zip(snapshot["headers"], cells)),
        })
    return records


class ViolationCollector(Collector):
    def check_login(self):
        self.check()
        if "/login" in urlparse(self.page.url).path or self.page.locator("#usernameId:visible").count():
            raise ViolationLoginRequired("登录已失效，请在店铺管理中重新登录。")

    def time_field(self):
        label = self.page.locator('[data-testid="beast-core-grid-col-wrapper"][label="违规时间"]:visible').first
        label.wait_for(state="visible", timeout=15000)
        row = label.locator("xpath=..")
        field = row.locator('[data-testid="beast-core-select"]').first
        if not field.count():
            raise CollectionError("未找到违规时间快速选择框，页面组件可能已变化。")
        return field

    def select_90_days(self):
        field = self.time_field()
        selected = field.locator('[data-testid="beast-core-select-htmlInput"]').first
        if selected.input_value().strip() == "最近90天内":
            return
        self.click_unobstructed(
            field.locator('[data-testid="beast-core-select-header"]'), "违规时间筛选")
        options = [
            self.page.get_by_role("option", name="最近90天内", exact=True),
            self.page.locator('[data-testid*="select-option"]:visible').filter(has_text=re.compile(r"^最近90天内$")),
            self.page.locator('[role="listbox"]:visible').get_by_text("最近90天内", exact=True),
            self.page.locator('[class*="ST_dropdown"]:visible, [class*="ST_selectDropdown"]:visible').get_by_text("最近90天内", exact=True),
        ]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.check_login()
            for locator in options:
                for index in range(locator.count()):
                    option = locator.nth(index)
                    if option.is_visible():
                        option.click()
                        if selected.input_value().strip() != "最近90天内":
                            raise CollectionError("违规时间未成功切换为“最近90天内”。")
                        return
            self.wait(.1)
        raise CollectionError("找不到“最近90天内”下拉选项；页面组件可能已变化。")

    def date_range(self):
        field = self.time_field()
        selected = field.locator('[data-testid="beast-core-select-htmlInput"]').first.input_value().strip()
        row = field.locator("xpath=../..")
        ranges = row.locator('[data-testid="beast-core-rangePicker-htmlInput"]')
        exact = ranges.first.input_value().strip() if ranges.count() else ""
        return f"{selected}（{exact}）" if exact else selected

    def query_button(self):
        form = self.time_field().locator("xpath=ancestor::form[1]")
        button = form.get_by_role("button", name="查询", exact=True)
        if not button.count():
            raise CollectionError("未找到违规信息查询按钮，页面组件可能已变化。")
        return button.first

    def snapshot(self):
        return self.page.evaluate(VIOLATION_SNAPSHOT_JS)

    def settled_after(self, target, previous=None):
        self.check_login()
        pending, finished, failed = set(), set(), set()

        def started(request):
            parsed = urlparse(request.url)
            relevant_path = re.search(r"violation|punish|after.?sale", parsed.path, re.I)
            if (request.resource_type in ("xhr", "fetch") and parsed.hostname == "mms.pinduoduo.com"
                    and relevant_path):
                pending.add(request)

        def succeeded(request):
            if request in pending:
                pending.discard(request)
                response = request.response()
                ok = bool(response and response.ok)
                if ok and "json" in response.headers.get("content-type", ""):
                    try:
                        body = response.json()
                        if isinstance(body, dict) and body.get("success") is False:
                            ok = False
                    except (ValueError, TypeError):
                        pass
                (finished if ok else failed).add(request)

        def aborted(request):
            if request in pending:
                pending.discard(request)
                failed.add(request)

        self.page.on("request", started)
        self.page.on("requestfinished", succeeded)
        self.page.on("requestfailed", aborted)
        self.page.evaluate(OBSERVE_JS)
        try:
            self.click_unobstructed(target, "违规列表操作")
            deadline = time.monotonic() + self.timeout
            last, stable_at = None, time.monotonic()
            while time.monotonic() < deadline:
                self.check_login()
                snapshot = self.snapshot()
                signature = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
                if signature != last or (snapshot and snapshot["busy"]) or pending:
                    last, stable_at = signature, time.monotonic()
                if failed and not pending and not finished:
                    raise CollectionError("违规列表查询请求失败，本次查询未保存，请查看网站提示后重试。")
                if snapshot and not snapshot["busy"] and (snapshot["rows"] or snapshot["empty"]):
                    changed = previous is None or snapshot["rows"] != previous["rows"]
                    observed = bool(finished) or self.page.evaluate("() => (window.__pddCollectorRevision || 0) > 0")
                    # A query that is empty both before and after clicking may not cause React to
                    # rebuild the table. The explicit, stable empty state is authoritative on the
                    # initial query; pagination continues to require an observed refresh.
                    explicit_initial_empty = previous is None and snapshot["empty"] and snapshot["total"] == 0
                    if changed and (observed or explicit_initial_empty) and not pending and time.monotonic() - stable_at >= 1:
                        return snapshot
                self.wait(.15)
            raise CollectionError("违规列表查询/翻页后未能确认数据加载完成，请检查网络或页面提示后重试。")
        finally:
            self.page.remove_listener("request", started)
            self.page.remove_listener("requestfinished", succeeded)
            self.page.remove_listener("requestfailed", aborted)
            if not self.page.is_closed():
                self.page.evaluate("() => window.__pddCollectorObserver?.disconnect()")

    def collect(self, expected_shop: Shop) -> ViolationCollection:
        self.check_login()
        self.select_90_days()
        date_range = self.date_range()
        self.log(f"违规时间：{date_range}；正在查询违规信息……")
        snapshot = self.settled_after(self.query_button())
        records, seen, pages = [], set(), 0
        expected_total = snapshot["total"]
        while True:
            self.check_login()
            if self.time_field().locator('[data-testid="beast-core-select-htmlInput"]').first.input_value().strip() != "最近90天内":
                raise CollectionError("采集期间违规时间筛选被修改，已停止。")
            if self.date_range() != date_range:
                raise CollectionError("采集期间违规时间范围发生变化，已停止。")
            current_shop = self.read_shop()
            if (current_shop.name, current_shop.account) != (expected_shop.name, expected_shop.account):
                raise CollectionError("查询违规信息期间店铺账号发生变化，未保存本次查询。")
            if expected_total is not None and snapshot["total"] != expected_total:
                raise CollectionError("违规列表翻页过程中总条数发生变化，请重新查询。")
            page_number = snapshot["current_page"]
            if page_number is not None and page_number != pages + 1:
                raise CollectionError(f"违规列表分页不连续：预期第 {pages + 1} 页，实际第 {page_number} 页。")
            batch = parse_violation_rows(snapshot)
            identities = [record["violation_sn"] for record in batch]
            if len(identities) != len(set(identities)) or any(identity in seen for identity in identities):
                raise CollectionError("违规编号重复，可能翻页未成功；未保存本次查询。")
            records.extend(batch)
            seen.update(identities)
            pages += 1
            self.progress(pages, len(records))
            self.log(f"违规列表第 {pages} 页已读取：{len(batch)} 条；累计 {len(records)} 条。")
            if expected_total is not None and len(records) > expected_total:
                raise CollectionError("违规记录数超过页面总条数，未保存本次查询。")
            next_page = self.next_button(page_number or pages)
            if next_page is None:
                if expected_total is not None and len(records) != expected_total:
                    raise CollectionError(f"违规列表记录数与总数不符（{len(records)}/{expected_total}），未保存本次查询。")
                break
            previous = snapshot
            snapshot = self.settled_after(next_page, previous=previous)
        return ViolationCollection(records, pages, date_range)
