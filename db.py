"""订单状态与分析结果的统一存储：一个 SQLite 文件，覆盖所有店铺。

替代原来分散在 output/<shop>/appealable_orders_<run_id>.json 和
output/<shop>/chat_<订单号>.json 里的数据——那种"文件存在与否即状态"的
隐式判断，换成一张有明确 status 字段的表，还能跨店铺查询"现在还有哪些
订单没处理完"。

有意不 import paths.py、不依赖 PDDCHAT_SHOP 环境变量：paths.py 那套
"进程启动时读一次 --shop 就固化路径"的设计，是"一个进程只服务一家店"这个
假设深入到全项目 10 个文件的产物，运行期想切换店铺代价很高。这里 shop
作为每个函数的显式参数传入，数据库本身跟"当前进程属于哪家店"完全无关，
以后无论是查跨店铺状态还是任何"运行时指定店铺"的场景都能直接调用。

截图类二进制文件（evidence/、debug/ 下的 PNG）不进数据库，仍然是文件；
这里的 evidence_paths 字段只存相对路径（字符串列表，JSON 编码）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "state" / "pddchat.db"

# 已提交申诉的结果字符串 -> (status, 是否算 error) 映射。
# 直接照抄 submit_single_order（main.py）现有的返回值词汇表，不新造一套。
_SUBMIT_STATUS_MAP: dict[str, str] = {
    "未找到申诉记录": "not_found",
    "已申诉-跳过": "already_appealed",
    "已超时-跳过": "expired",  # 申诉窗口已过期、平台已处罚，见 appeal_submit.py 的 EXPIRED 状态
    "已驳回-跳过": "rejected",  # 申诉过但没通过、已处罚，见 appeal_submit.py 的 REJECTED 状态
    "已提交": "submitted",
    "成功": "simulated",
    # 申诉结果查询功能（check_appeal_results.py）专用——拼多多审核通过。
    # 左边这个 key 是 check_appeal_results.py 内部用的 result_text 词汇
    # （它的 APPEAL_RESULT_TEXT_MAP 把列表页原始文案"申诉通过"翻译成
    # 这里的"已通过"再传进来），不是照搬页面原文——跟"已超时-跳过"/
    # "已驳回-跳过"这两个词汇同理，都是"给 main.py 提交流程用的内部
    # 说法"，check_appeal_results.py 复用同一套说法而不是照抄页面原文，
    # 是为了让两条路径查到同一种结果时落到同一个 status。
    "已通过": "approved",
}

# _SUBMIT_STATUS_MAP 的反向映射：status 枚举值 -> 有代表性的原始 result_text。
# 供 result_text_for_status() 用，见其文档。_SUBMIT_STATUS_MAP 目前是
# 一一对应（没有两个 key 映射到同一个 status），反向查找不会丢信息；
# 以后如果哪天出现多对一，这里会保留"字典推导式里最后一个"，调用方不需要
# 关心这个边界情况——result_text_for_status() 本来就只是"给个有代表性的
# 说法"，不是精确还原当时的原始页面文案。
_STATUS_TO_RESULT_TEXT: dict[str, str] = {v: k for k, v in _SUBMIT_STATUS_MAP.items()}

# login.py 的 wait_for_login(interactive=False) 检测不到登录态时抛出的
# 错误消息，原样落进 shop_daily_status.detail。放在 db.py 而不是
# login.py——db.py 是全项目唯一"零第三方依赖、GUI 和主项目都能安全 import"
# 的模块（login.py 依赖 paths.py 那条链，GUI 不能直接 import 它），
# login.py 反过来 `from db import LOGIN_REQUIRED_DETAIL` 没有循环依赖
# 风险。gui/app/services/shop_repo.py 用这个常量比对当天的 detail 字段，
# 判断"今天调度失败具体是不是因为登录失效"，用来在 B 页"登录状态"列
# 显示更准确的"未登录"（而不是只看 state/profiles/<shop>/ 目录存不存在）。
LOGIN_REQUIRED_DETAIL = (
    "登录状态已失效或未登录（serve 模式不等待人工登录）——"
    "需要用 python main.py --shop <店铺> 手动登录一次这家店铺后再试。"
)

_connection: sqlite3.Connection | None = None

# 首个版本发布之后才新增的列：字段名 -> ALTER TABLE 用的类型声明。
# CREATE TABLE 语句本身已经含有这些列（全新数据库直接生效），这份字典是
# 给已经存在、还没这些列的旧数据库补课用的——_ensure_columns 会检查每一
# 列存在与否，缺了就 ALTER TABLE ADD COLUMN。以后再加字段，两处都要加一笔
# （CREATE TABLE 里加一行，这里也加一行），保持新旧数据库最终同构。
_MIGRATION_COLUMNS: dict[str, str] = {
    "ticket_sn": "TEXT",
    "goods_title": "TEXT",
    "goods_id": "TEXT",
    "goods_image_url": "TEXT",
    "compensation_amount": "TEXT",
    "order_created_at": "TEXT",
    "error_screenshot_path": "TEXT",
    "form_screenshot_path": "TEXT",
}

_RECONSIDERATION_MIGRATION_COLUMNS: dict[str, str] = {
    "result_text": "TEXT",
    "result_checked_at": "TEXT",
    "platform_reconsidered_at": "TEXT",
    "refund_amount": "TEXT",
}

_AFTERSALE_APPEAL_MIGRATION_COLUMNS: dict[str, str] = {
    "order_info": "TEXT",
    "goods_title": "TEXT",
    "goods_spec": "TEXT",
    "goods_image_url": "TEXT",
    "shipping_status": "TEXT",
    "paid_amount": "TEXT",
    "appeal_reason": "TEXT",
    "appeal_amount": "TEXT",
    "submitted_at": "TEXT",
    "platform_appealed_at": "TEXT",
    "result_text": "TEXT",
    "result_checked_at": "TEXT",
}

_SHOP_SETTINGS_MIGRATION_COLUMNS: dict[str, str] = {
    "group_name": "TEXT",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_columns(
    conn: sqlite3.Connection, table: str, columns: dict[str, str]
) -> None:
    """给已存在的表追加缺失的列，不需要额外的迁移框架。"""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            shop                 TEXT NOT NULL,
            order_no             TEXT NOT NULL,
            ticket_sn            TEXT,
            issue_type           TEXT,
            goods_title          TEXT,
            goods_id             TEXT,
            goods_image_url      TEXT,
            compensation_amount  TEXT,
            order_created_at     TEXT,
            status               TEXT NOT NULL,
            classification       TEXT,
            appeal_text          TEXT,
            messages             TEXT,
            message_count        INTEGER,
            evidence_paths       TEXT,
            error_message        TEXT,
            error_screenshot_path TEXT,
            form_screenshot_path  TEXT,
            collected_at         TEXT,
            analyzed_at          TEXT,
            submitted_at         TEXT,
            updated_at           TEXT NOT NULL,
            PRIMARY KEY (shop, order_no)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_shop_status ON orders(shop, status)"
    )
    # 对全新数据库这是空操作（上面 CREATE TABLE 已经含有这些列）；
    # 对已存在的旧数据库会把缺的列追加上。
    _ensure_columns(conn, "orders", _MIGRATION_COLUMNS)

    # 店铺 + 日期粒度的"今日调度周期"状态——跟 orders 表是不同粒度的东西：
    # orders 只知道单个订单的结果，回答不了"这家店铺今天的采集周期本身
    # 有没有跑完"。run_shops_interleaved.py 靠这张表决定要不要为一家店铺
    # 开浏览器：done 就跳过，failed（或者压根没记录）就重试。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shop_daily_status (
            shop        TEXT NOT NULL,
            run_date    TEXT NOT NULL,
            status      TEXT NOT NULL,
            detail      TEXT,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (shop, run_date)
        )
        """
    )

    # 一个订单号在拼多多那边可能对应多条独立的"消费者负向体验"记录（商品
    # 问题一条、服务问题一条，甚至同类型也可能不止一条）——orders 表按
    # (shop, order_no) 一行的粒度存不下"这个订单号下第几条记录是什么状态"，
    # 所以单独开一张表，用 record_key（从这条记录所在行文本里提取出的稳定
    # 标识，一般是创建时间）区分同一订单号下的不同记录。分析结果（聊天记录
    # /AI 判定的申诉凭证/申诉文案）不放在这张表——那些是基于聊天记录判断
    # 的，跟"第几条负向体验记录"无关，同一订单号的多条记录共用 orders 表
    # 里那一份分析结果。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS appeal_records (
            shop                  TEXT NOT NULL,
            order_no              TEXT NOT NULL,
            record_key            TEXT NOT NULL,
            issue_type            TEXT,
            status                TEXT NOT NULL,
            error_message         TEXT,
            form_screenshot_path  TEXT,
            error_screenshot_path TEXT,
            submitted_at          TEXT,
            updated_at            TEXT NOT NULL,
            PRIMARY KEY (shop, order_no, record_key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_appeal_records_shop_order "
        "ON appeal_records(shop, order_no)"
    )

    # 申诉结果每次回查的快照。orders 只保留最新状态，这张追加表供报表页
    # 的“查看详情”展示回查历史；旧数据库启动时会自动建表。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS appeal_review_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            shop        TEXT NOT NULL,
            order_no    TEXT NOT NULL,
            status      TEXT NOT NULL,
            detail      TEXT,
            checked_at  TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_history_shop_order "
        "ON appeal_review_history(shop, order_no, checked_at)"
    )

    # 复议与首次申诉是两个独立生命周期：首次申诉的最终结果必须继续保留
    # 在 orders/appeal_records 中，不能因为开始复议就被“待复议”等中间态
    # 覆盖。因此用独立表记录可复议列表快照、材料复用情况与提交结果。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reconsideration_records (
            shop                  TEXT NOT NULL,
            order_no              TEXT NOT NULL,
            record_key            TEXT NOT NULL,
            issue_type            TEXT,
            goods_title           TEXT,
            goods_image_url       TEXT,
            penalty_amount        TEXT,
            punished_at           TEXT,
            countdown_text        TEXT,
            deadline_at           TEXT,
            status                TEXT NOT NULL,
            reconsider_reason     TEXT,
            reconsider_text       TEXT,
            evidence_paths        TEXT,
            error_message         TEXT,
            form_screenshot_path  TEXT,
            error_screenshot_path TEXT,
            discovered_at         TEXT NOT NULL,
            last_seen_at          TEXT NOT NULL,
            submitted_at          TEXT,
            result_text           TEXT,
            result_checked_at     TEXT,
            platform_reconsidered_at TEXT,
            refund_amount         TEXT,
            updated_at            TEXT NOT NULL,
            PRIMARY KEY (shop, order_no, record_key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reconsideration_shop_status_deadline "
        "ON reconsideration_records(shop, status, deadline_at)"
    )
    _ensure_columns(
        conn,
        "reconsideration_records",
        _RECONSIDERATION_MIGRATION_COLUMNS,
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reconsideration_review_history (
            id                         INTEGER PRIMARY KEY AUTOINCREMENT,
            shop                       TEXT NOT NULL,
            order_no                   TEXT NOT NULL,
            record_key                 TEXT NOT NULL,
            status                     TEXT NOT NULL,
            result_text                TEXT,
            platform_reconsidered_at   TEXT,
            refund_amount              TEXT,
            detail                     TEXT,
            checked_at                 TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reconsider_review_shop_order "
        "ON reconsideration_review_history(shop, order_no, record_key, checked_at)"
    )

    # “售后申诉”与消费者负向体验申诉、复议是第三条独立业务线。同一个
    # 订单号可能同时出现在三处，不能复用 orders.status，否则一次售后
    # 模拟填表会覆盖首次申诉的审核结果。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS aftersale_appeal_records (
            shop                       TEXT NOT NULL,
            order_no                   TEXT NOT NULL,
            record_key                 TEXT NOT NULL,
            aftersale_id               TEXT,
            order_info                 TEXT,
            goods_title                TEXT,
            goods_spec                 TEXT,
            goods_image_url            TEXT,
            shipping_status            TEXT,
            paid_amount                TEXT,
            appeal_item                TEXT,
            refund_amount              TEXT,
            refund_time                TEXT,
            countdown_text             TEXT,
            status                     TEXT NOT NULL,
            classification             TEXT,
            appeal_reason              TEXT,
            appeal_amount              TEXT,
            appeal_text                TEXT,
            messages                   TEXT,
            message_count              INTEGER,
            refund_application_path     TEXT,
            chat_evidence_paths         TEXT,
            form_screenshot_path        TEXT,
            error_screenshot_path       TEXT,
            error_message               TEXT,
            discovered_at               TEXT NOT NULL,
            submitted_at                TEXT,
            platform_appealed_at        TEXT,
            result_text                 TEXT,
            result_checked_at           TEXT,
            updated_at                  TEXT NOT NULL,
            PRIMARY KEY (shop, order_no, record_key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_aftersale_appeal_shop_status "
        "ON aftersale_appeal_records(shop, status, refund_time)"
    )
    _ensure_columns(
        conn,
        "aftersale_appeal_records",
        _AFTERSALE_APPEAL_MIGRATION_COLUMNS,
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS aftersale_appeal_review_history (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            shop                  TEXT NOT NULL,
            order_no              TEXT NOT NULL,
            record_key            TEXT NOT NULL,
            status                TEXT NOT NULL,
            result_text           TEXT,
            platform_appealed_at  TEXT,
            appeal_item           TEXT,
            appeal_amount         TEXT,
            detail                TEXT,
            checked_at            TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_aftersale_review_shop_order "
        "ON aftersale_appeal_review_history(shop, order_no, record_key, checked_at)"
    )

    # 每家店铺的常设配置。单独开一张表而不是
    # 塞进 shop_daily_status：那张表是"日期粒度"的运行记录（每天一行，
    # run_date 是主键的一部分），这里要存的是"这家店铺本身"的配置，跟
    # 具体哪一天无关——混在一起会让"清空某天状态"之类按日期操作的函数
    # 不小心带上这份配置，两者生命周期完全不同，分开一张表更干净。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shop_settings (
            shop        TEXT NOT NULL PRIMARY KEY,
            max_orders  INTEGER,
            group_name  TEXT,
            updated_at  TEXT NOT NULL
        )
        """
    )
    _ensure_columns(conn, "shop_settings", _SHOP_SETTINGS_MIGRATION_COLUMNS)

    conn.commit()


def get_connection() -> sqlite3.Connection:
    """惰性建立模块级单例连接（每个进程一份）。

    主项目本身对数据库的访问都发生在单线程内（各 --serve 子进程各自单
    线程跑自己的浏览器逻辑；run_shops_interleaved.py 的读管道线程完全不碰
    数据库）——但 gui/ 子项目会用 QThreadPool 定时轮询这些查询函数（见
    gui/app/workers/db_poll_worker.py），每次轮询可能派发到线程池里不同
    的工作线程上，而不是固定同一个线程。sqlite3 连接默认绑定创建它的
    那个线程，不满足这个前提就会在第二个线程用到时直接抛
    ProgrammingError（现象：GUI 侧查询静默失败、界面数据一直不刷新）。
    check_same_thread=False 放开这条限制，让同一个连接对象可以跨线程用；
    这里的调用方式全是"一条查询、立刻 fetchall 拿完结果"，没有跨线程共享
    一个未提交事务或游标的场景，不会有数据错乱风险。对主项目原有的单线程
    用法是无操作的纯放宽，行为不变。
    """
    global _connection
    if _connection is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # 多个 --serve 子进程可能共用这一个数据库文件，WAL 模式下写锁冲突
        # 概率低很多（读不阻塞写、写也不阻塞读）。
        conn.execute("PRAGMA journal_mode=WAL")
        _init_schema(conn)
        _connection = conn
    return _connection


def _upsert(shop: str, order_no: str, **fields) -> None:
    """通用写入：行已存在就 UPDATE 只传入的字段，不存在就 INSERT 一行新的。

    只更新调用时传入的字段——不会因为这次只想记提交结果，就把之前存的
    classification/appeal_text 之类没提到的字段冲掉。

    没有用 SQLite 的 `INSERT ... ON CONFLICT DO UPDATE` 一条语句搞定：那种
    写法即使最终会走 UPDATE 分支，SQLite 仍然会先按 INSERT 的列表校验一遍
    NOT NULL 约束——只传 issue_type 这种"肯定是更新已有行"的调用会因为
    没带 status 而在完全不该报错的地方被 NOT NULL 约束拦下来。改成先查
    存在性、再分别走纯 UPDATE / 纯 INSERT，就没有这个问题：UPDATE 只碰
    传入的列，INSERT 才需要（且只在这个分支）为 status 兜底默认值。
    """
    fields.setdefault("updated_at", _now())
    conn = get_connection()

    with conn:
        existing = conn.execute(
            "SELECT 1 FROM orders WHERE shop=? AND order_no=?", (shop, order_no)
        ).fetchone()

        if existing is not None:
            set_clause = ", ".join(f"{c}=?" for c in fields)
            conn.execute(
                f"UPDATE orders SET {set_clause} WHERE shop=? AND order_no=?",
                [*fields.values(), shop, order_no],
            )
            return

        fields.setdefault("status", "pending")
        all_fields = {"shop": shop, "order_no": order_no, **fields}
        columns = list(all_fields.keys())
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(
            f"INSERT INTO orders ({', '.join(columns)}) VALUES ({placeholders})",
            [all_fields[c] for c in columns],
        )


def record_collected(
    shop: str,
    order_no: str,
    issue_type: str = "",
    *,
    goods_title: str = "",
    goods_id: str = "",
    goods_image_url: str = "",
    compensation_amount: str = "",
    order_created_at: str = "",
) -> None:
    """Phase 1 采集到一个可申诉订单时调用。

    已存在的行刷新问题类型及本次实际采到的商品/金额/创建时间字段，但不动
    处理状态等其余字段（已经分析或提交过的订单再次出现时不应回退状态）。

    注意：不主动把 status 设成 "pending"——如果这个订单之前已经分析/提交
    过，重新采集到它不该把状态往回退；只有全新订单（本来就没有这一行）
    才会因为 INSERT 分支落到下面 record_collected 传的 status。
    """
    collected_fields = {
        "issue_type": issue_type,
        "goods_title": goods_title,
        "goods_id": goods_id,
        "goods_image_url": goods_image_url,
        "compensation_amount": compensation_amount,
        "order_created_at": order_created_at,
    }
    # 页面偶尔会因为图片/文本尚未完成渲染而返回空值。重复采集已有订单时，
    # 空值不应把上一次已经采到的业务字段覆盖掉；问题类型沿用原有行为，
    # 即使本次为空也照常刷新。
    fields = {
        key: value
        for key, value in collected_fields.items()
        if key == "issue_type" or value
    }

    existing = get_order(shop, order_no)
    if existing is not None:
        # 已有记录：刷新本次实际采到的列表字段，状态保持原样。
        _upsert(shop, order_no, **fields)
        return

    _upsert(
        shop,
        order_no,
        **fields,
        status="pending",
        collected_at=_now(),
    )


def backfill_order_list_details(
    shop: str,
    order_no: str,
    *,
    goods_title: str = "",
    goods_id: str = "",
    goods_image_url: str = "",
    compensation_amount: str = "",
    order_created_at: str = "",
) -> bool:
    """用申诉结果列表的非空值补齐订单资料，不覆盖已有内容或订单状态。

    结果回查时有些历史订单只保存了订单号和申诉状态，ticketSn、商品信息、
    补偿金额、创建时间均可能为空。平台按订单号查询仍能返回这些列表字段，
    这里专门做“只补空缺”的增量更新，避免较晚的一次回查把首次采集到的
    商品或创建时间覆盖掉。
    """
    existing = get_order(shop, order_no)
    if existing is None:
        raise KeyError(f"订单不存在：{shop}/{order_no}")

    incoming = {
        "goods_title": goods_title,
        "goods_id": goods_id,
        "goods_image_url": goods_image_url,
        "compensation_amount": compensation_amount,
        "order_created_at": order_created_at,
    }
    fields = {
        name: str(value).strip()
        for name, value in incoming.items()
        if str(value or "").strip() and not str(existing.get(name) or "").strip()
    }
    if not fields:
        return False
    _upsert(shop, order_no, **fields)
    return True


def record_ticket_sn(shop: str, order_no: str, ticket_sn: str) -> None:
    """记录打开申诉详情页后从 URL 获得的 ticketSn。

    通常一个订单只有一个 ticketSn；若同一订单对应多条申诉记录，则把不同
    ticketSn 按发现顺序用中文顿号连接，重复打开同一详情页不会重复追加。
    """
    ticket_sn = ticket_sn.strip()
    if not ticket_sn:
        return

    existing = get_order(shop, order_no)
    existing_value = (existing or {}).get("ticket_sn") or ""
    ticket_sns = [value for value in existing_value.split("、") if value]
    if ticket_sn in ticket_sns:
        return

    ticket_sns.append(ticket_sn)
    _upsert(shop, order_no, ticket_sn="、".join(ticket_sns))


def record_analyzed(
    shop: str,
    order_no: str,
    classification: str,
    appeal_text: str,
    messages: list[dict],
    message_count: int,
    evidence_paths: list[str],
    issue_type: str = "",
) -> None:
    """Phase 2 分析完成（含"无聊天记录"兜底分类）时调用。"""
    _upsert(
        shop,
        order_no,
        status="analyzed",
        classification=classification,
        appeal_text=appeal_text,
        messages=json.dumps(messages, ensure_ascii=False),
        message_count=message_count,
        evidence_paths=json.dumps(evidence_paths, ensure_ascii=False),
        issue_type=issue_type,
        analyzed_at=_now(),
        error_message=None,
    )


def record_analysis_failed(
    shop: str,
    order_no: str,
    error: object,
    screenshot_path: str | None = None,
) -> None:
    """Phase 2 分析过程异常时调用。

    screenshot_path：save_error_screenshot() 返回的现场截图相对路径
    （调用方转换好再传进来，db.py 不碰 Path/文件系统），没截到就传 None
    （不传也一样，这样已有截图路径不会被没截到的一次调用覆盖成空）。
    """
    fields: dict[str, object] = {
        "status": "analysis_failed",
        "error_message": str(error),
    }
    if screenshot_path:
        fields["error_screenshot_path"] = screenshot_path
    _upsert(shop, order_no, **fields)


def record_submit_result(
    shop: str,
    order_no: str,
    result_text: str,
    form_screenshot_path: str | None = None,
    error_screenshot_path: str | None = None,
    fallback_status: str = "submit_failed",
) -> None:
    """submit_single_order() 算出结果字符串后调用，把它映射成 status 落库。

    result_text 取值集合见 main.py 的 submit_single_order/
    process_and_submit_one_order 文档：
    "未找到申诉记录" / "已申诉-跳过" / "已提交" / "成功" / "失败: {exc}"。

    form_screenshot_path：填表后截图（DEBUG_DIR/appeal_form_filled_
    <单号>.png）的相对路径，"未找到申诉记录"/"已申诉-跳过" 这两种结果发生
    在填表之前，通常不会有；error_screenshot_path：失败时 save_error_
    screenshot() 的相对路径。两个都是可选的，不传就不碰对应的列。

    fallback_status：result_text 没有命中 _SUBMIT_STATUS_MAP 时兜底落库的
    status，默认 "submit_failed"（原有调用方零改动、行为不变）。
    check_appeal_results.py 会显式传 fallback_status="review_unknown"——
    那条路径下"没识别出来的文案"通常意味着"成功读到了一个还没见过的审核
    结论"，跟"提交这个动作本身失败了"是两回事，不该都归进 submit_failed，
    否则 --status/GUI 会把"审核给了新说法"误报成程序出错。
    """
    status = _SUBMIT_STATUS_MAP.get(result_text)
    fields: dict[str, object] = {}

    if status is not None:
        fields["status"] = status
        # 命中映射表说明这次是已知的正常结束状态（提交成功/模拟成功/
        # 各种跳过），不是异常——清掉上一次失败遗留的 error_message，
        # 否则订单这次已经处理成功了，_upsert 只更新传入字段的机制会让
        # 那条早就不成立的旧错误信息一直留在库里，误导 --status 报表和
        # 人工查库。跟 record_analyzed 对分析阶段成功时清 error_message
        # 是同一个思路。
        fields["error_message"] = None
        if status in ("submitted", "simulated"):
            fields["submitted_at"] = _now()
    else:
        # 未命中映射表的一律当失败处理（"失败: ..." 或任何新出现的措辞），
        # 把原始文本存进 error_message 方便排查，不用维护一份精确的前缀匹配。
        fields["status"] = fallback_status
        fields["error_message"] = result_text

    if form_screenshot_path:
        fields["form_screenshot_path"] = form_screenshot_path
    if error_screenshot_path:
        fields["error_screenshot_path"] = error_screenshot_path

    _upsert(shop, order_no, **fields)


def record_order_review_status(
    shop: str,
    order_no: str,
    status_text: str,
    detail: str | None = None,
) -> None:
    """把申诉结果查询得到的中文汇总状态写回 orders 表。

    这条路径只更新结果状态和可选明细，不改变提交时间、截图等申诉阶段字段。
    """
    status_text = status_text.strip()
    if not status_text:
        raise ValueError("订单申诉结果状态不能为空")
    _upsert(
        shop,
        order_no,
        status=status_text,
        error_message=detail,
    )
    conn = get_connection()
    with conn:
        conn.execute(
            """
            INSERT INTO appeal_review_history
                (shop, order_no, status, detail, checked_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (shop, order_no, status_text, detail, _now()),
        )


def record_appeal_result(
    shop: str,
    order_no: str,
    record_key: str,
    issue_type: str,
    result_text: str,
    form_screenshot_path: str | None = None,
    error_screenshot_path: str | None = None,
    fallback_status: str = "submit_failed",
) -> None:
    """一个订单号名下某一条具体记录处理完（提交/取消/发现已申诉/找不到）
    后调用，落到 appeal_records 表——跟 record_submit_result 用同一套
    _SUBMIT_STATUS_MAP 映射，取值含义完全一致，只是多了 record_key 这个
    维度，能区分同一订单号下的不同记录。

    fallback_status：含义跟 record_submit_result 的同名参数完全一致，
    见其文档——默认值同样保持所有现有调用方零改动。

    每条记录只会在处理完那一刻整行落一次库（不像 orders 表的 _upsert 那样
    要支持分阶段增量更新同一行），用 INSERT OR REPLACE 整行覆盖足够，不会
    碰到 _upsert() 文档里提到的部分字段 NOT NULL 校验问题。
    """
    status = _SUBMIT_STATUS_MAP.get(result_text)
    error_message = None if status is not None else result_text
    if status is None:
        status = fallback_status
    submitted_at = _now() if status in ("submitted", "simulated") else None

    conn = get_connection()
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO appeal_records
                (shop, order_no, record_key, issue_type, status, error_message,
                 form_screenshot_path, error_screenshot_path, submitted_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                shop,
                order_no,
                record_key,
                issue_type,
                status,
                error_message,
                form_screenshot_path,
                error_screenshot_path,
                submitted_at,
                _now(),
            ),
        )


def result_text_for_status(status: str) -> str:
    """把 status 枚举值还原成 _SUBMIT_STATUS_MAP 里对应的原始 result_text。

    场景：check_appeal_results.py 需要把 appeal_records 表里已经存好的
    多条记录状态重新合并、喂回 record_submit_result()——那个函数是按
    result_text（原始文案）分类的接口，不直接接受 status。找不到对应
    关系时（比如 status 本身就是某次调用传的 fallback_status，例如
    "review_unknown"，不在 _SUBMIT_STATUS_MAP 的值域里）原样返回 status
    自己，足够在合并展示时看出"这条状态未知"，不强求精确还原当时的原始
    页面文案。
    """
    return _STATUS_TO_RESULT_TEXT.get(status, status)


def list_appeal_records(shop: str, order_no: str) -> list[dict]:
    """查一个订单号名下所有记录当前的状态，按 record_key 排序。

    用于以后做"只重试失败的那条记录"之类的场景——这次先把数据落好，
    调度层面还没有专门消费这张表的逻辑。
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM appeal_records WHERE shop=? AND order_no=? ORDER BY record_key",
        (shop, order_no),
    ).fetchall()
    return [dict(r) for r in rows]


def list_appeal_report_rows(shop: str | None = None) -> list[dict]:
    """查询已经提交后的首次申诉订单，以订单汇总结果作为最终口径。

    同一订单可能有多条 ``appeal_records``，结果回查会把页面汇总状态写回
    ``orders``。因此报表按订单展示，避免旧的子记录状态尚未同步时少算通过/
    失败，也避免一个订单因多条申诉记录重复累计金额。
    """
    report_statuses = (
        "submitted",
        "already_appealed",
        "review_unknown",
        "approved",
        "rejected",
        "已提交",
        "已申诉",
        "结果待确认",
        "申诉通过",
        "申诉未通过已处罚",
    )
    params: list[str] = list(report_statuses)
    shop_clause = ""
    if shop is not None:
        shop_clause = " AND o.shop=?"
        params.append(shop)
    placeholders = ",".join("?" for _ in report_statuses)
    rows = (
        get_connection()
        .execute(
            f"""
        SELECT
            o.*,
            NULL AS record_key,
            (
                SELECT MAX(h.checked_at)
                FROM appeal_review_history AS h
                WHERE h.shop=o.shop AND h.order_no=o.order_no
            ) AS result_checked_at
        FROM orders AS o
        WHERE o.status IN ({placeholders}){shop_clause}
        ORDER BY
            COALESCE(o.submitted_at, o.updated_at) DESC,
            o.shop, o.order_no
        """,
            params,
        )
        .fetchall()
    )
    result: list[dict] = []
    for row in rows:
        item = dict(row)
        raw = item.get("evidence_paths")
        if raw:
            try:
                item["evidence_paths"] = json.loads(raw)
            except json.JSONDecodeError:
                pass
        result.append(item)
    return result


def list_appeal_review_history(shop: str, order_no: str) -> list[dict]:
    """查询订单每次结果回查快照，最新记录排在最前面。"""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT status, detail, checked_at
        FROM appeal_review_history
        WHERE shop=? AND order_no=?
        ORDER BY checked_at DESC, id DESC
        """,
        (shop, order_no),
    ).fetchall()
    return [dict(row) for row in rows]


def record_reconsideration_discovered(
    shop: str,
    order_no: str,
    record_key: str,
    *,
    issue_type: str = "",
    goods_title: str = "",
    goods_image_url: str = "",
    penalty_amount: str = "",
    punished_at: str = "",
    countdown_text: str = "",
    deadline_at: str = "",
) -> None:
    """记录一次可复议列表扫描结果；已有任务只刷新页面字段与最后发现时间。"""
    now = _now()
    conn = get_connection()
    with conn:
        existing = conn.execute(
            """
            SELECT status FROM reconsideration_records
            WHERE shop=? AND order_no=? AND record_key=?
            """,
            (shop, order_no, record_key),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO reconsideration_records
                    (shop, order_no, record_key, issue_type, goods_title,
                     goods_image_url, penalty_amount, punished_at,
                     countdown_text, deadline_at, status, discovered_at,
                     last_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shop,
                    order_no,
                    record_key,
                    issue_type,
                    goods_title,
                    goods_image_url,
                    penalty_amount,
                    punished_at,
                    countdown_text,
                    deadline_at or None,
                    "待准备材料",
                    now,
                    now,
                    now,
                ),
            )
            return

        conn.execute(
            """
            UPDATE reconsideration_records SET
                issue_type=?, goods_title=?, goods_image_url=?,
                penalty_amount=?, punished_at=?, countdown_text=?,
                deadline_at=?, last_seen_at=?, updated_at=?
            WHERE shop=? AND order_no=? AND record_key=?
            """,
            (
                issue_type,
                goods_title,
                goods_image_url,
                penalty_amount,
                punished_at,
                countdown_text,
                deadline_at or None,
                now,
                now,
                shop,
                order_no,
                record_key,
            ),
        )


def update_reconsideration_status(
    shop: str,
    order_no: str,
    record_key: str,
    status: str,
    *,
    reconsider_reason: str | None = None,
    reconsider_text: str | None = None,
    evidence_paths: list[str] | None = None,
    error_message: str | None = None,
    form_screenshot_path: str | None = None,
    error_screenshot_path: str | None = None,
) -> None:
    """增量更新一条复议任务，所有对外状态均使用中文。"""
    fields: dict[str, object] = {
        "status": status,
        "error_message": error_message,
        "updated_at": _now(),
    }
    if reconsider_reason is not None:
        fields["reconsider_reason"] = reconsider_reason
    if reconsider_text is not None:
        fields["reconsider_text"] = reconsider_text
    if evidence_paths is not None:
        fields["evidence_paths"] = json.dumps(evidence_paths, ensure_ascii=False)
    if form_screenshot_path is not None:
        fields["form_screenshot_path"] = form_screenshot_path
    if error_screenshot_path is not None:
        fields["error_screenshot_path"] = error_screenshot_path
    if status == "已提交复议":
        fields["submitted_at"] = _now()

    set_clause = ", ".join(f"{name}=?" for name in fields)
    conn = get_connection()
    with conn:
        cursor = conn.execute(
            f"""
            UPDATE reconsideration_records SET {set_clause}
            WHERE shop=? AND order_no=? AND record_key=?
            """,
            [*fields.values(), shop, order_no, record_key],
        )
        if cursor.rowcount != 1:
            raise KeyError(f"复议任务不存在：{shop}/{order_no}/{record_key}")


def list_reconsideration_records(
    shop: str | None = None,
    statuses: list[str] | None = None,
) -> list[dict]:
    """查询复议任务；有截止时间的按最临近优先，没有截止时间的排后。"""
    clauses: list[str] = []
    params: list[str] = []
    if shop is not None:
        clauses.append("shop=?")
        params.append(shop)
    if statuses:
        clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
        params.extend(statuses)

    sql = "SELECT * FROM reconsideration_records"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY deadline_at IS NULL, deadline_at, shop, order_no, record_key"
    rows = get_connection().execute(sql, params).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        raw = item.get("evidence_paths")
        if raw:
            try:
                item["evidence_paths"] = json.loads(raw)
            except json.JSONDecodeError:
                pass
        result.append(item)
    return result


def list_reconsideration_report_rows(shop: str | None = None) -> list[dict]:
    """查询复议报表明细，并补齐首次申诉订单里的运营字段。"""
    report_statuses = (
        "已提交复议",
        "复议提交确认中",
        "待处理",
        "审核中",
        "复议结果待确认",
        "申诉成功",
        "申诉失败",
    )
    params: list[str] = list(report_statuses)
    placeholders = ",".join("?" for _ in report_statuses)
    where = f"WHERE r.status IN ({placeholders})"
    if shop is not None:
        where += " AND r.shop=?"
        params.append(shop)
    rows = (
        get_connection()
        .execute(
            f"""
        SELECT
            r.*,
            o.status AS original_appeal_status,
            o.goods_title AS order_goods_title,
            o.goods_image_url AS order_goods_image_url,
            o.issue_type AS order_issue_type,
            o.ticket_sn AS ticket_sn
        FROM reconsideration_records AS r
        LEFT JOIN orders AS o
          ON o.shop=r.shop AND o.order_no=r.order_no
        {where}
        ORDER BY
            COALESCE(r.platform_reconsidered_at, r.submitted_at, r.updated_at) DESC,
            r.shop, r.order_no, r.record_key
        """,
            params,
        )
        .fetchall()
    )
    result: list[dict] = []
    for row in rows:
        item = dict(row)
        if not item.get("goods_title"):
            item["goods_title"] = item.get("order_goods_title")
        if not item.get("goods_image_url"):
            item["goods_image_url"] = item.get("order_goods_image_url")
        if not item.get("issue_type"):
            item["issue_type"] = item.get("order_issue_type")
        raw = item.get("evidence_paths")
        if raw:
            try:
                item["evidence_paths"] = json.loads(raw)
            except json.JSONDecodeError:
                pass
        result.append(item)
    return result


def record_reconsideration_review_result(
    shop: str,
    order_no: str,
    record_key: str,
    status: str,
    *,
    result_text: str,
    platform_reconsidered_at: str = "",
    refund_amount: str = "",
    detail: str | None = None,
) -> None:
    """写入复议结果最新状态，并追加一次不可变的查询历史快照。"""
    checked_at = _now()
    conn = get_connection()
    with conn:
        cursor = conn.execute(
            """
            UPDATE reconsideration_records SET
                status=?, result_text=?, result_checked_at=?,
                platform_reconsidered_at=?, refund_amount=?,
                error_message=?, updated_at=?
            WHERE shop=? AND order_no=? AND record_key=?
            """,
            (
                status,
                result_text,
                checked_at,
                platform_reconsidered_at or None,
                refund_amount or None,
                detail,
                checked_at,
                shop,
                order_no,
                record_key,
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"复议任务不存在：{shop}/{order_no}/{record_key}")
        conn.execute(
            """
            INSERT INTO reconsideration_review_history
                (shop, order_no, record_key, status, result_text,
                 platform_reconsidered_at, refund_amount, detail, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                shop,
                order_no,
                record_key,
                status,
                result_text,
                platform_reconsidered_at or None,
                refund_amount or None,
                detail,
                checked_at,
            ),
        )


def list_reconsideration_review_history(
    shop: str,
    order_no: str,
    record_key: str | None = None,
) -> list[dict]:
    clauses = ["shop=?", "order_no=?"]
    params: list[str] = [shop, order_no]
    if record_key is not None:
        clauses.append("record_key=?")
        params.append(record_key)
    rows = (
        get_connection()
        .execute(
            "SELECT * FROM reconsideration_review_history WHERE "
            + " AND ".join(clauses)
            + " ORDER BY checked_at DESC, id DESC",
            params,
        )
        .fetchall()
    )
    return [dict(row) for row in rows]


def record_aftersale_appeal_discovered(
    shop: str,
    order_no: str,
    record_key: str,
    *,
    order_info: str = "",
    goods_title: str = "",
    goods_spec: str = "",
    goods_image_url: str = "",
    shipping_status: str = "",
    paid_amount: str = "",
    appeal_item: str = "",
    refund_amount: str = "",
    refund_time: str = "",
    countdown_text: str = "",
) -> None:
    """记录售后申诉列表行；重复发现只刷新页面快照，不倒退处理状态。"""
    now = _now()
    conn = get_connection()
    with conn:
        existing = conn.execute(
            """
            SELECT 1 FROM aftersale_appeal_records
            WHERE shop=? AND order_no=? AND record_key=?
            """,
            (shop, order_no, record_key),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO aftersale_appeal_records
                    (shop, order_no, record_key, order_info, goods_title,
                     goods_spec, goods_image_url, shipping_status, paid_amount,
                     appeal_item, refund_amount, refund_time, countdown_text,
                     status, discovered_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shop,
                    order_no,
                    record_key,
                    order_info,
                    goods_title,
                    goods_spec,
                    goods_image_url,
                    shipping_status,
                    paid_amount,
                    appeal_item,
                    refund_amount,
                    refund_time,
                    countdown_text,
                    "待处理",
                    now,
                    now,
                ),
            )
            return
        conn.execute(
            """
            UPDATE aftersale_appeal_records SET
                order_info=?, goods_title=?, goods_spec=?, goods_image_url=?,
                shipping_status=?, paid_amount=?, appeal_item=?,
                refund_amount=?, refund_time=?, countdown_text=?, updated_at=?
            WHERE shop=? AND order_no=? AND record_key=?
            """,
            (
                order_info,
                goods_title,
                goods_spec,
                goods_image_url,
                shipping_status,
                paid_amount,
                appeal_item,
                refund_amount,
                refund_time,
                countdown_text,
                now,
                shop,
                order_no,
                record_key,
            ),
        )


def update_aftersale_appeal_status(
    shop: str,
    order_no: str,
    record_key: str,
    status: str,
    *,
    aftersale_id: str | None = None,
    classification: str | None = None,
    appeal_reason: str | None = None,
    appeal_amount: str | None = None,
    appeal_text: str | None = None,
    messages: list[dict] | None = None,
    message_count: int | None = None,
    refund_application_path: str | None = None,
    chat_evidence_paths: list[str] | None = None,
    form_screenshot_path: str | None = None,
    error_screenshot_path: str | None = None,
    error_message: str | None = None,
) -> None:
    """增量更新一条售后申诉任务，保留未传入的既有材料。"""
    fields: dict[str, object] = {
        "status": status,
        "error_message": error_message,
        "updated_at": _now(),
    }
    optional = {
        "aftersale_id": aftersale_id,
        "classification": classification,
        "appeal_reason": appeal_reason,
        "appeal_amount": appeal_amount,
        "appeal_text": appeal_text,
        "message_count": message_count,
        "refund_application_path": refund_application_path,
        "form_screenshot_path": form_screenshot_path,
        "error_screenshot_path": error_screenshot_path,
    }
    fields.update(
        {name: value for name, value in optional.items() if value is not None}
    )
    if status == "已提交":
        fields["submitted_at"] = _now()
    if messages is not None:
        fields["messages"] = json.dumps(messages, ensure_ascii=False)
    if chat_evidence_paths is not None:
        fields["chat_evidence_paths"] = json.dumps(
            chat_evidence_paths, ensure_ascii=False
        )

    set_clause = ", ".join(f"{name}=?" for name in fields)
    conn = get_connection()
    with conn:
        cursor = conn.execute(
            f"""
            UPDATE aftersale_appeal_records SET {set_clause}
            WHERE shop=? AND order_no=? AND record_key=?
            """,
            [*fields.values(), shop, order_no, record_key],
        )
        if cursor.rowcount != 1:
            raise KeyError(f"售后申诉任务不存在：{shop}/{order_no}/{record_key}")


def record_aftersale_appeal_result(
    shop: str,
    order_no: str,
    record_key: str,
    status: str,
    *,
    result_text: str,
    platform_appealed_at: str = "",
    appeal_item: str = "",
    appeal_amount: str = "",
    detail: str | None = None,
) -> None:
    """回写售后申诉最新审核结果，并追加不可变历史快照。"""
    checked_at = _now()
    conn = get_connection()
    with conn:
        cursor = conn.execute(
            """
            UPDATE aftersale_appeal_records SET
                status=?, result_text=?, result_checked_at=?,
                platform_appealed_at=?, appeal_amount=?, error_message=?, updated_at=?
            WHERE shop=? AND order_no=? AND record_key=?
            """,
            (
                status,
                result_text,
                checked_at,
                platform_appealed_at or None,
                appeal_amount or None,
                detail,
                checked_at,
                shop,
                order_no,
                record_key,
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"售后申诉任务不存在：{shop}/{order_no}/{record_key}")
        conn.execute(
            """
            INSERT INTO aftersale_appeal_review_history
                (shop, order_no, record_key, status, result_text,
                 platform_appealed_at, appeal_item, appeal_amount, detail, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                shop,
                order_no,
                record_key,
                status,
                result_text,
                platform_appealed_at or None,
                appeal_item or None,
                appeal_amount or None,
                detail,
                checked_at,
            ),
        )


def list_aftersale_appeal_review_history(
    shop: str,
    order_no: str,
    record_key: str | None = None,
) -> list[dict]:
    clauses = ["shop=?", "order_no=?"]
    params: list[str] = [shop, order_no]
    if record_key is not None:
        clauses.append("record_key=?")
        params.append(record_key)
    rows = (
        get_connection()
        .execute(
            "SELECT * FROM aftersale_appeal_review_history WHERE "
            + " AND ".join(clauses)
            + " ORDER BY checked_at DESC, id DESC",
            params,
        )
        .fetchall()
    )
    return [dict(row) for row in rows]


def list_aftersale_appeal_records(shop: str | None = None) -> list[dict]:
    """查询售后申诉任务，JSON 字段还原成 Python 列表。"""
    conn = get_connection()
    if shop is None:
        rows = conn.execute(
            """
            SELECT * FROM aftersale_appeal_records
            ORDER BY shop, refund_time DESC, order_no, record_key
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM aftersale_appeal_records
            WHERE shop=? ORDER BY refund_time DESC, order_no, record_key
            """,
            (shop,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for field in ("messages", "chat_evidence_paths"):
            raw = item.get(field)
            item[field] = json.loads(raw) if raw else []
        result.append(item)
    return result


def get_order(shop: str, order_no: str) -> dict | None:
    """查一个订单当前的完整记录；不存在返回 None。

    messages/evidence_paths 会自动从 JSON 文本解码回 Python 列表。
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM orders WHERE shop=? AND order_no=?", (shop, order_no)
    ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def has_analysis(shop: str, order_no: str) -> bool:
    """该订单是否已经有非空的 classification + appeal_text。"""
    order = get_order(shop, order_no)
    if order is None:
        return False
    return bool(order.get("classification")) and bool(order.get("appeal_text"))


def list_orders(
    shop: str | None = None,
    statuses: list[str] | None = None,
) -> list[dict]:
    """查询订单列表。shop=None 时跨店铺查询（供报表/--status 用）。"""
    conn = get_connection()
    clauses: list[str] = []
    params: list[str] = []

    if shop is not None:
        clauses.append("shop=?")
        params.append(shop)
    if statuses:
        clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
        params.extend(statuses)

    sql = "SELECT * FROM orders"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY shop, order_no"

    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def last_activity_by_shop() -> dict[str, str]:
    """每家店铺在 orders 表里最新的 updated_at，供 GUI 仪表盘"最近活动
    时间"用。跟 status_counts/daily_shop_summary 一样直接现算，不额外
    记账；只有店铺在 orders 表里出现过（至少采集到一条记录）才会出现在
    返回的字典里，一次都没跑过采集的店铺查不到，调用方按 key 不存在处理
    （比如展示"尚未有活动"），不在这里强行补一个默认值。"""
    conn = get_connection()
    rows = conn.execute(
        "SELECT shop, MAX(updated_at) AS last_updated FROM orders GROUP BY shop"
    ).fetchall()
    return {row["shop"]: row["last_updated"] for row in rows}


def status_counts(shop: str | None = None) -> list[tuple[str, str, int]]:
    """按 (shop, status) 分组计数，供 --status 报表用。返回按 shop/status 排序的
    (shop, status, count) 三元组列表。"""
    conn = get_connection()
    if shop is not None:
        rows = conn.execute(
            "SELECT shop, status, COUNT(*) FROM orders WHERE shop=? "
            "GROUP BY shop, status ORDER BY shop, status",
            (shop,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT shop, status, COUNT(*) FROM orders "
            "GROUP BY shop, status ORDER BY shop, status"
        ).fetchall()
    return [tuple(r) for r in rows]


def daily_shop_summary(shop: str | None = None, day: str | None = None) -> list[dict]:
    """按店铺统计"今天"（本地日期）处理了多少单、失败多少单，以及当前还
    剩多少 pending 没处理完。供 main.py --status 的"今日处理情况"展示用。

    没有新建汇总表——orders 表本身的字段已经够算：updated_at 每次写入都会
    刷新，"今天处理了"就是 updated_at 落在这一天、且状态已经有最终结果的
    行数，不用额外记账，也不会跟 orders 表实际状态脱节（缺点是每次都要
    现算，但这张表的数据量对本地 SQLite 来说不算什么）。

    day：本地日期字符串 'YYYY-MM-DD'，不传默认今天；预留这个参数方便以后
    查"昨天"之类的场景，不用改函数签名。

    统计口径：
    - touched_today / submitted_today / failed_today / skipped_today：先按
      updated_at 转本地时区（'localtime'，即项目默认的 Asia/Shanghai）后
      是否等于目标日期筛选（不用 UTC 直接比，避免跨天时段——本地时间过了
      0 点但 UTC 日期还没跳——把同一个操作日的数据错误地切成两天），再要求
      status 是"已有最终结果"的一种（submitted/simulated/submit_failed/
      analysis_failed/not_found/already_appealed）——今天刚采集到、还没
      轮到处理的 pending，或者分析完但还没走到提交这步的 analyzed，即使
      updated_at 也是今天，不算"处理了"。touched_today 就是后三者之和。
    - pending_remaining：不按日期筛，是这家店*此刻*实际还剩多少单没处理
      完，不管是今天剩的还是攒了好几天——"是否处理完了"看这个是不是 0，
      跟"今天"这个时间窗口本身是两件事。

    返回按 shop 排序的字典列表，字段：shop / touched_today / submitted_today
    / failed_today / skipped_today / pending_remaining。只包含 orders 表里
    有过记录的店铺（一家店一条记录都没有，不会出现在结果里；如果只想看
    "当前有多少店铺连 pending 都没有"，这个函数看不出来，得配合
    state/profiles/ 目录一起看）。
    """
    conn = get_connection()

    day_expr = "date(?, 'localtime')" if day else "date('now', 'localtime')"
    day_params: list[str] = [day] if day else []
    shop_clause = "AND shop=?" if shop is not None else ""
    shop_params: list[str] = [shop] if shop is not None else []

    # touched_today 只数"今天有了最终结果"的订单——status 是 pending（还
    # 排队）或 analyzed（分析完但还没走到提交这步）都不算"处理完"，哪怕
    # 它们的 updated_at 也是今天（比如今天刚采集到、还没轮到处理）。
    today_rows = conn.execute(
        f"""
        SELECT
            shop,
            SUM(CASE WHEN status IN (
                'submitted', 'simulated', 'submit_failed',
                'analysis_failed', 'not_found', 'already_appealed'
            ) THEN 1 ELSE 0 END) AS touched_today,
            SUM(CASE WHEN status IN ('submitted', 'simulated') THEN 1 ELSE 0 END) AS submitted_today,
            SUM(CASE WHEN status IN ('analysis_failed', 'submit_failed') THEN 1 ELSE 0 END) AS failed_today,
            SUM(CASE WHEN status IN ('not_found', 'already_appealed') THEN 1 ELSE 0 END) AS skipped_today
        FROM orders
        WHERE date(updated_at, 'localtime') = {day_expr} {shop_clause}
        GROUP BY shop
        """,
        [*day_params, *shop_params],
    ).fetchall()

    pending_rows = conn.execute(
        f"""
        SELECT shop, COUNT(*) AS pending_remaining
        FROM orders
        WHERE status='pending' {shop_clause}
        GROUP BY shop
        """,
        shop_params,
    ).fetchall()

    summary: dict[str, dict] = {}

    def _entry(shop_name: str) -> dict:
        return summary.setdefault(
            shop_name,
            {
                "shop": shop_name,
                "touched_today": 0,
                "submitted_today": 0,
                "failed_today": 0,
                "skipped_today": 0,
                "pending_remaining": 0,
            },
        )

    for row in today_rows:
        entry = _entry(row["shop"])
        entry["touched_today"] = row["touched_today"]
        entry["submitted_today"] = row["submitted_today"]
        entry["failed_today"] = row["failed_today"]
        entry["skipped_today"] = row["skipped_today"]

    for row in pending_rows:
        _entry(row["shop"])["pending_remaining"] = row["pending_remaining"]

    return sorted(summary.values(), key=lambda x: x["shop"])


def _today_local() -> str:
    """本地日期字符串 'YYYY-MM-DD'，跟 main.py"今日处理情况"用的是同一种
    取值方式（datetime.now() 不带时区，取系统本地时间）——两处保持一致，
    不会出现"一边按 UTC 一边按本地时间"的错位。"""
    return datetime.now().strftime("%Y-%m-%d")


def get_shop_daily_status(shop: str, run_date: str | None = None) -> dict | None:
    """查一家店铺"某天"（默认今天，本地日期）的调度周期状态。

    返回 None 表示这天还没有任何记录——调用方应该理解成"需要处理"（还没
    跑过，或者是全新的一天），跟返回 {"status": "failed", ...} 走同一条
    "需要处理"的分支；只有明确拿到 status="done" 才应该跳过。
    """
    conn = get_connection()
    day = run_date or _today_local()
    row = conn.execute(
        "SELECT * FROM shop_daily_status WHERE shop=? AND run_date=?",
        (shop, day),
    ).fetchone()
    return dict(row) if row else None


def mark_shop_daily_done(shop: str, run_date: str | None = None) -> None:
    """标记这家店铺"今天"（默认今天）的调度周期正常结束——不管当时处理
    了 0 单还是很多单，只要不是中途因为登录失效之类的原因被迫放弃。

    这张表每次都是整行覆盖（没有 _upsert() 那种"只更新传入字段"的部分
    更新需求），用 INSERT OR REPLACE 就够，不会碰到 _upsert() 文档里提到
    的 NOT NULL 校验问题——那个问题只在"只传部分字段、可能落到 INSERT
    分支"时才会出现，这里每次都是完整一行。
    """
    day = run_date or _today_local()
    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO shop_daily_status "
            "(shop, run_date, status, detail, updated_at) VALUES (?, ?, 'done', NULL, ?)",
            (shop, day, _now()),
        )


def mark_shop_daily_failed(shop: str, detail: str, run_date: str | None = None) -> None:
    """标记这家店铺"今天"（默认今天）遇到没解决的问题（比如登录失效、
    连续失败被踢出）。跟 done 互斥——下次查询会认为这家店铺"需要处理"，
    不会被跳过。"""
    day = run_date or _today_local()
    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO shop_daily_status "
            "(shop, run_date, status, detail, updated_at) VALUES (?, ?, 'failed', ?, ?)",
            (shop, day, detail, _now()),
        )


def mark_shop_daily_skipped(
    shop: str, detail: str, run_date: str | None = None
) -> None:
    """标记这家店铺"今天"（默认今天）本轮调度压根没轮到开工——跟 failed
    不同：这不是这家店铺自身出了问题（登录失效/连续报错），单纯是并发
    名额不够、排到它的时候名额已经被别的店铺占满了。单独用一个状态而不是
    复用 'failed'，是为了在 A/B 页和 --status 报表里不把它误报成"这家店
    出故障了"，也让 Scheduler._skip_priority 能精确识别"连续被冷落的
    天数"，不跟真正的失败重试混在一起统计。

    跟 done/failed 一样，跟 get_shop_daily_status() 的"status != 'done' 才
    需要处理"这条判断规则天然兼容——'skipped' 不是 'done'，下次调度照样
    会把这家店铺纳入待处理名单，不会被跳过。
    """
    day = run_date or _today_local()
    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO shop_daily_status "
            "(shop, run_date, status, detail, updated_at) VALUES (?, ?, 'skipped', ?, ?)",
            (shop, day, detail, _now()),
        )


def clear_shop_daily_status(shop: str, run_date: str | None = None) -> None:
    """删掉这家店铺"今天"（默认今天）的调度状态记录，让下次查询回到
    "尚未开始"（get_shop_daily_status 返回 None）这个状态。

    典型场景：店铺已经用模拟提交跑完一轮被标成 done，运营想当天再切到
    真实提交重新跑一遍——done 不区分模拟/真实，只表示"这一轮跑完了"，
    不清掉的话调度器会一直跳过这家店铺直到明天。跟 mark_shop_daily_failed
    不同，这里是真的删除这一行而不是覆盖成 'failed'：模拟提交本身没有
    "失败"，用 failed 状态描述不准确，GUI/--status 报表看到的应该是
    "尚未开始"而不是一条子虚乌有的失败记录。

    行本来就不存在时是安全的空操作（DELETE 对不存在的行没有任何效果）。
    """
    day = run_date or _today_local()
    conn = get_connection()
    with conn:
        conn.execute(
            "DELETE FROM shop_daily_status WHERE shop=? AND run_date=?",
            (shop, day),
        )


def list_shop_daily_status(run_date: str | None = None) -> list[dict]:
    """查"某天"（默认今天）所有店铺的调度状态，按 shop 排序，供 --status
    展示用。只包含真正写过记录的店铺——没跑过的店铺不会出现在结果里，
    调用方要展示"尚未开始"需要自己拿店铺全集跟这个结果做差集。"""
    conn = get_connection()
    day = run_date or _today_local()
    rows = conn.execute(
        "SELECT * FROM shop_daily_status WHERE run_date=? ORDER BY shop",
        (day,),
    ).fetchall()
    return [dict(r) for r in rows]


# ============================================================
# 店铺配置：shop_settings 表
# ============================================================


def get_shop_max_orders(shop: str) -> int | None:
    """查一家店铺配置的"单次采集最多处理订单数"上限。

    返回 None 表示这家店铺没配置过（或者配置过又被显式清除）——调用方
    应该理解成"维持默认行为"：main.py 的 run_phase1() 拿到 None 时完全
    不会给 collect_all_appealable_order_numbers() 传 max_orders，等价于
    这家店铺没参与这次改动，采集行为跟这次改动上线前一模一样（默认只读
    第一页，见 run_appeal_batch.py 的 collect_all_appealable_order_numbers
    文档）。

    "行不存在"和"行存在但 max_orders 列是 NULL"都算"未配置"。后一种
    情况会在店铺只设置了 group_name 时正常出现，所以读取时同时兼容两者。
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT max_orders FROM shop_settings WHERE shop=?", (shop,)
    ).fetchone()
    return row["max_orders"] if row is not None else None


def set_shop_max_orders(shop: str, max_orders: int | None) -> None:
    """写入或清除一家店铺"单次采集最多处理订单数"的配置。

    max_orders=None：只清除这家店铺的自定义上限，恢复默认行为；已经设置的
    店铺分组必须保留。若这一行同时没有分组，才删除空配置行。

    max_orders=正整数：这家店铺以后每次 Phase 1 采集最多处理这么多个
    订单（允许翻页，直到凑够这个数或者没有更多订单为止——具体翻页页数
    上限由 main.py 的 run_phase1() 另外决定，这里只负责存这个数字本身，
    不关心它最终怎么被消费）。

    这里不校验 max_orders 的合法范围（比如不检查 > 0）——范围校验是各
    输入层自己的职责（GUI 对话框的 QSpinBox.setRange()、main.py argparse
    --max-orders 的手动检查），db.py 作为 GUI 和主项目共用的底层存储，
    不重复定义一次"合法范围"这种展示/交互层面的规则，以后调用方想放宽
    或收紧范围也不需要动这个函数。

    这里使用冲突更新而不是 INSERT OR REPLACE，避免修改处理上限时把同一行
    的 group_name 覆盖掉。
    """
    conn = get_connection()
    with conn:
        if max_orders is None:
            conn.execute(
                "UPDATE shop_settings SET max_orders=NULL, updated_at=? WHERE shop=?",
                (_now(), shop),
            )
            conn.execute(
                "DELETE FROM shop_settings WHERE shop=? AND max_orders IS NULL "
                "AND (group_name IS NULL OR TRIM(group_name)='')",
                (shop,),
            )
        else:
            conn.execute(
                "INSERT INTO shop_settings (shop, max_orders, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(shop) DO UPDATE SET "
                "max_orders=excluded.max_orders, updated_at=excluded.updated_at",
                (shop, max_orders, _now()),
            )


def set_shop_max_orders_batch(shops: list[str], max_orders: int | None) -> int:
    """批量写入或清除多家店铺的处理上限，返回实际处理的店铺数。

    所有店铺共用一个事务，保存过程中即使发生异常也不会留下只更新一部分
    店铺的中间状态。重复或空白店铺名会先被清理，避免无意义的重复写入。
    """
    normalized_shops = list(
        dict.fromkeys(shop.strip() for shop in shops if shop.strip())
    )
    if not normalized_shops:
        return 0

    conn = get_connection()
    with conn:
        if max_orders is None:
            updated_at = _now()
            conn.executemany(
                "UPDATE shop_settings SET max_orders=NULL, updated_at=? WHERE shop=?",
                ((updated_at, shop) for shop in normalized_shops),
            )
            conn.executemany(
                "DELETE FROM shop_settings WHERE shop=? AND max_orders IS NULL "
                "AND (group_name IS NULL OR TRIM(group_name)='')",
                ((shop,) for shop in normalized_shops),
            )
        else:
            updated_at = _now()
            conn.executemany(
                "INSERT INTO shop_settings (shop, max_orders, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(shop) DO UPDATE SET "
                "max_orders=excluded.max_orders, updated_at=excluded.updated_at",
                ((shop, max_orders, updated_at) for shop in normalized_shops),
            )
    return len(normalized_shops)


def list_shop_max_orders() -> dict[str, int]:
    """一次性查所有店铺已配置的处理上限，供 GUI 店铺列表批量展示用——
    跟 last_activity_by_shop()/list_shop_daily_status() 一样是"一次查
    全部、调用方按 shop 取"的批量接口，避免渲染一次表格就对 N 家店铺
    发 N 次单独查询。

    只包含配置过（且未被清除）的店铺——没配置过的店铺不会出现在返回的
    字典里，调用方要用 `result.get(shop)`（不存在时得到 None）而不是
    `result[shop]`，跟 last_activity_by_shop() 的约定完全一致。
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT shop, max_orders FROM shop_settings WHERE max_orders IS NOT NULL"
    ).fetchall()
    return {row["shop"]: row["max_orders"] for row in rows}


def get_shop_group(shop: str) -> str | None:
    """查询店铺当前所属组；未设置或已清除时返回 None。"""
    conn = get_connection()
    row = conn.execute(
        "SELECT group_name FROM shop_settings WHERE shop=?", (shop,)
    ).fetchone()
    if row is None or row["group_name"] is None:
        return None
    group_name = str(row["group_name"]).strip()
    return group_name or None


def set_shop_group(shop: str, group_name: str | None) -> None:
    """设置或清除一家店铺的所属组，同时保留处理上限配置。"""
    normalized_shop = shop.strip()
    if not normalized_shop:
        return
    normalized_group = str(group_name or "").strip() or None
    conn = get_connection()
    with conn:
        if normalized_group is None:
            conn.execute(
                "UPDATE shop_settings SET group_name=NULL, updated_at=? WHERE shop=?",
                (_now(), normalized_shop),
            )
            conn.execute(
                "DELETE FROM shop_settings WHERE shop=? AND max_orders IS NULL "
                "AND group_name IS NULL",
                (normalized_shop,),
            )
        else:
            conn.execute(
                "INSERT INTO shop_settings (shop, group_name, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(shop) DO UPDATE SET "
                "group_name=excluded.group_name, updated_at=excluded.updated_at",
                (normalized_shop, normalized_group, _now()),
            )


def set_shop_group_batch(shops: list[str], group_name: str | None) -> int:
    """批量设置或清除所属组，返回去重后的实际店铺数。"""
    normalized_shops = list(
        dict.fromkeys(shop.strip() for shop in shops if shop.strip())
    )
    if not normalized_shops:
        return 0
    normalized_group = str(group_name or "").strip() or None
    updated_at = _now()
    conn = get_connection()
    with conn:
        if normalized_group is None:
            conn.executemany(
                "UPDATE shop_settings SET group_name=NULL, updated_at=? WHERE shop=?",
                ((updated_at, shop) for shop in normalized_shops),
            )
            conn.executemany(
                "DELETE FROM shop_settings WHERE shop=? AND max_orders IS NULL "
                "AND group_name IS NULL",
                ((shop,) for shop in normalized_shops),
            )
        else:
            conn.executemany(
                "INSERT INTO shop_settings (shop, group_name, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(shop) DO UPDATE SET "
                "group_name=excluded.group_name, updated_at=excluded.updated_at",
                ((shop, normalized_group, updated_at) for shop in normalized_shops),
            )
    return len(normalized_shops)


def list_shop_groups() -> dict[str, str]:
    """一次性返回所有已分组店铺的 shop -> group_name 映射。"""
    conn = get_connection()
    rows = conn.execute(
        "SELECT shop, group_name FROM shop_settings "
        "WHERE group_name IS NOT NULL AND TRIM(group_name)<>''"
    ).fetchall()
    return {row["shop"]: str(row["group_name"]).strip() for row in rows}


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("messages", "evidence_paths"):
        raw = d.get(key)
        if raw:
            try:
                d[key] = json.loads(raw)
            except json.JSONDecodeError:
                pass
    return d


