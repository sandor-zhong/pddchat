from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import db


class ShopMaxOrdersDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_path = db.DB_PATH
        self._old_connection = db._connection
        if db._connection is not None:
            db._connection.close()
        self._temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self._temp_dir.name) / "test.db"
        db._connection = None

    def tearDown(self) -> None:
        if db._connection is not None:
            db._connection.close()
        db._connection = self._old_connection
        db.DB_PATH = self._old_path
        self._temp_dir.cleanup()

    def test_batch_sets_deduplicated_shops_atomically(self) -> None:
        count = db.set_shop_max_orders_batch(["店铺A", "店铺B", "店铺A"], 35)

        self.assertEqual(count, 2)
        self.assertEqual(db.list_shop_max_orders(), {"店铺A": 35, "店铺B": 35})

    def test_batch_can_restore_selected_shops_to_default(self) -> None:
        db.set_shop_max_orders_batch(["店铺A", "店铺B"], 50)

        count = db.set_shop_max_orders_batch(["店铺A"], None)

        self.assertEqual(count, 1)
        self.assertEqual(db.list_shop_max_orders(), {"店铺B": 50})

    def test_group_and_max_orders_updates_preserve_each_other(self) -> None:
        db.set_shop_group("店铺A", " 组1 ")
        db.set_shop_max_orders("店铺A", 35)

        self.assertEqual(db.get_shop_group("店铺A"), "组1")
        self.assertEqual(db.get_shop_max_orders("店铺A"), 35)

        db.set_shop_max_orders("店铺A", None)
        self.assertEqual(db.get_shop_group("店铺A"), "组1")
        self.assertIsNone(db.get_shop_max_orders("店铺A"))

        db.set_shop_max_orders("店铺A", 50)
        db.set_shop_group("店铺A", None)
        self.assertIsNone(db.get_shop_group("店铺A"))
        self.assertEqual(db.get_shop_max_orders("店铺A"), 50)

    def test_batch_group_assignment_deduplicates_and_can_clear(self) -> None:
        count = db.set_shop_group_batch(["店铺A", "店铺B", "店铺A"], " 组1 ")

        self.assertEqual(count, 2)
        self.assertEqual(db.list_shop_groups(), {"店铺A": "组1", "店铺B": "组1"})

        count = db.set_shop_group_batch(["店铺A"], None)
        self.assertEqual(count, 1)
        self.assertEqual(db.list_shop_groups(), {"店铺B": "组1"})

    def test_existing_shop_settings_table_is_migrated(self) -> None:
        connection = sqlite3.connect(db.DB_PATH)
        connection.execute(
            "CREATE TABLE shop_settings ("
            "shop TEXT NOT NULL PRIMARY KEY, max_orders INTEGER, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO shop_settings (shop, max_orders, updated_at) VALUES (?, ?, ?)",
            ("店铺A", 20, "2026-09-01T00:00:00+00:00"),
        )
        connection.commit()
        connection.close()

        db.set_shop_group("店铺A", "组1")

        self.assertEqual(db.get_shop_group("店铺A"), "组1")
        self.assertEqual(db.get_shop_max_orders("店铺A"), 20)


if __name__ == "__main__":
    unittest.main()
