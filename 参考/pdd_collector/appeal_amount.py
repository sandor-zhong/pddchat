"""Collect the two 30-day appeal amount summaries for one shop."""
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlparse

from .models import CollectionError
from .scraper import Collector, CollectionResult, TARGET_URL


APPEAL_AMOUNT_URL = "https://mms.pinduoduo.com/orders/appeals?msfrom=mms_sidenav&activeKey=2"
NEGATIVE_APPEAL_AMOUNT_URL = TARGET_URL

AMOUNT_SNAPSHOT_JS = r"""label => {
    const text = el => (el?.innerText ?? el?.textContent ?? '').replace(/\s+/g, '').trim();
    const visible = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
    const labels = [...document.querySelectorAll('div,span')]
        .filter(el => visible(el) && text(el) === label);
    for (const node of labels) {
        let container = node;
        for (let depth=0; container && depth<7; depth++, container=container.parentElement) {
            const content = text(container);
            const suffix = content.slice(content.indexOf(label) + label.length);
            const match = suffix.match(/^([+-]?[\d,]+(?:\.\d+)?)元(?:$|[^\d])/);
            if (match) return {label, amount_text: match[1], container_text: content};
        }
    }
    return null;
}"""


class AppealAmountLoginRequired(CollectionError):
    pass


def parse_amount(snapshot, label):
    if not snapshot:
        raise CollectionError(f"未找到“{label}”，本次查询未保存。")
    raw = snapshot.get("amount_text", "").replace(",", "").strip()
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise CollectionError(f"无法识别“{label}”：{raw!r}") from exc
    if not value.is_finite() or value < 0:
        raise CollectionError(f"“{label}”金额无效：{raw!r}")
    cents = int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return cents, f"{cents // 100}.{cents % 100:02d}"


class AppealAmountCollector(Collector):
    def check_login(self):
        self.check()
        if "/login" in urlparse(self.page.url).path or self.page.locator("#usernameId:visible").count():
            raise AppealAmountLoginRequired("登录已失效，请在店铺管理中重新登录。")

    def read_amount(self, label):
        self.check_login()
        try:
            self.page.get_by_text(label, exact=True).first.wait_for(state="visible", timeout=15000)
        except Exception as exc:
            self.check_login()
            raise CollectionError(f"页面未显示“{label}”，页面结构可能已变化。本次查询未保存。") from exc
        snapshot = self.page.evaluate(AMOUNT_SNAPSHOT_JS, label)
        cents, text = parse_amount(snapshot, label)
        return {"cents": cents, "text": text, "raw": snapshot}

    def collect(self):
        self.check_login()
        first_shop = self.read_shop()
        self.log(f"店铺：{first_shop.name} / {first_shop.account}；正在读取近30日申诉成功金额……")
        appeal = self.read_amount("近30日申诉成功金额")
        appeal_url = self.page.url

        self.check()
        self.log("正在读取近30天负向申诉成功金额……")
        self.page.goto(NEGATIVE_APPEAL_AMOUNT_URL, wait_until="domcontentloaded", timeout=45000)
        negative = self.read_amount("近30天申诉成功金额")
        second_shop = self.read_shop()
        if (second_shop.name, second_shop.account) != (first_shop.name, first_shop.account):
            raise CollectionError("查询两项申诉金额期间店铺账号发生变化，本次查询未保存。")

        record = {
            "appeal_amount_cents": appeal["cents"], "appeal_amount_text": appeal["text"],
            "negative_amount_cents": negative["cents"], "negative_amount_text": negative["text"],
            "raw_values": {"appeal": appeal["raw"], "negative": negative["raw"]},
        }
        self.db.bind_shop(first_shop)
        self.db.set_login_status(self.shop_key, "verified")
        self.db.save_appeal_amount(self.shop_key, record, appeal_url, self.page.url)
        self.progress(2, 2)
        message = (f"查询完成：近30日申诉成功金额 {appeal['text']} 元，"
                   f"近30天负向申诉成功金额 {negative['text']} 元。")
        return CollectionResult("completed", 2, 2, message)
