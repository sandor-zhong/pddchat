"""Collect the one-day customer-service overview for one shop."""
import json
import re
import time
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .models import CollectionError
from .scraper import Collector, CollectionResult


CUSTOMER_SERVICE_URL = "https://mms.pinduoduo.com/mms-chat/overview/service"

SERVICE_DATA_SNAPSHOT_JS = r"""() => {
    const text = el => (el?.innerText ?? el?.textContent ?? '').trim();
    const visible = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
    const title = [...document.querySelectorAll('[class*="BeastBlockTitle"]')]
        .find(el => visible(el) && text(el) === '数据总览');
    const titleBlock = title?.closest('[class*="BeastBlockTitle___wrapper"]') ||
        title?.parentElement?.parentElement;
    if (!titleBlock) return null;
    const dateText = text(titleBlock.querySelector('[class*="BeastBlockTitle___subContent"]'));
    const periodButton = [...titleBlock.querySelectorAll('button')].find(el => text(el) === '近1天');
    const scoreLabel = [...document.querySelectorAll('[class*="statistic-panel-label"]')]
        .find(el => visible(el) && text(el).replace(/\s/g, '') === '消费者服务体验分');
    const scorePanel = scoreLabel?.closest('[class*="statistic-panel-default"]') || scoreLabel?.parentElement;
    const scoreNode = scorePanel?.querySelector('[class*="statistic-panel-value___"]') ||
        scorePanel?.querySelector('[class*="statistic-panel-value"]');
    const score = text(scoreNode);
    const cards = [...document.querySelectorAll('.introduce-card')].filter(visible).map(card => ({
        label: text(card.querySelector('.card-header-title')).replace(/\s/g, ''),
        value: text(card.querySelector('.introduce-card-body')),
    }));
    return {date_text: dateText, score, cards,
        period_button_class: periodButton?.className || '',
        period_button_pressed: periodButton?.getAttribute('aria-pressed') || ''};
}"""

SERVICE_DATA_OBSERVE_JS = r"""() => {
    window.__pddServiceDataObserver?.disconnect();
    window.__pddServiceDataRevision = 0;
    const relevant = node => node instanceof Element && (
        node.matches('.body-left, .introduce-card, [data-testid="beast-core-tab-topWrapper"], [class*="BeastBlockTitle___subContent"]') ||
        node.closest('.body-left, .introduce-card, [data-testid="beast-core-tab-topWrapper"], [class*="BeastBlockTitle___subContent"]') ||
        node.querySelector('.body-left, .introduce-card, [data-testid="beast-core-tab-topWrapper"], [class*="BeastBlockTitle___subContent"]'));
    window.__pddServiceDataObserver = new MutationObserver(records => {
        if (records.some(change => relevant(change.target.nodeType === 1 ? change.target : change.target.parentElement) ||
            [...change.addedNodes, ...change.removedNodes].some(relevant))) window.__pddServiceDataRevision++;
    });
    window.__pddServiceDataObserver.observe(document.body, {subtree:true, childList:true, characterData:true, attributes:true});
}"""


class CustomerServiceLoginRequired(CollectionError):
    pass


def _card_value(cards, label):
    normalized = label.replace("(", "（").replace(")", "）")
    for card in cards:
        current = card["label"].replace("(", "（").replace(")", "）")
        if current == normalized:
            return card["value"].strip()
    raise CollectionError(f"客服数据页面缺少指标：{label}。本次查询未保存。")


def parse_service_data(snapshot):
    if not snapshot:
        raise CollectionError("未找到客服数据总览，本次查询未保存。")
    date_match = re.search(r"统计时间\s*[：:]\s*(\d{4}-\d{2}-\d{2})", snapshot["date_text"])
    if not date_match:
        raise CollectionError("未识别到客服数据统计时间，本次查询未保存。")
    score_text = snapshot["score"].strip()
    score_match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*分?", score_text)
    if not score_match:
        raise CollectionError(f"无法识别消费者服务体验分：{score_text!r}")

    count_labels = ("咨询人数（人）", "需人工回复的咨询人数（人）", "人工接待人数（人）")
    counts = []
    for label in count_labels:
        raw = _card_value(snapshot["cards"], label)
        match = re.search(r"[\d,]+", raw)
        if not match:
            raise CollectionError(f"无法识别{label}：{raw!r}")
        counts.append(int(match.group().replace(",", "")))

    reply_raw = _card_value(snapshot["cards"], "3分钟人工回复率")
    reply_match = re.search(r"(?:\d+(?:\.\d+)?|--)%", reply_raw)
    if not reply_match:
        raise CollectionError(f"无法识别3分钟人工回复率：{reply_raw!r}")
    response_raw = _card_value(snapshot["cards"], "平均人工响应时长")
    response_match = re.search(r"\d+\s*(?:秒|分钟|分)|--", response_raw)
    if not response_match:
        raise CollectionError(f"无法识别平均人工响应时长：{response_raw!r}")
    response_text = re.sub(r"\s+", "", response_match.group())
    seconds = int(re.search(r"\d+", response_text).group()) if response_text.endswith("秒") else None
    return {
        "stat_date": date_match.group(1), "period": "近1天",
        "experience_score": float(score_match.group(1)), "consultation_count": counts[0],
        "manual_required_count": counts[1], "manual_reception_count": counts[2],
        "reply_rate": reply_match.group(), "average_response_text": response_text,
        "average_response_seconds": seconds,
        "raw_metrics": snapshot,
    }


class CustomerServiceCollector(Collector):
    def dismiss_obstructions(self):
        """Close dismissible notices, while preserving login and verification dialogs."""
        total = 0
        for _ in range(3):
            dismissed = self.page.evaluate(r"""() => {
                const visible = el => !!el && el.isConnected && el.getClientRects().length > 0 &&
                    getComputedStyle(el).visibility !== 'hidden';
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
                    if (protectedText.test(content) || protectedControl) continue;

                    let close = panel.querySelector(
                        '[data-testid="beast-core-modal-icon-close"], [aria-label="关闭"], [title="关闭"], [data-testid$="-close"]');
                    if (!close && panel.id === 'umd-msgbox-handler')
                        close = panel.firstElementChild?.querySelector('i, button, [role="button"]');
                    if (!close) close = [...panel.querySelectorAll('button, [role="button"]')]
                        .find(el => /^(关闭|知道了|我知道了|稍后再说)$/.test((el.innerText || el.textContent || '').trim()));
                    if (!close) close = [...panel.querySelectorAll('i')]
                        .find(el => /close/i.test(el.className || ''));
                    if (close && visible(close)) {
                        close.click();
                        // The message drawer can remain mounted or animate after React's click.
                        // Hiding this known non-modal notice guarantees it no longer intercepts input.
                        if (panel.id === 'umd-msgbox-handler')
                            panel.style.setProperty('display', 'none', 'important');
                        count++;
                    }
                }
                return count;
            }""")
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
                getComputedStyle(el).visibility !== 'hidden';
            const nodes = [...new Set([
                ...document.querySelectorAll('#umd-msgbox-handler, [role="dialog"], [aria-modal="true"], [data-testid="beast-core-modal"], [data-testid="beast-core-modal-inner"]')
            ])].filter(visible);
            return nodes.slice(0, 3).map(el =>
                (el.innerText || el.textContent || '未知弹窗').replace(/\s+/g, ' ').trim().slice(0, 60));
        }""")

    def click_period_button(self, button):
        for _ in range(3):
            self.dismiss_obstructions()
            try:
                button.click(timeout=3000)
                return
            except PlaywrightTimeoutError:
                continue
        titles = self.visible_dialog_titles()
        detail = f"，当前可见弹窗：{'；'.join(titles)}" if titles else ""
        raise CollectionError(f"“近1天”按钮仍被页面浮层遮挡{detail}。请关闭后重试。")

    def check_login(self):
        self.check()
        if "/login" in urlparse(self.page.url).path or self.page.locator("#usernameId:visible").count():
            raise CustomerServiceLoginRequired("登录已失效，请在店铺管理中重新登录。")

    def snapshot(self):
        return self.page.evaluate(SERVICE_DATA_SNAPSHOT_JS)

    def period_button(self):
        title = self.page.get_by_text("数据总览", exact=True).first
        title.wait_for(state="visible", timeout=15000)
        block = title.locator("xpath=ancestor::div[.//button[normalize-space(.)='近1天']][1]")
        button = block.get_by_role("button", name="近1天", exact=True)
        if not button.count():
            raise CollectionError("未找到客服数据的“近1天”按钮，页面组件可能已变化。")
        return button.first

    def refreshed(self, action):
        pending, succeeded, failed = set(), set(), set()

        def started(request):
            parsed = urlparse(request.url)
            if (request.resource_type in ("xhr", "fetch") and parsed.hostname == "mms.pinduoduo.com"
                    and re.search(r"chat|service|overview", parsed.path, re.I)):
                pending.add(request)

        def finished(request):
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
                (succeeded if ok else failed).add(request)

        def aborted(request):
            if request in pending:
                pending.discard(request)
                failed.add(request)

        self.page.on("request", started)
        self.page.on("requestfinished", finished)
        self.page.on("requestfailed", aborted)
        self.page.evaluate(SERVICE_DATA_OBSERVE_JS)
        try:
            action()
            deadline = time.monotonic() + self.timeout
            last, stable_at = None, time.monotonic()
            while time.monotonic() < deadline:
                self.check_login()
                snapshot = self.snapshot()
                signature = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
                if signature != last or pending:
                    last, stable_at = signature, time.monotonic()
                if failed and not pending and not succeeded:
                    raise CollectionError("客服数据查询请求失败，本次查询未保存，请查看网站提示后重试。")
                observed = bool(succeeded) or self.page.evaluate("() => (window.__pddServiceDataRevision || 0) > 0")
                if snapshot and observed and not pending and time.monotonic() - stable_at >= 1:
                    return snapshot
                self.wait(.15)
            raise CollectionError("点击“近1天”后未能确认客服数据刷新完成，请检查网络或页面提示后重试。")
        finally:
            self.page.remove_listener("request", started)
            self.page.remove_listener("requestfinished", finished)
            self.page.remove_listener("requestfailed", aborted)
            if not self.page.is_closed():
                self.page.evaluate("() => window.__pddServiceDataObserver?.disconnect()")

    def collect(self):
        self.check_login()
        self.dismiss_obstructions()
        shop = self.read_shop()
        self.log(f"店铺：{shop.name} / {shop.account}；正在选择“近1天”……")
        button = self.period_button()
        snapshot = self.refreshed(lambda: self.click_period_button(button))
        record = parse_service_data(snapshot)
        current_shop = self.read_shop()
        if (current_shop.name, current_shop.account) != (shop.name, shop.account):
            raise CollectionError("查询客服数据期间店铺账号发生变化，本次查询未保存。")
        self.db.bind_shop(shop)
        self.db.set_login_status(self.shop_key, "verified")
        self.db.save_customer_service(self.shop_key, record, self.page.url)
        self.progress(1, 1)
        message = f"查询完成：统计时间 {record['stat_date']}，消费者服务体验分 {record['experience_score']:g}。"
        return CollectionResult("completed", 1, 1, message)
