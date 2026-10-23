import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from openpyxl import Workbook

from gui.app.services import account_store, shop_repo
from gui.app.services.account_import import (
    AccountRecord,
    assign_account_shop_names,
    parse_pasted_accounts,
    read_excel_accounts,
)
from gui.app.services.credentials import CredentialError


class ExcelShopImportTest(unittest.TestCase):
    def test_finds_headers_skips_blanks_and_distinguishes_subaccounts(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "accounts.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["子账号", "密码"])
            sheet.append([None, None])
            sheet.append(["示例店铺:1", "secret-one"])
            sheet.append(["示例店铺:2", "secret-two"])
            sheet.append(["另一店铺", "secret-three"])
            workbook.save(path)
            workbook.close()

            rows = read_excel_accounts(path)

            self.assertEqual(len(rows), 3)
            self.assertEqual(len({row.shop for row in rows}), 3)
            self.assertTrue(all(":" not in row.shop for row in rows))
            self.assertEqual([row.username for row in rows], ["示例店铺:1", "示例店铺:2", "另一店铺"])

    def test_rejects_incomplete_credential_row(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "accounts.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["账号", "密码"])
            sheet.append(["only-account", None])
            workbook.save(path)
            workbook.close()

            with self.assertRaisesRegex(ValueError, "必须同时填写"):
                read_excel_accounts(path)

    def test_import_registry_contains_status_but_no_credentials(self):
        with TemporaryDirectory() as folder:
            registry = Path(folder) / "imported_shops.json"
            original = shop_repo.IMPORTED_SHOPS_PATH
            try:
                shop_repo.IMPORTED_SHOPS_PATH = registry
                shop_repo.register_imported_shops(["店铺A"])
                shop_repo.mark_imported_shop_login("店铺A", False, "等待短信验证")
                saved = registry.read_text(encoding="utf-8")
            finally:
                shop_repo.IMPORTED_SHOPS_PATH = original
            self.assertIn("店铺A", saved)
            self.assertIn("needs_login", saved)
            self.assertNotIn("password", saved.lower())
            self.assertNotIn("username", saved.lower())

    def test_paste_accepts_reordered_headers(self):
        records = parse_pasted_accounts("店铺名\t密码\t账号\t分组\n店铺A\tsecret\taccount-a\t一组")
        self.assertEqual(records[0], AccountRecord("account-a", "secret", "店铺A", "一组"))

    def test_duplicate_account_updates_password_and_preserves_blank_optional_fields(self):
        rows = assign_account_shop_names([
            AccountRecord("account-a", "old", "店铺A", "一组"),
            AccountRecord("account-a", "new", "", ""),
        ])
        self.assertEqual(rows, [AccountRecord("account-a", "new", "店铺A", "一组")])

    def test_saved_password_is_dpapi_encrypted(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "accounts.db"
            original = account_store.ACCOUNT_DB_PATH
            try:
                account_store.ACCOUNT_DB_PATH = database
                secret = "sensitive-password-测试"
                try:
                    account_store.replace_accounts([AccountRecord("account-a", secret, "店铺A", "一组")])
                except CredentialError:
                    self.skipTest("Windows DPAPI user profile is unavailable in this sandbox")
                self.assertNotIn(secret.encode("utf-8"), database.read_bytes())
                loaded = account_store.list_accounts()
            finally:
                account_store.ACCOUNT_DB_PATH = original
            self.assertEqual(loaded[0].password, secret)
            loaded[0].clear_password()


if __name__ == "__main__":
    unittest.main()
