"""Persistent account list. Passwords are stored only as Windows DPAPI blobs."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .account_import import AccountRecord, assign_account_shop_names
from .credentials import protect, unprotect

ACCOUNT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "state" / "shop_accounts.db"


@contextmanager
def _connect():
    ACCOUNT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(ACCOUNT_DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS shop_accounts (
                username TEXT PRIMARY KEY,
                password_encrypted BLOB NOT NULL,
                shop TEXT NOT NULL UNIQUE,
                group_name TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        with connection:
            yield connection
    finally:
        connection.close()


def list_accounts() -> list[AccountRecord]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT username,password_encrypted,shop,group_name FROM shop_accounts ORDER BY group_name,shop"
        ).fetchall()
    result = []
    for row in rows:
        record = AccountRecord(
            username=row["username"],
            password=unprotect(row["password_encrypted"]),
            shop=row["shop"],
            group_name=row["group_name"],
        )
        result.append(record)
    return result


def replace_accounts(records: list[AccountRecord]) -> list[AccountRecord]:
    normalized = assign_account_shop_names(records)
    encrypted = [(record, protect(record.password)) for record in normalized]
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")
    with _connect() as connection:
        connection.execute("DELETE FROM shop_accounts")
        connection.executemany(
            "INSERT INTO shop_accounts(username,password_encrypted,shop,group_name,updated_at) VALUES (?,?,?,?,?)",
            [(record.username, blob, record.shop, record.group_name, timestamp) for record, blob in encrypted],
        )
    return normalized


def delete_by_shop(shop: str) -> None:
    with _connect() as connection:
        connection.execute("DELETE FROM shop_accounts WHERE shop=?", (shop,))
