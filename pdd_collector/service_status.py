"""Read-only customer-service snapshots. Never click assignment or status controls."""
import base64
import hashlib
import json
import re
import time
from urllib.parse import urlparse

from .models import CollectionError
from .scraper import Collector, CollectionResult
from .violation import ViolationCollector, VIOLATION_URL

SERVICE_STATUS_URL = 'https://mms.pinduoduo.com/chat-service/status?msfrom=mms_sidenav'

# Exact image hashes from the supplied page, independent of misleading parent CSS names.
ICON_STATES = {
    '24feb4e7e642e831c3bca44dd2ee87517668e6eecaef2833d39d1dfb7a3e1921': ('pc', True),
    '87696b1601aea794f9249752b8ab47c5bc1f0cefdea577370e9527917bd5ad35': ('pc', False),
    '1012b96486a11905e7a9d944a44ac3a6f1d3e483f9b754e74e13beb6ecbf72e4': ('mobile', False),
}
# The mobile-online PNG supplied separately by the user: match its compressed image
# prefix as well as PNG header; CSS `offlineStatus` is not evidence of being offline.
MOBILE_ONLINE_PREFIX = ('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADwAAAA8CAYAAAA6/NlyAAAAAXNSR0IArs4c6QAABLtJREFUaAXtWn1oU1cUP22T9CM2EU1Kp3a0IBOTWmVObKlzwqD+4fbHYN2KYwgTQZRVHBYcjK7gGPvWuaFTcGyIUifbGN0GLa6zm5Vi5zeNtBN0rR9tmqY0/Upe+rF3HrvZy8vL7XvJ+8gf98Hj3nvOueec3zn3vrx78gDYxSLAIsAiwCKQuRHIornW09NjC4VCp8Ph8Escx1lpslrzbDZbNC8v')

STATUS_SNAPSHOT_JS = r"""() => {
    const visible = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
    const root = [...document.querySelectorAll('.customer-service-status-body')].find(visible);
    if (!root) return null;
    const table = root.querySelector('[data-testid="beast-core-table"]');
    if (!table) return null;
    const text = el => (el?.innerText || '').trim();
    const totalMatch = text(root.querySelector('[data-testid="beast-core-pagination"]')).match(/共(?:有)?\s*([\d,]+)\s*条/);
    const rows = [...table.querySelectorAll('[data-testid="beast-core-table-middle-tbody"] > tr')].filter(visible).map(tr => {
        const cells = [...tr.querySelectorAll('td')];
        return {cells:cells.map(text), icons:[...(cells[1]?.querySelectorAll('img') || [])].map(img => img.getAttribute('src') || ''),
            statusClass:cells[1]?.querySelector('.loginStatus, .offlineStatus')?.className || ''};
    });
    const empty = [...table.querySelectorAll('[class*="emptyData"], [data-testid*="empty"]')]
        .some(el => visible(el) && /暂无|没有|无数据/.test(text(el)));
    const busy = [...table.querySelectorAll('[aria-busy="true"], [data-testid="beast-core-spin-spinner"], [class*="loadingIcon"], [class*="loadingMask"]')].some(visible);
    const pagination = root.querySelector('[data-testid="beast-core-pagination"]');
    const pageMatch = (pagination?.getAttribute('data-status') || '').match(/pagination-\d+-(\d+)$/);
    return {headers:[...table.querySelectorAll('[data-testid="beast-core-table-middle-thead"] th')].map(text),
        rows,total:totalMatch ? Number(totalMatch[1].replaceAll(',','')) : empty && !rows.length ? 0 : null,
        empty,busy,current_page:pageMatch ? Number(pageMatch[1]) : null};
}"""


class ServiceLoginRequired(CollectionError):
    pass


def decode_status(icons):
    states, hashes = {}, []
    for src in icons:
        try:
            digest = hashlib.sha256(base64.b64decode(src.split(',', 1)[1], validate=True)).hexdigest() if src.startswith('data:image/png;base64,') else ''
        except (ValueError, IndexError):
            digest = ''
        hashes.append(digest or src)
        known = ICON_STATES.get(digest)
        if src.startswith(MOBILE_ONLINE_PREFIX):
            known = ('mobile', True)
        if known:
            states[known[0]] = known[1]
    if len(icons) != 2 or len(states) != 2:
        return '未识别', hashes
    return { (True, True): '电脑、手机都在线', (True, False): '电脑在线',
             (False, True): '手机在线', (False, False): '电脑、手机都离线'}[(states['pc'], states['mobile'])], hashes


def parse_status_rows(snapshot):
    headers = [re.sub(r'\s+', '', h).replace('(', '（').replace(')', '）') for h in snapshot['headers']]
    required = ['客服账号', '状态', '3分钟人工回复率', '累计咨询人数（需人工回复）']
    if not all(name in headers for name in required):
        raise CollectionError('客服状态列表缺少所需列，未保存本次查询。')
    indices = [headers.index(name) for name in required]
    records = []
    for row in snapshot['rows']:
        if len(row['cells']) <= max(indices):
            raise CollectionError('客服状态列表行不完整，未保存本次查询。')
        account, _, rate, count = [row['cells'][i].strip() for i in indices]
        if not account or not rate or not count:
            raise CollectionError('客服账号或统计指标为空，未保存本次查询。')
        status, hashes = decode_status(row['icons'])
        records.append(dict(account=account, status=status, reply_rate=rate, consultation_count=count,
                            raw_status=dict(image_hashes=hashes, css_class=row['statusClass'])))
    return records


class ServiceStatusCollector(Collector):
    def check_login(self):
        self.check()
        if '/login' in urlparse(self.page.url).path or self.page.locator('#usernameId:visible').count():
            raise ServiceLoginRequired('登录已失效，请在店铺管理中重新登录。')

    def settled(self, previous_accounts=None):
        deadline = time.monotonic() + self.timeout
        last, stable_at = None, time.monotonic()
        while time.monotonic() < deadline:
            self.check_login()
            snap = self.page.evaluate(STATUS_SNAPSHOT_JS)
            signature = json.dumps(snap, ensure_ascii=False, sort_keys=True)
            if signature != last or (snap and snap['busy']):
                last, stable_at = signature, time.monotonic()
            if snap and not snap['busy'] and snap['total'] is not None and (snap['rows'] or snap['empty']):
                accounts = [r['cells'][0] if r['cells'] else '' for r in snap['rows']]
                if accounts != previous_accounts and time.monotonic() - stable_at >= 1:
                    return snap
            self.wait(.15)
        raise CollectionError('客服状态列表未加载完成，或未识别总条数/翻页结果；未保存本次查询。')

    def collect(self):
        snapshot = self.settled()
        shop = self.read_shop()
        self.db.bind_shop(shop)
        self.db.set_login_status(self.shop_key, 'verified')
        expected = snapshot['total']
        rows, seen, pages = [], set(), 0
        while True:
            self.check_login()
            if self.read_shop() != shop:
                raise CollectionError('查询期间店铺身份改变，未保存本次查询。')
            if snapshot['total'] != expected or snapshot['current_page'] not in (None, pages + 1):
                raise CollectionError('客服列表总数变化或分页不连续，请重新查询。')
            records = parse_status_rows(snapshot)
            for record in records:
                if record['account'] in seen:
                    raise CollectionError('客服账号重复，可能翻页未成功；未保存本次查询。')
                seen.add(record['account'])
            rows.extend(records)
            pages += 1
            self.progress(pages, len(rows))
            if len(rows) == expected:
                break
            if len(rows) > expected or not records:
                raise CollectionError('客服记录数与页面总数不符，未保存本次查询。')
            button = self.next_button(pages)
            if button is None:
                raise CollectionError('客服列表尚未采集完整，但未找到下一页；未保存本次查询。')
            previous = [r['cells'][0] for r in snapshot['rows']]
            button.click()
            snapshot = self.settled(previous)
        self.check_login()
        service_url = self.page.url
        self.log(f'客服状态读取完成：{len(rows)} 个账号；接着查询最近90天违规信息……')
        self.page.goto(VIOLATION_URL, wait_until='domcontentloaded', timeout=45000)
        violations = ViolationCollector(self.page, self.db, self.shop_key, self.stop,
            log=self.log, progress=lambda violation_pages, _count: self.progress(
                pages + violation_pages, len(rows)), timeout=self.timeout).collect(shop)
        self.check_login()
        self.db.save_operational_snapshot(self.shop_key, rows, service_url, violations.records,
                                          self.page.url, violations.date_range)
        unknown = sum(r['status'] == '未识别' for r in rows)
        message = f'查询完成，{len(rows)} 个客服账号、最近90天 {len(violations.records)} 条违规记录。'
        if unknown:
            message += f'其中 {unknown} 个账号图标未识别，请核对网页状态。'
        return CollectionResult('completed', pages + violations.pages, len(rows), message)
