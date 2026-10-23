import unittest
from unittest.mock import MagicMock, patch

import stealth


class BusinessPacingTests(unittest.TestCase):
    def test_inactive_calls_add_no_wait(self):
        page = MagicMock()
        stealth.business_pause(page)
        stealth.business_read_page(page)
        page.wait_for_timeout.assert_not_called()
        page.evaluate_handle.assert_not_called()

    def test_nested_scope_and_exception_restore_state(self):
        seen = []

        @stealth.paced_business_operations
        def inner():
            seen.append(stealth._business_pace.get())
            raise ValueError("stop")

        @stealth.paced_business_operations
        def outer():
            seen.append(stealth._business_pace.get())
            inner()

        with self.assertRaises(ValueError):
            outer()
        self.assertEqual(seen[0], seen[1])
        self.assertIsNone(stealth._business_pace.get())

    def test_validation_scope_suspends_and_restores_business_waits(self):
        page = MagicMock()

        @stealth.without_business_pacing
        def validate():
            stealth.business_pause(page)
            self.assertIsNone(stealth._business_pace.get())

        @stealth.paced_business_operations
        def run():
            pace = stealth._business_pace.get()
            validate()
            page.wait_for_timeout.assert_not_called()
            self.assertEqual(stealth._business_pace.get(), pace)
            stealth.business_pause(page, reading=True)

        run()
        self.assertEqual(page.wait_for_timeout.call_count, 1)
        self.assertLessEqual(page.wait_for_timeout.call_args.args[0], 8000)

    def test_scroll_preserves_requested_distance_both_directions(self):
        with patch("stealth.time.sleep"):
            for distance in (525, -450, 0):
                page = MagicMock()
                stealth.human_scroll(page, distance)
                moved = sum(c.args[1] for c in page.mouse.wheel.call_args_list)
                self.assertAlmostEqual(moved, distance)

    def test_each_pause_stays_bounded(self):
        page = MagicMock()

        @stealth.paced_business_operations
        def run():
            for _ in range(50):
                stealth.business_pause(page)

        run()
        values = [c.args[0] for c in page.wait_for_timeout.call_args_list]
        self.assertEqual(len(values), 50)
        self.assertTrue(all(1200 <= value <= 8000 for value in values))

    def test_read_page_restores_position_and_disposes_handle(self):
        for top in (0, 980):
            page = MagicMock()
            handle = page.evaluate_handle.return_value
            handle.evaluate.return_value = {"top": top, "maximum": 1000}
            stealth.paced_business_operations(stealth.business_read_page)(page)
            positions = [c.args[1] for c in handle.evaluate.call_args_list if len(c.args) == 2]
            self.assertEqual(len(positions), 9)
            self.assertEqual(positions[-1], top)
            self.assertTrue(all(0 <= value <= 1000 for value in positions))
            self.assertNotEqual(positions[3], top)
            handle.dispose.assert_called_once()
            page.mouse.wheel.assert_not_called()

    def test_read_page_skips_modal_and_non_scrollable_document(self):
        for info in (None, {"top": 0, "maximum": 0}):
            page = MagicMock()
            handle = page.evaluate_handle.return_value
            handle.evaluate.return_value = info
            stealth.paced_business_operations(stealth.business_read_page)(page)
            self.assertEqual(handle.evaluate.call_count, 1)
            handle.dispose.assert_called_once()

    def test_read_page_restores_and_disposes_when_wait_fails(self):
        page = MagicMock()
        handle = page.evaluate_handle.return_value
        handle.evaluate.return_value = {"top": 100, "maximum": 1000}
        page.wait_for_timeout.side_effect = [None, RuntimeError("closed")]
        with self.assertRaisesRegex(RuntimeError, "closed"):
            stealth.paced_business_operations(stealth.business_read_page)(page)
        self.assertEqual(handle.evaluate.call_args.args[1], 100)
        handle.dispose.assert_called_once()

    def test_typing_is_slower_only_in_business_scope(self):
        page = MagicMock()
        with patch("stealth.time.sleep"), patch("stealth.random.randint", return_value=40):
            stealth.human_type(page, MagicMock(), "a", click_first=False)
            self.assertEqual(page.keyboard.type.call_args.kwargs["delay"], 40)
            stealth.paced_business_operations(stealth.human_type)(
                page, MagicMock(), "a", click_first=False)
            self.assertGreaterEqual(page.keyboard.type.call_args.kwargs["delay"], 160)


if __name__ == "__main__":
    unittest.main()
