"""包一层 db.py 查询 + state/profiles/ 目录扫描，给页面用的"店铺"视图。

页面代码只应该跟这个模块打交道，不要直接 import scheduler_core.discover_
shops 或者直接拼 state/profiles 路径——路径规则、"店铺"这个概念到底
由谁定义（profile 目录 vs 数据库历史记录 vs 本地停用名单）都收在这一处，
以后调整不用到处改。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .project_paths import PROJECT_PATHS

import db  # noqa: E402  (sys.path 由 project_paths 导入时已插入)
import browser_settings  # noqa: E402
import scheduler_core  # noqa: E402

DISABLED_SHOPS_PATH = (
    Path(__file__).resolve().parent.parent.parent / "state" / "disabled_shops.json"
)
IMPORTED_SHOPS_PATH = (
    Path(__file__).resolve().parent.parent.parent / "state" / "imported_shops.json"
)


@dataclass
class ShopSummary:
    """一家店铺在 GUI 里展示所需的全部信息，一次性查好，页面直接渲染。"""

    shop: str
    has_profile: bool  # state/profiles/<shop>/ 是否存在（登录态的必要非充分条件）
    disabled: bool
    schedule_status: str  # "done" / "failed" / "skipped"（并发名额不够、本轮没轮到开工）/ "not_started"
    schedule_detail: str | None  # failed 时的原因
    login_expired: bool  # 今天调度失败确实是因为登录失效（比目录判断更准确，见下）
    pending_remaining: int
    submitted_today: int
    failed_today: int
    skipped_today: int
    touched_today: int
    last_activity: str | None  # ISO 时间字符串，orders 表里没记录过时为 None
    max_orders: (
        int | None
    )  # 单次采集处理上限；None 表示未配置，维持默认只读第一页的行为
    group_name: str | None  # 用户自定义店铺组；None 表示未分组


def _load_disabled_shops() -> set[str]:
    if not DISABLED_SHOPS_PATH.is_file():
        return set()
    try:
        data = json.loads(DISABLED_SHOPS_PATH.read_text(encoding="utf-8"))
        return set(data.get("disabled", []))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_disabled_shops(shops: set[str]) -> None:
    DISABLED_SHOPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DISABLED_SHOPS_PATH.write_text(
        json.dumps({"disabled": sorted(shops)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_imported_shops() -> dict[str, dict]:
    if not IMPORTED_SHOPS_PATH.is_file():
        return {}
    try:
        data = json.loads(IMPORTED_SHOPS_PATH.read_text(encoding="utf-8"))
        shops = data.get("shops", {})
        return shops if isinstance(shops, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_imported_shops(shops: dict[str, dict]) -> None:
    IMPORTED_SHOPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    IMPORTED_SHOPS_PATH.write_text(
        json.dumps({"shops": shops}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def register_imported_shops(shops: list[str]) -> None:
    """Register Excel rows without storing their usernames or passwords."""
    saved = _load_imported_shops()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for shop in shops:
        previous = saved.get(shop, {})
        saved[shop] = {
            "status": previous.get("status", "pending"),
            "message": previous.get("message", "等待批量登录"),
            "updated_at": now,
            **({"managed_account": True} if previous.get("managed_account") else {}),
        }
    _save_imported_shops(saved)


def sync_account_shops(shops: list[str]) -> None:
    """Make account-list-backed registry entries match the saved account list."""
    wanted = set(shops)
    saved = {
        shop: row
        for shop, row in _load_imported_shops().items()
        if not row.get("managed_account") or shop in wanted
    }
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for shop in wanted:
        previous = saved.get(shop, {})
        saved[shop] = {
            "status": previous.get("status", "pending"),
            "message": previous.get("message", "等待批量登录"),
            "updated_at": now,
            "managed_account": True,
        }
    _save_imported_shops(saved)


def mark_imported_shop_login(shop: str, success: bool, message: str = "") -> None:
    saved = _load_imported_shops()
    previous = saved.get(shop, {})
    state = {
        "status": "verified" if success else "needs_login",
        "message": message[:300],
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    states = dict(previous.get("browser_login", {}))
    if not states and previous.get("status"):
        states["chromium"] = {
            key: previous[key] for key in ("status", "message", "updated_at") if key in previous
        }
    states[browser_settings.browser_engine()] = state
    saved[shop] = {
        **state,
        "browser_login": states,
        **({"managed_account": True} if previous.get("managed_account") else {}),
    }
    _save_imported_shops(saved)


def set_shop_disabled(shop: str, disabled: bool) -> None:
    shops = _load_disabled_shops()
    if disabled:
        shops.add(shop)
    else:
        shops.discard(shop)
    _save_disabled_shops(shops)


def delete_shop_profile(shop: str) -> None:
    """删除 state/profiles/<shop>/ 登录态目录，不碰数据库里的历史订单
    记录（见项目实施计划"删除店铺"一节的默认行为）。"""
    import shutil

    profile_dir = PROJECT_PATHS.root / "state" / "profiles" / shop
    if profile_dir.is_dir():
        shutil.rmtree(profile_dir)
    imported = _load_imported_shops()
    if shop in imported:
        imported.pop(shop, None)
        _save_imported_shops(imported)
    try:
        from . import account_store

        account_store.delete_by_shop(shop)
    except Exception:
        # Profile deletion remains available if the optional account store is unreadable.
        pass


def reset_today_schedule(shop: str) -> None:
    """清掉这家店铺今天的调度状态（done/failed 都清），让下一轮重新
    处理它——典型场景：模拟提交跑完一轮被标成 done，想当天切到真实
    提交重新跑一遍。只是清一个调度标记位，不碰订单历史数据。"""
    db.clear_shop_daily_status(shop)


def _is_login_required_failure(schedule: dict | None) -> bool:
    """兼容历史版本略有差异的登录失效文案。"""
    if not schedule or schedule.get("status") != "failed":
        return False
    detail = str(schedule.get("detail") or "")
    return detail == db.LOGIN_REQUIRED_DETAIL or "登录状态已失效或未登录" in detail


def mark_shop_login_succeeded(shop: str) -> None:
    """重新登录成功后清除当天残留的“登录失效”调度失败状态。

    只清登录问题造成的 failed；正常完成、排队中或其它业务失败都保留，
    避免一次登录操作意外改变无关的调度结果。
    """
    schedule = db.get_shop_daily_status(shop)
    if _is_login_required_failure(schedule):
        db.clear_shop_daily_status(shop)
    if shop in _load_imported_shops():
        mark_imported_shop_login(shop, True, "登录已验证")


def get_shop_max_orders(shop: str) -> int | None:
    """这家店铺当前配置的单次采集处理上限；未配置过返回 None。"""
    return db.get_shop_max_orders(shop)


def set_shop_max_orders(shop: str, max_orders: int | None) -> None:
    """写入/清除这家店铺的处理上限；max_orders=None 表示清除，恢复默认行为。"""
    db.set_shop_max_orders(shop, max_orders)


def set_shop_max_orders_batch(shops: list[str], max_orders: int | None) -> int:
    """为多家店铺统一写入/清除处理上限，返回实际处理数量。"""
    return db.set_shop_max_orders_batch(shops, max_orders)


def get_shop_group(shop: str) -> str | None:
    """这家店铺当前所属组；未分组时返回 None。"""
    return db.get_shop_group(shop)


def set_shop_group(shop: str, group_name: str | None) -> None:
    """设置或清除一家店铺的所属组。"""
    db.set_shop_group(shop, group_name)


def set_shop_group_batch(shops: list[str], group_name: str | None) -> int:
    """批量设置或清除店铺所属组，返回实际处理数量。"""
    return db.set_shop_group_batch(shops, group_name)


def list_shop_groups() -> dict[str, str]:
    """返回全部已设置的店铺分组映射。"""
    return db.list_shop_groups()


def _browser_login_state(imported: dict) -> dict:
    engine = browser_settings.browser_engine()
    states = imported.get("browser_login", {})
    if engine in states:
        return states[engine]
    # Before browser selection existed, saved login records belonged to Chrome.
    return imported if engine == "chromium" and not states else {"status": "needs_login"}


def list_shop_summaries() -> list[ShopSummary]:
    """全量店铺清单：state/profiles/ 下已登录过的店铺 ∪ 数据库里有历史
    记录的店铺（两者理论上大部分重合，但都可能有对方没有的——比如刚删了
    profile 目录但历史订单还在库里，或者 profile 目录还在但从来没跑过
    采集）。按店铺名排序，disabled 的店铺照样返回（是否在界面上隐藏由
    调用方决定），不在这里过滤。
    """
    profile_shops = set(scheduler_core.discover_shops())
    summary_rows = {row["shop"]: row for row in db.daily_shop_summary()}
    activity = db.last_activity_by_shop()
    schedule_rows = {row["shop"]: row for row in db.list_shop_daily_status()}
    disabled = _load_disabled_shops()
    max_orders_rows = db.list_shop_max_orders()
    group_rows = db.list_shop_groups()
    imported_rows = _load_imported_shops()

    all_shops = sorted(
        profile_shops | set(summary_rows) | set(schedule_rows) | set(imported_rows)
    )

    result: list[ShopSummary] = []
    for shop in all_shops:
        summary = summary_rows.get(shop)
        schedule = schedule_rows.get(shop)
        imported = imported_rows.get(shop)

        if schedule is None:
            schedule_status, schedule_detail = "not_started", None
        else:
            schedule_status = schedule["status"]
            schedule_detail = schedule.get("detail")

        # 今天确实跑过、确实是因为登录失效才失败的——比"目录存不存在"
        # 准确得多，但只在跑过一次之后才知道（见调用方 B/A 页展示的
        # 说明）。跟"已经手动删了 profile 目录"是两回事，那种情况
        # has_profile 本身就是 False，不需要靠这个字段再判断一次。
        login_expired = _is_login_required_failure(schedule) or bool(
            imported and _browser_login_state(imported).get("status") != "verified"
        )

        result.append(
            ShopSummary(
                shop=shop,
                has_profile=shop in profile_shops and browser_settings.profile_directory(
                    PROJECT_PATHS.root / "state" / "profiles" / shop
                ).is_dir(),
                disabled=shop in disabled,
                schedule_status=schedule_status,
                schedule_detail=schedule_detail,
                login_expired=login_expired,
                pending_remaining=summary["pending_remaining"] if summary else 0,
                submitted_today=summary["submitted_today"] if summary else 0,
                failed_today=summary["failed_today"] if summary else 0,
                skipped_today=summary["skipped_today"] if summary else 0,
                touched_today=summary["touched_today"] if summary else 0,
                last_activity=activity.get(shop),
                max_orders=max_orders_rows.get(shop),
                group_name=group_rows.get(shop),
            )
        )
    return result


def overall_totals(summaries: list[ShopSummary] | None = None) -> dict:
    """跨店铺汇总数字，供 A 页顶部数字条用。"""
    rows = summaries if summaries is not None else list_shop_summaries()
    return {
        "shop_count": len(rows),
        "done_today": sum(1 for r in rows if r.schedule_status == "done"),
        "pending_remaining": sum(r.pending_remaining for r in rows),
        "submitted_today": sum(r.submitted_today for r in rows),
        "failed_today": sum(r.failed_today for r in rows),
    }
