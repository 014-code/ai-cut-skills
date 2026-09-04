from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock


SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))

from usergrowth_automation.usergrowth_browser import UserGrowthBrowserClient  # noqa: E402


class _Marker:
    def __init__(self, count: int) -> None:
        self._count = count

    async def count(self) -> int:
        return self._count


class _Option:
    def __init__(self, *, class_name: str = "", aria_selected: str | None = None, visible_check: bool = False) -> None:
        self.class_name = class_name
        self.aria_selected = aria_selected
        self.visible_check = visible_check

    async def get_attribute(self, name: str) -> str | None:
        if name == "class":
            return self.class_name
        if name == "aria-selected":
            return self.aria_selected
        return None

    def locator(self, selector: str) -> _Marker:
        return _Marker(1 if self.visible_check and ":visible" in selector else 0)


class DeliveryFieldSelectionTests(unittest.IsolatedAsyncioTestCase):
    def _client(self) -> UserGrowthBrowserClient:
        client = UserGrowthBrowserClient.__new__(UserGrowthBrowserClient)
        client._raise_if_cancelled = lambda: None
        client._write_run_log = lambda *_args, **_kwargs: None
        client._snapshot = AsyncMock()
        return client

    async def test_hidden_check_icon_is_not_treated_as_selected(self) -> None:
        client = self._client()

        self.assertFalse(await client._dropdown_option_selected(_Option(visible_check=False)))
        self.assertTrue(await client._dropdown_option_selected(_Option(visible_check=True)))

    async def test_explicitly_selected_option_is_not_clicked_again(self) -> None:
        client = self._client()

        self.assertTrue(await client._dropdown_option_selected(_Option(aria_selected="true")))
        self.assertTrue(await client._dropdown_option_selected(_Option(class_name="arco-select-option-selected")))

    async def test_multiple_platform_values_are_selected_and_dropdown_is_closed(self) -> None:
        client = self._client()
        page = object()
        selected: set[str] = set()

        async def has_value(_page, _field: str, value: str) -> bool:
            return value in selected

        async def select_value(_page, field: str, value: str, **kwargs) -> bool:
            self.assertEqual(field, "投放平台")
            self.assertTrue(kwargs["keep_dropdown_open"])
            selected.add(value)
            return True

        client._clear_delivery_field_values = AsyncMock()
        client._delivery_field_has_value = AsyncMock(side_effect=has_value)
        client._ensure_delivery_field_value = AsyncMock(side_effect=select_value)
        client._close_open_delivery_dropdown_if_needed = AsyncMock()

        result = await client._ensure_delivery_field_values(
            page,
            "投放平台",
            ["广点通", "头条内广", "穿山甲联盟"],
        )

        self.assertTrue(result)
        self.assertEqual(selected, {"广点通", "头条内广", "穿山甲联盟"})
        client._clear_delivery_field_values.assert_awaited_once_with(page, "投放平台")
        self.assertEqual(
            [row.args[2] for row in client._ensure_delivery_field_value.await_args_list],
            ["广点通", "头条内广", "穿山甲联盟"],
        )
        client._close_open_delivery_dropdown_if_needed.assert_awaited_once()

    async def test_failed_option_selection_stops_at_attempt_limit(self) -> None:
        client = self._client()
        page = type("Page", (), {"wait_for_timeout": AsyncMock()})()
        client._delivery_field_has_value = AsyncMock(return_value=False)
        client._delivery_dropdown_opened = AsyncMock(return_value=True)
        client._type_into_open_dropdown = AsyncMock()
        client._click_dropdown_option = AsyncMock(return_value=False)

        result = await client._ensure_delivery_field_value(
            page,
            "投放平台",
            "广点通",
            keep_dropdown_open=True,
            max_attempts=1,
        )

        self.assertFalse(result)
        client._click_dropdown_option.assert_awaited_once_with(page, "广点通")
        client._type_into_open_dropdown.assert_awaited_once_with(page, "广点通")


if __name__ == "__main__":
    unittest.main()
