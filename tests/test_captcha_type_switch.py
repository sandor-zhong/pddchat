import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import captcha


class CaptchaTypeSwitchTest(unittest.TestCase):
    def setUp(self):
        recording = patch("captcha.captcha_diagnostics.save_attempt")
        recording.start()
        self.addCleanup(recording.stop)

    def run_sequence(self, sequence, success=False):
        state = {"index": 0}
        calls = []

        def solve(kind):
            calls.append(kind)
            state["index"] += 1
            return success and state["index"] == len(sequence)

        with ExitStack() as stack:
            stack.enter_context(patch.object(captcha, "_auto_solve_captcha", True))
            stack.enter_context(patch.object(captcha, "_give_up_after_auto_solve_fails", True))
            stack.enter_context(patch.object(captcha, "is_captcha_visible", return_value=True))
            for kind in ("text", "picture", "rotate"):
                stack.enter_context(patch.object(
                    captcha, f"is_{kind}_captcha",
                    side_effect=lambda page, kind=kind: sequence[min(state["index"], len(sequence)-1)] == kind,
                ))
                stack.enter_context(patch.object(
                    captcha, f"try_auto_solve_{kind}_captcha",
                    side_effect=lambda page, kind=kind: solve(kind),
                ))
            for name in ("_captcha_diagnostic", "save_captcha_screenshot", "jitter_wait", "_refresh_visible_captcha"):
                stack.enter_context(patch.object(captcha, name))
            human = stack.enter_context(patch.object(captcha, "wait_for_human_captcha", return_value=True))
            error = None
            try:
                captcha.ensure_no_captcha(MagicMock())
            except RuntimeError as exc:
                error = str(exc)
        return calls, human.call_count, error

    def test_switches_between_all_types_until_success(self):
        sequence = ["text", "picture", "rotate", "text"]
        self.assertEqual(self.run_sequence(sequence, success=True), (sequence, 0, None))

    def test_mixed_types_share_five_attempts_and_skip_order(self):
        sequence = ["rotate", "text", "picture", "text", "rotate", "picture"]
        calls, human_calls, error = self.run_sequence(sequence)
        self.assertEqual(calls, sequence[:5])
        self.assertEqual(human_calls, 0)
        self.assertIn("5 次均失败", error)
