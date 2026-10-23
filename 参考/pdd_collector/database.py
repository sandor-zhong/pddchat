import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import Appeal, CollectionError, Shop
from .credentials import protect, unprotect
from .account_import import account_key, normalize_accounts


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS shops (
                    shop_key TEXT PRIMARY KEY,
                    shop_name TEXT NOT NULL,
                    account_name TEXT NOT NULL,
                    avatar_url TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collection_runs (
                    id INTEGER PRIMARY KEY,
                    shop_key TEXT NOT NULL REFERENCES shops(shop_key),
                    date_range TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    pages INTEGER NOT NULL DEFAULT 0,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS appeals (
                    id INTEGER PRIMARY KEY,
                    shop_key TEXT NOT NULL REFERENCES shops(shop_key),
                    order_sn TEXT NOT NULL,
                    goods_id TEXT NOT NULL,
                    goods_name TEXT NOT NULL,
                    goods_image_url TEXT NOT NULL,
                    issue_type TEXT NOT NULL,
                    compensation_cents INTEGER,
                    compensation_text TEXT NOT NULL,
                    deduction_progress TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status = '申诉通过'),
                    operation TEXT NOT NULL,
                    detail_url TEXT NOT NULL,
                    raw_columns TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_run_id INTEGER NOT NULL REFERENCES collection_runs(id),
                    UNIQUE(shop_key, order_sn, goods_id, issue_type, created_at)
                );
                CREATE INDEX IF NOT EXISTS idx_appeals_created ON appeals(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_runs_shop ON collection_runs(shop_key, id DESC);
                CREATE TABLE IF NOT EXISTS managed_shops (
                    shop_key TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    group_name TEXT NOT NULL DEFAULT '未分组',
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                    max_pages INTEGER NOT NULL DEFAULT 0 CHECK(max_pages >= 0),
                    channel TEXT NOT NULL DEFAULT 'chrome',
                    login_status TEXT NOT NULL DEFAULT 'unknown',
                    login_checked_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_jobs (
                    id INTEGER PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    shop_key TEXT NOT NULL REFERENCES managed_shops(shop_key),
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    pages INTEGER NOT NULL DEFAULT 0,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_shop ON execution_jobs(shop_key, id DESC);
                CREATE TABLE IF NOT EXISTS shop_credentials (
                    shop_key TEXT PRIMARY KEY REFERENCES managed_shops(shop_key),
                    username TEXT NOT NULL UNIQUE,
                    password_encrypted BLOB NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS service_status_runs (
                    id INTEGER PRIMARY KEY,
                    shop_key TEXT NOT NULL REFERENCES managed_shops(shop_key),
                    collected_at TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    source_url TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_service_status_runs_shop ON service_status_runs(shop_key,id DESC);
                CREATE TABLE IF NOT EXISTS service_status_records (
                    run_id INTEGER NOT NULL REFERENCES service_status_runs(id),
                    account TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reply_rate TEXT NOT NULL,
                    consultation_count TEXT NOT NULL,
                    raw_status TEXT NOT NULL,
                    PRIMARY KEY(run_id,account)
                );
                CREATE TABLE IF NOT EXISTS violation_runs (
                    id INTEGER PRIMARY KEY,
                    service_run_id INTEGER NOT NULL UNIQUE REFERENCES service_status_runs(id),
                    shop_key TEXT NOT NULL REFERENCES managed_shops(shop_key),
                    collected_at TEXT NOT NULL,
                    date_range TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    source_url TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_violation_runs_shop ON violation_runs(shop_key,id DESC);
                CREATE TABLE IF NOT EXISTS violation_records (
                    run_id INTEGER NOT NULL REFERENCES violation_runs(id),
                    violation_sn TEXT NOT NULL,
                    content TEXT NOT NULL,
                    measure TEXT NOT NULL,
                    status TEXT NOT NULL,
                    order_sn TEXT NOT NULL,
                    violation_time TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    detail_url TEXT NOT NULL,
                    raw_columns TEXT NOT NULL,
                    PRIMARY KEY(run_id,violation_sn)
                );
                CREATE TABLE IF NOT EXISTS customer_service_runs (
                    id INTEGER PRIMARY KEY,
                    shop_key TEXT NOT NULL REFERENCES managed_shops(shop_key),
                    collected_at TEXT NOT NULL,
                    stat_date TEXT NOT NULL,
                    period TEXT NOT NULL CHECK(period = '近1天'),
                    experience_score REAL NOT NULL,
                    consultation_count INTEGER NOT NULL,
                    manual_required_count INTEGER NOT NULL,
                    manual_reception_count INTEGER NOT NULL,
                    reply_rate TEXT NOT NULL,
                    average_response_text TEXT NOT NULL,
                    average_response_seconds INTEGER,
                    source_url TEXT NOT NULL,
                    raw_metrics TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_customer_service_runs_shop
                    ON customer_service_runs(shop_key,id DESC);
                CREATE TABLE IF NOT EXISTS appeal_amount_runs (
                    id INTEGER PRIMARY KEY,
                    shop_key TEXT NOT NULL REFERENCES managed_shops(shop_key),
                    collected_at TEXT NOT NULL,
                    appeal_amount_cents INTEGER NOT NULL CHECK(appeal_amount_cents >= 0),
                    appeal_amount_text TEXT NOT NULL,
                    negative_amount_cents INTEGER NOT NULL CHECK(negative_amount_cents >= 0),
                    negative_amount_text TEXT NOT NULL,
                    appeal_source_url TEXT NOT NULL,
                    negative_source_url TEXT NOT NULL,
                    raw_values TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_appeal_amount_runs_shop
                    ON appeal_amount_runs(shop_key,id DESC);
                CREATE TABLE IF NOT EXISTS work_order_runs (
                    id INTEGER PRIMARY KEY,
                    shop_key TEXT NOT NULL REFERENCES managed_shops(shop_key),
                    collected_at TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    total_count INTEGER,
                    work_order_count INTEGER,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    screenshot_path TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_work_order_runs_shop
                    ON work_order_runs(shop_key,id DESC);
                CREATE TABLE IF NOT EXISTS work_order_records (
                    run_id INTEGER NOT NULL REFERENCES work_order_runs(id),
                    position INTEGER NOT NULL,
                    order_sn TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    problem TEXT NOT NULL,
                    goods_name TEXT NOT NULL,
                    goods_image_url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT NOT NULL,
                    deadline TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    PRIMARY KEY(run_id,position)
                );
                INSERT OR IGNORE INTO managed_shops(shop_key,display_name,created_at)
                    SELECT shop_key, shop_name, updated_at FROM shops;
            """)
            if "deleted_at" not in {row["name"] for row in db.execute("PRAGMA table_info(managed_shops)")}:
                db.execute("ALTER TABLE managed_shops ADD COLUMN deleted_at TEXT")
            work_order_columns = {row["name"] for row in db.execute("PRAGMA table_info(work_order_runs)")}
            if "start_date" not in work_order_columns:
                db.execute("ALTER TABLE work_order_runs ADD COLUMN start_date TEXT NOT NULL DEFAULT ''")
            if "end_date" not in work_order_columns:
                db.execute("ALTER TABLE work_order_runs ADD COLUMN end_date TEXT NOT NULL DEFAULT ''")
            if "work_order_count" not in work_order_columns:
                db.execute("ALTER TABLE work_order_runs ADD COLUMN work_order_count INTEGER")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def bind_shop(self, shop: Shop):
        with self.connect() as db:
            previous = db.execute("SELECT * FROM shops WHERE shop_key=?", (shop.key,)).fetchone()
            if previous and (previous["shop_name"], previous["account_name"]) != (shop.name, shop.account):
                raise CollectionError("当前页面店铺/账号与此店铺标识的历史记录不同。请核对登录账号，或为另一店铺使用新的标识。")
            db.execute("""INSERT INTO shops VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(shop_key) DO UPDATE SET avatar_url=excluded.avatar_url, updated_at=excluded.updated_at""",
                (shop.key, shop.name, shop.account, shop.avatar_url, now()))

    def start_run(self, shop: Shop, date_range: str, source_url: str) -> int:
        self.bind_shop(shop)
        with self.connect() as db:
            cursor = db.execute("""INSERT INTO collection_runs
                (shop_key, date_range, source_url, started_at, status) VALUES (?, ?, ?, ?, 'running')""",
                (shop.key, date_range, source_url, now()))
            return cursor.lastrowid

    def save_page(self, shop_key: str, run_id: int, records: list[Appeal], page: int, count: int):
        timestamp = now()
        with self.connect() as db:
            run = db.execute("SELECT shop_key,status FROM collection_runs WHERE id=?", (run_id,)).fetchone()
            if not run or run["shop_key"] != shop_key or run["status"] != "running":
                raise CollectionError("采集任务与店铺不匹配或已结束。")
            for record in records:
                values = record.to_dict()
                values["raw_columns"] = json.dumps(values["raw_columns"], ensure_ascii=False)
                columns = list(values)
                updates = ",".join(f"{c}=excluded.{c}" for c in columns)
                db.execute(f"""INSERT INTO appeals
                    (shop_key,{','.join(columns)},first_seen_at,last_seen_at,last_run_id)
                    VALUES ({','.join('?' for _ in range(len(columns) + 4))})
                    ON CONFLICT(shop_key,order_sn,goods_id,issue_type,created_at)
                    DO UPDATE SET {updates},last_seen_at=excluded.last_seen_at,last_run_id=excluded.last_run_id""",
                    (shop_key, *values.values(), timestamp, timestamp, run_id))
            db.execute("UPDATE collection_runs SET pages=?,row_count=? WHERE id=?", (page, count, run_id))

    def finish_run(self, run_id: int, status: str, message: str):
        if status not in ("completed", "cancelled", "failed", "partial"):
            raise ValueError(status)
        with self.connect() as db:
            db.execute("UPDATE collection_runs SET status=?,message=?,finished_at=? WHERE id=?", (status, message, now(), run_id))

    def query(self, keyword: str = "", limit: int = 500, offset: int = 0,
              shop_key: str = "", since: str = "", until: str = ""):
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        where = r"""WHERE (a.shop_key LIKE ? ESCAPE '\' OR s.shop_name LIKE ? ESCAPE '\'
            OR a.order_sn LIKE ? ESCAPE '\' OR a.goods_name LIKE ? ESCAPE '\'
            OR m.display_name LIKE ? ESCAPE '\')"""
        params = [pattern] * 5
        if shop_key:
            where += " AND a.shop_key=?"
            params.append(shop_key)
        if since:
            where += " AND a.created_at>=?"
            params.append(since + " 00:00:00")
        if until:
            where += " AND a.created_at<=?"
            params.append(until + " 23:59:59")
        joins = "JOIN shops s USING(shop_key) LEFT JOIN managed_shops m USING(shop_key)"
        with self.connect() as db:
            count = db.execute(f"SELECT COUNT(*) FROM appeals a {joins} {where}", params).fetchone()[0]
            rows = db.execute(f"""SELECT a.*,s.shop_name,s.account_name,
                COALESCE(m.display_name,s.shop_name) AS display_name,
                COALESCE(m.group_name,'未分组') AS group_name FROM appeals a
                {joins} {where} ORDER BY COALESCE(m.display_name,s.shop_name) COLLATE NOCASE,
                    a.created_at DESC,a.id DESC LIMIT ? OFFSET ?""",
                (*params, limit, offset)).fetchall()
            return count, [dict(row) for row in rows]

    def recent_runs(self, limit=20):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM collection_runs ORDER BY id DESC LIMIT ?", (limit,))]

    def add_shops(self, records: list[dict]):
        """Atomic import: never silently replace an existing shop or its identity."""
        with self.connect() as db:
            for record in records:
                username = str(record.get("username", "")).strip()
                if username and db.execute("SELECT 1 FROM shop_credentials WHERE username=?", (username,)).fetchone():
                    raise CollectionError("该登录账号已存在，请编辑原店铺；批量导入会更新原记录。")
                key = str(record.get("shop_key", "")).strip()
                name = str(record.get("display_name", "")).strip() or str(record.get("username", "")).strip()[:120] or key
                if not key or not name or len(key) > 120 or len(name) > 120:
                    raise CollectionError("店铺标识必填；标识与名称最多 120 个字符。")
                channel = record.get("channel", "chrome")
                limit = int(record.get("max_pages", 0))
                if channel not in ("chrome", "msedge", "") or not 0 <= limit <= 100000:
                    raise CollectionError("浏览器或采集页数上限无效。")
                previous = db.execute("SELECT deleted_at FROM managed_shops WHERE shop_key=?", (key,)).fetchone()
                if previous is not None and previous["deleted_at"] is None:
                    raise CollectionError(f"店铺标识已存在：{key}。请使用编辑功能，或从导入内容中移除重复项。")
                db.execute("""INSERT INTO managed_shops
                    (shop_key,display_name,group_name,enabled,max_pages,channel,created_at)
                    VALUES (?,?,?,?,?,?,?) ON CONFLICT(shop_key) DO UPDATE SET
                    display_name=excluded.display_name,group_name=excluded.group_name,enabled=excluded.enabled,
                    max_pages=excluded.max_pages,channel=excluded.channel,deleted_at=NULL,login_status='unknown',login_checked_at=NULL""", (key, name, record.get("group_name", "").strip() or "未分组",
                    int(bool(record.get("enabled", True))), limit, channel, now()))
                if record.get("username") or record.get("password"):
                    self._save_credentials(db, key, record.get("username", ""), record.get("password", ""))

    def update_shops(self, keys: list[str], **changes):
        credential_fields = {field: changes.pop(field) for field in ("username", "password") if field in changes}
        allowed = {"display_name", "group_name", "enabled", "max_pages", "channel"}
        if (not changes and not credential_fields) or set(changes) - allowed:
            raise ValueError("Unsupported shop fields")
        if "display_name" in changes and not changes["display_name"].strip():
            raise CollectionError("店铺名称不能为空。")
        if "max_pages" in changes and not 0 <= int(changes["max_pages"]) <= 100000:
            raise CollectionError("采集页数上限无效。")
        if "channel" in changes and changes["channel"] not in ("chrome", "msedge", ""):
            raise CollectionError("浏览器设置无效。")
        with self.connect() as db:
            for key in keys:
                if changes:
                    db.execute(f"UPDATE managed_shops SET {','.join(f'{k}=?' for k in changes)} WHERE shop_key=?",
                               (*changes.values(), key))
                if credential_fields:
                    self._save_credentials(db, key, credential_fields.get("username", ""), credential_fields.get("password", ""))

    def delete_shops(self, keys):
        """Remove shops and credentials from active management; retain historical ownership and jobs."""
        keys = list(dict.fromkeys(keys))
        with self.connect() as db:
            for key in keys:
                if db.execute("""SELECT 1 FROM execution_jobs WHERE shop_key=?
                    AND status IN ('pending','running','waiting_verification')""", (key,)).fetchone():
                    raise CollectionError("选中店铺还有未结束的任务，请安全停止后再删除。")
            for key in keys:
                db.execute("DELETE FROM shop_credentials WHERE shop_key=?", (key,))
                db.execute("""UPDATE managed_shops SET deleted_at=?,enabled=0,login_status='unknown',login_checked_at=NULL
                    WHERE shop_key=? AND deleted_at IS NULL""", (now(), key))

    def _save_credentials(self, db, key, username, password):
        username = username.strip()
        previous = db.execute("SELECT username,password_encrypted FROM shop_credentials WHERE shop_key=?", (key,)).fetchone()
        if not username and not password and not previous:
            return
        if not username:
            raise CollectionError("账号不能为空；已有账号不会因留空而删除。")
        if len(username) > 200:
            raise CollectionError("账号最多 200 个字符。")
        if not password and (previous is None or previous["username"] != username):
            raise CollectionError("新增或更换账号时必须输入密码。")
        duplicate = db.execute("SELECT shop_key FROM shop_credentials WHERE username=? AND shop_key<>?", (username, key)).fetchone()
        if duplicate:
            raise CollectionError("该账号已关联另一家店铺，请编辑原店铺，避免重复绑定。")
        encrypted = protect(password) if password else previous["password_encrypted"]
        changed = previous is None or previous["username"] != username or bool(password)
        db.execute("""INSERT INTO shop_credentials VALUES (?,?,?,?) ON CONFLICT(shop_key) DO UPDATE SET
            username=excluded.username,password_encrypted=excluded.password_encrypted,updated_at=excluded.updated_at""",
            (key, username, encrypted, now()))
        if changed:
            db.execute("UPDATE managed_shops SET login_status='unknown',login_checked_at=NULL WHERE shop_key=?", (key,))

    def get_credentials(self, key):
        with self.connect() as db:
            row = db.execute("SELECT username,password_encrypted FROM shop_credentials WHERE shop_key=?", (key,)).fetchone()
            if row is None:
                raise CollectionError("这家店铺尚未录入账号密码，请编辑店铺或使用账号录入功能。")
            return row["username"], unprotect(row["password_encrypted"])

    def import_accounts(self, records):
        """Atomic upsert by login account only; repeated rows use the last row's values."""
        records = normalize_accounts([[r.get("username"), r.get("password"), r.get("display_name"), r.get("group_name")]
                                      for r in records])
        keys = []
        with self.connect() as db:
            for number, record in enumerate(records, 1):
                username, password = record["username"].strip(), record["password"]
                name = record.get("display_name", "").strip()
                if not username or not password or len(name) > 120:
                    raise CollectionError(f"第 {number} 行的账号、密码或店铺名无效。")
                matched = db.execute("SELECT shop_key FROM shop_credentials WHERE username=?", (username,)).fetchone()
                if matched:
                    key = matched["shop_key"]
                else:
                    key = account_key(username)
                    old = db.execute("SELECT username FROM shop_credentials WHERE shop_key=?", (key,)).fetchone()
                    if old and old["username"] != username:
                        raise CollectionError(f"第 {number} 行店铺已绑定不同账号，请双击该店铺核对修改。")
                existing = db.execute("SELECT group_name,display_name FROM managed_shops WHERE shop_key=?", (key,)).fetchone()
                name = name or (existing["display_name"] if existing else username[:120])
                group = record.get("group_name", "").strip() or (existing[0] if existing else "未分组")
                db.execute("""INSERT INTO managed_shops(shop_key,display_name,group_name,created_at) VALUES (?,?,?,?)
                    ON CONFLICT(shop_key) DO UPDATE SET display_name=excluded.display_name,group_name=excluded.group_name,
                    enabled=CASE WHEN managed_shops.deleted_at IS NOT NULL THEN 1 ELSE managed_shops.enabled END,deleted_at=NULL""",
                    (key, name, group, now()))
                self._save_credentials(db, key, username, password)
                keys.append(key)
        return keys

    def set_login_status(self, key, status):
        if status not in ("verified", "needs_login", "unknown"):
            raise ValueError(status)
        with self.connect() as db:
            db.execute("UPDATE managed_shops SET login_status=?,login_checked_at=? WHERE shop_key=?", (status, now(), key))

    def managed_shops(self):
        with self.connect() as db:
            rows = db.execute("""SELECT m.*,s.shop_name,s.account_name,c.username,
                CASE WHEN c.shop_key IS NULL THEN 0 ELSE 1 END AS has_password,
                j.status AS last_status,j.created_at AS last_activity,j.message AS last_message,
                j.row_count AS last_count,j.mode AS last_mode,
                (SELECT COUNT(*) FROM appeals a WHERE a.shop_key=m.shop_key) AS record_count
                FROM managed_shops m LEFT JOIN shops s USING(shop_key)
                LEFT JOIN shop_credentials c USING(shop_key)
                LEFT JOIN execution_jobs j ON j.id=(SELECT MAX(id) FROM execution_jobs WHERE shop_key=m.shop_key)
                WHERE m.deleted_at IS NULL ORDER BY m.group_name,m.display_name,m.shop_key""").fetchall()
            return [dict(row) for row in rows]

    def report_shops(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT shop_key,display_name,deleted_at FROM managed_shops m
                WHERE deleted_at IS NULL OR EXISTS(SELECT 1 FROM appeals a WHERE a.shop_key=m.shop_key)
                ORDER BY display_name,shop_key""")]

    def shop_exists(self, key):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM managed_shops WHERE shop_key=?", (key,)).fetchone() is not None

    def create_jobs(self, batch_id, keys, mode="collect"):
        if mode not in ("collect", "login", "login_check", "account_login", "service_status",
                        "customer_service", "appeal_amount", "work_order_screenshot"):
            raise ValueError(mode)
        with self.connect() as db:
            result = []
            for key in dict.fromkeys(keys):
                row = db.execute("SELECT * FROM managed_shops WHERE shop_key=?", (key,)).fetchone()
                if row is None or row["deleted_at"] is not None or (mode in (
                        "collect", "login_check", "account_login", "service_status",
                        "customer_service", "appeal_amount", "work_order_screenshot") and not row["enabled"]):
                    continue
                cursor = db.execute("""INSERT INTO execution_jobs(batch_id,shop_key,mode,status,created_at)
                    VALUES (?,?,?,'pending',?)""", (batch_id, key, mode, now()))
                result.append({**dict(row), "job_id": cursor.lastrowid})
        return result

    def save_service_status(self, key, records, source_url):
        """Commit a complete snapshot, including empty results; failures retain the previous snapshot."""
        with self.connect() as db:
            return self._insert_service_status(db, key, records, source_url, now())

    @staticmethod
    def _insert_service_status(db, key, records, source_url, collected_at):
        run_id = db.execute("""INSERT INTO service_status_runs(shop_key,collected_at,row_count,source_url)
            VALUES (?,?,?,?)""", (key, collected_at, len(records), source_url)).lastrowid
        for record in records:
            db.execute("INSERT INTO service_status_records VALUES (?,?,?,?,?,?)",
                       (run_id, record['account'], record['status'], record['reply_rate'],
                        record['consultation_count'], json.dumps(record['raw_status'], ensure_ascii=False)))
        return run_id

    def save_operational_snapshot(self, key, service_records, service_url, violations, violation_url, date_range):
        """Atomically save matching customer-service and violation snapshots for one shop."""
        collected_at = now()
        with self.connect() as db:
            service_run_id = self._insert_service_status(db, key, service_records, service_url, collected_at)
            violation_run_id = db.execute("""INSERT INTO violation_runs
                (service_run_id,shop_key,collected_at,date_range,row_count,source_url)
                VALUES (?,?,?,?,?,?)""", (service_run_id, key, collected_at, date_range,
                                            len(violations), violation_url)).lastrowid
            for record in violations:
                db.execute("""INSERT INTO violation_records
                    (run_id,violation_sn,content,measure,status,order_sn,violation_time,
                     operation,detail_url,raw_columns) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (violation_run_id, record['violation_sn'], record['content'], record['measure'],
                     record['status'], record['order_sn'], record['violation_time'],
                     record['operation'], record['detail_url'],
                     json.dumps(record['raw_columns'], ensure_ascii=False)))
            return service_run_id, violation_run_id

    def latest_service_status(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("""SELECT m.shop_key,m.display_name,m.group_name,
                p.shop_name,p.account_name,r.collected_at,r.row_count,s.account,s.status,s.reply_rate,s.consultation_count,
                COALESCE(v.row_count,0) AS violation_count,v.collected_at AS violation_collected_at
                FROM managed_shops m JOIN service_status_runs r ON r.id=(
                    SELECT MAX(id) FROM service_status_runs WHERE shop_key=m.shop_key)
                LEFT JOIN service_status_records s ON s.run_id=r.id
                LEFT JOIN shops p ON p.shop_key=m.shop_key
                LEFT JOIN violation_runs v ON v.service_run_id=r.id
                WHERE m.deleted_at IS NULL ORDER BY m.group_name,m.display_name,s.account""")]

    def latest_violations(self):
        """Return each active shop's latest complete 90-day snapshot, including explicit empty runs."""
        with self.connect() as db:
            return [dict(r) for r in db.execute("""SELECT m.shop_key,m.display_name,m.group_name,
                p.shop_name,p.account_name,r.collected_at,r.date_range,r.row_count,
                v.violation_sn,v.content,v.measure,v.status,v.order_sn,v.violation_time,
                v.operation,v.detail_url
                FROM managed_shops m JOIN violation_runs r ON r.id=(
                    SELECT MAX(id) FROM violation_runs WHERE shop_key=m.shop_key)
                LEFT JOIN violation_records v ON v.run_id=r.id
                LEFT JOIN shops p ON p.shop_key=m.shop_key
                WHERE m.deleted_at IS NULL ORDER BY m.group_name,m.display_name,v.violation_time DESC,v.violation_sn""")]

    def save_customer_service(self, key, record, source_url):
        with self.connect() as db:
            return db.execute("""INSERT INTO customer_service_runs
                (shop_key,collected_at,stat_date,period,experience_score,consultation_count,
                 manual_required_count,manual_reception_count,reply_rate,average_response_text,
                 average_response_seconds,source_url,raw_metrics)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, now(), record['stat_date'], record['period'], record['experience_score'],
                 record['consultation_count'], record['manual_required_count'],
                 record['manual_reception_count'], record['reply_rate'],
                 record['average_response_text'], record['average_response_seconds'], source_url,
                 json.dumps(record['raw_metrics'], ensure_ascii=False))).lastrowid

    def latest_customer_service(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("""SELECT m.shop_key,m.display_name,m.group_name,
                p.shop_name,p.account_name,r.*
                FROM managed_shops m JOIN customer_service_runs r ON r.id=(
                    SELECT MAX(id) FROM customer_service_runs WHERE shop_key=m.shop_key)
                LEFT JOIN shops p ON p.shop_key=m.shop_key
                WHERE m.deleted_at IS NULL ORDER BY m.group_name,m.display_name""")]

    def save_appeal_amount(self, key, record, appeal_source_url, negative_source_url):
        with self.connect() as db:
            return db.execute("""INSERT INTO appeal_amount_runs
                (shop_key,collected_at,appeal_amount_cents,appeal_amount_text,
                 negative_amount_cents,negative_amount_text,appeal_source_url,
                 negative_source_url,raw_values) VALUES (?,?,?,?,?,?,?,?,?)""",
                (key, now(), record['appeal_amount_cents'], record['appeal_amount_text'],
                 record['negative_amount_cents'], record['negative_amount_text'], appeal_source_url,
                 negative_source_url, json.dumps(record['raw_values'], ensure_ascii=False))).lastrowid

    def latest_appeal_amounts(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("""SELECT m.shop_key,m.display_name,m.group_name,
                p.shop_name,p.account_name,r.*
                FROM managed_shops m JOIN appeal_amount_runs r ON r.id=(
                    SELECT MAX(id) FROM appeal_amount_runs WHERE shop_key=m.shop_key)
                LEFT JOIN shops p ON p.shop_key=m.shop_key
                WHERE m.deleted_at IS NULL ORDER BY m.group_name,m.display_name""")]

    def save_work_order_snapshot(self, key, work_order_count, source_url, screenshot_path,
                                 start_date="", end_date=""):
        """Save one screenshot and its queried work-order count."""
        if not isinstance(work_order_count, int) or work_order_count < 0:
            raise CollectionError("工单数量必须是大于或等于 0 的整数。")
        with self.connect() as db:
            run_id = db.execute("""INSERT INTO work_order_runs
                (shop_key,collected_at,row_count,total_count,work_order_count,start_date,end_date,
                 source_url,screenshot_path) VALUES (?,?,?,?,?,?,?,?,?)""",
                (key, now(), work_order_count, None, work_order_count, start_date, end_date,
                 source_url, screenshot_path)).lastrowid
            return run_id

    def latest_work_orders(self):
        """Return one summary row for each active shop's latest work-order screenshot."""
        with self.connect() as db:
            return [dict(r) for r in db.execute("""SELECT m.shop_key,m.display_name,m.group_name,
                p.shop_name,p.account_name,r.collected_at,r.work_order_count,r.start_date,
                r.end_date,r.source_url,r.screenshot_path
                FROM managed_shops m JOIN work_order_runs r ON r.id=(
                    SELECT MAX(id) FROM work_order_runs WHERE shop_key=m.shop_key)
                LEFT JOIN shops p ON p.shop_key=m.shop_key
                WHERE m.deleted_at IS NULL
                ORDER BY m.group_name,m.display_name""")]

    def update_job(self, job_id, status=None, message=None, pages=None, count=None):
        changes = {}
        if status is not None:
            if status not in ("pending", "running", "waiting_verification", "completed", "partial", "failed", "cancelled", "needs_login", "authenticated", "interrupted"):
                raise ValueError(status)
            changes["status"] = status
            if status == "running":
                changes["started_at"] = now()
            elif status not in ("pending", "waiting_verification"):
                changes["finished_at"] = now()
        for field, value in (("message", message), ("pages", pages), ("row_count", count)):
            if value is not None:
                changes[field] = value
        if changes:
            with self.connect() as db:
                db.execute(f"UPDATE execution_jobs SET {','.join(f'{k}=?' for k in changes)} WHERE id=?", (*changes.values(), job_id))

    def jobs(self, batch_id=None, limit=500):
        with self.connect() as db:
            where = "WHERE j.batch_id=?" if batch_id else ""
            params = (batch_id, limit) if batch_id else (limit,)
            return [dict(row) for row in db.execute(f"""SELECT j.*,m.display_name,m.group_name
                FROM execution_jobs j JOIN managed_shops m USING(shop_key) {where}
                ORDER BY j.id DESC LIMIT ?""", params)]

    def latest_collection_jobs(self, mode="collect"):
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT j.* FROM managed_shops m
                JOIN execution_jobs j ON j.id=(SELECT MAX(id) FROM execution_jobs
                    WHERE shop_key=m.shop_key AND mode=?) WHERE m.deleted_at IS NULL ORDER BY j.id DESC""", (mode,))]

    def recover_interrupted_jobs(self):
        """Called at app startup under the single-instance lock; do not silently resume."""
        with self.connect() as db:
            db.execute("""UPDATE execution_jobs SET status='interrupted',finished_at=?,
                message='上次程序退出时任务未结束，可选择失败重试。' WHERE status IN ('pending','running','waiting_verification')""", (now(),))
            db.execute("""UPDATE collection_runs SET status='failed',finished_at=?,
                message='上次程序退出时采集未结束，已完成页面保留。' WHERE status='running'""", (now(),))
