from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from stealth import STABLE_FINGERPRINT_INIT_SCRIPT, create_stealth_context


class StableFingerprintTests(unittest.TestCase):
    def test_script_does_not_randomize_device_fingerprint(self) -> None:
        forbidden = (
            "Math.random",
            "RTX 3060",
            "CanvasRenderingContext2D",
            "getShaderPrecisionFormat",
            "hardwareConcurrency",
            "deviceMemory",
            "navigator.plugins",
        )
        for marker in forbidden:
            self.assertNotIn(marker, STABLE_FINGERPRINT_INIT_SCRIPT)

    def test_default_context_uses_native_window_and_no_fake_location(self) -> None:
        context = MagicMock()
        playwright = MagicMock()
        playwright.chromium.launch_persistent_context.return_value = context

        result = create_stealth_context(playwright, "profile")

        self.assertIs(result, context)
        kwargs = playwright.chromium.launch_persistent_context.call_args.kwargs
        self.assertTrue(kwargs["no_viewport"])
        self.assertNotIn("viewport", kwargs)
        self.assertNotIn("geolocation", kwargs)
        self.assertNotIn("permissions", kwargs)
        self.assertNotIn("device_scale_factor", kwargs)
        self.assertNotIn("user_agent", kwargs)
        context.add_init_script.assert_called_once_with(
            STABLE_FINGERPRINT_INIT_SCRIPT
        )

    def test_explicit_viewport_and_user_agent_are_still_supported(self) -> None:
        context = MagicMock()
        playwright = MagicMock()
        playwright.chromium.launch_persistent_context.return_value = context

        create_stealth_context(
            playwright,
            "profile",
            viewport={"width": 1440, "height": 900},
            user_agent="custom-agent",
        )

        kwargs = playwright.chromium.launch_persistent_context.call_args.kwargs
        self.assertEqual(kwargs["viewport"], {"width": 1440, "height": 900})
        self.assertFalse(kwargs["no_viewport"])
        self.assertEqual(kwargs["user_agent"], "custom-agent")


if __name__ == "__main__":
    unittest.main()
