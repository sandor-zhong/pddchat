from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import appeal_submit
import main


class _FakePage:
    def __init__(self, url: str, *, closed: bool = False) -> None:
        self.url = url
        self._closed = closed
        self.close_calls = 0

    def is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self.close_calls += 1
        self._closed = True


class _FakeContext:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = pages
        self.created: list[_FakePage] = []

    def new_page(self) -> _FakePage:
        page = _FakePage("about:blank")
        self.pages.append(page)
        self.created.append(page)
        return page


class BatchAppealPageReuseTest(unittest.TestCase):
    def test_role_page_reuses_matching_page_in_same_context(self) -> None:
        chat_page = _FakePage("https://mms.pinduoduo.com/mms-chat/search")
        appeal_page = _FakePage(
            "https://mms.pinduoduo.com/aftersales/customer_complain_appeal"
        )
        context = _FakeContext([chat_page, appeal_page])

        result = main._get_or_create_role_page(
            context,
            None,
            main._APPEAL_PAGE_FRAGMENT,
            excluded_pages=(chat_page,),
        )

        self.assertIs(result, appeal_page)
        self.assertEqual(context.created, [])

    def test_role_page_recreates_only_missing_role(self) -> None:
        chat_page = _FakePage("https://mms.pinduoduo.com/mms-chat/search")
        closed_appeal_page = _FakePage(
            "https://mms.pinduoduo.com/aftersales/customer_complain_appeal",
            closed=True,
        )
        context = _FakeContext([chat_page, closed_appeal_page])

        result = main._get_or_create_role_page(
            context,
            closed_appeal_page,
            main._APPEAL_PAGE_FRAGMENT,
            excluded_pages=(chat_page,),
        )

        self.assertIs(result, context.created[0])
        self.assertFalse(chat_page.is_closed())

    def test_cleanup_keeps_persistent_tabs_and_closes_transient_tab(self) -> None:
        chat_page = _FakePage("https://mms.pinduoduo.com/mms-chat/search")
        appeal_page = _FakePage(
            "https://mms.pinduoduo.com/aftersales/customer_complain_appeal"
        )
        detail_page = _FakePage(
            "https://mms.pinduoduo.com/aftersales/customer_complain_appeal/detail"
        )
        context = _FakeContext([chat_page, appeal_page, detail_page])

        main._close_pages_opened_since(
            context,
            {id(chat_page), id(appeal_page)},
            protected_pages=(chat_page, appeal_page),
        )

        self.assertFalse(chat_page.is_closed())
        self.assertFalse(appeal_page.is_closed())
        self.assertTrue(detail_page.is_closed())

    def test_order_processing_routes_analysis_and_submit_to_separate_tabs(self) -> None:
        context = MagicMock()
        chat_page = MagicMock(name="chat_page")
        appeal_page = MagicMock(name="appeal_page")

        with (
            patch.object(main, "_order_has_analysis", return_value=True),
            patch.object(
                main,
                "submit_single_order",
                return_value=("ORDER-1", "已提交"),
            ) as submit,
        ):
            result = main.process_and_submit_one_order(
                context,
                chat_page,
                "ORDER-1",
                {},
                confirm=True,
                appeal_page=appeal_page,
            )

        self.assertEqual(result, ("ORDER-1", "已提交"))
        submit.assert_called_once_with(
            context,
            appeal_page,
            "ORDER-1",
            {},
            True,
        )

    def test_open_appeal_search_page_reuses_ready_page_without_goto(self) -> None:
        page = MagicMock()
        page.locator.return_value.first.is_visible.return_value = False
        page.url = (
            "https://mms.pinduoduo.com/aftersales/customer_complain_appeal"
        )
        order_input = MagicMock()

        with (
            patch.object(appeal_submit, "get_order_sn_input", return_value=order_input),
            patch.object(appeal_submit, "clear_blockers") as clear_blockers,
        ):
            appeal_submit.open_appeal_search_page(page)

        order_input.wait_for.assert_called_once_with(
            state="visible",
            timeout=1_500,
        )
        page.goto.assert_not_called()
        clear_blockers.assert_called_once_with(page)


if __name__ == "__main__":
    unittest.main()
