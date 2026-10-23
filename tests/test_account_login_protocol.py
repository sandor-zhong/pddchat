import io
import json
import unittest
from unittest.mock import patch

import main


class _Context:
    def __init__(self):
        self.pages = [object()]
        self.closed = False

    def close(self):
        self.closed = True


class AccountLoginProtocolTest(unittest.TestCase):
    def test_credentials_use_stdin_and_never_appear_in_protocol_output(self):
        secret = "do-not-log-this-password"
        source = io.StringIO(json.dumps({"cmd": "login", "username": "account", "password": secret}) + "\n")
        target = io.StringIO()
        context = _Context()

        with (
            patch.object(main.sys, "stdin", source),
            patch.object(main, "create_stealth_context", return_value=context),
            patch.object(
                main,
                "account_login",
                side_effect=lambda _context, _page, _username, _password, _stop, progress: (
                    progress("正在验证"),
                    (True, "登录成功"),
                )[1],
            ),
        ):
            main.run_account_login_mode(object(), target)

        output = target.getvalue()
        self.assertNotIn(secret, output)
        messages = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(messages[-1]["type"], "login_result")
        self.assertTrue(messages[-1]["success"])
        self.assertTrue(context.closed)


if __name__ == "__main__":
    unittest.main()
