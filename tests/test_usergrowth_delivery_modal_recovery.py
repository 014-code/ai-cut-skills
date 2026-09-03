from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "aivideoeditor-usergrowth-automation" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))

from usergrowth_automation.usergrowth_browser import (  # noqa: E402
    DELIVERY_MODAL_REOPEN_LIMIT,
    UserGrowthBrowserClient,
    UserGrowthDeliveryModalIncomplete,
)


class DeliveryModalRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def _client(self) -> UserGrowthBrowserClient:
        client = UserGrowthBrowserClient.__new__(UserGrowthBrowserClient)
        client._raise_if_cancelled = lambda: None
        client._snapshot_error = AsyncMock()
        client._sleep = AsyncMock()
        client._emit = lambda *_args, **_kwargs: None
        client._write_run_log = lambda *_args, **_kwargs: None
        client._reload_chameleon_entry_for_retry = AsyncMock()
        return client

    async def test_missing_delivery_field_refreshes_and_reopens(self) -> None:
        client = self._client()
        client._ensure_chameleon_modal = AsyncMock(
            side_effect=[
                UserGrowthDeliveryModalIncomplete("投放产品:红果免费漫剧(8704)"),
                None,
            ]
        )

        result = await client._ensure_chameleon_modal_with_recovery("page", "item")

        self.assertEqual(result, "page")
        client._reload_chameleon_entry_for_retry.assert_awaited_once()
        client._sleep.assert_awaited_once_with(2.0)
        self.assertEqual(client._ensure_chameleon_modal.await_count, 2)

    async def test_recovery_stops_after_ten_attempts_with_exponential_backoff(self) -> None:
        client = self._client()
        client._ensure_chameleon_modal = AsyncMock(
            side_effect=UserGrowthDeliveryModalIncomplete("投放产品缺失")
        )

        with self.assertRaisesRegex(RuntimeError, "连续 10 次刷新重开"):
            await client._ensure_chameleon_modal_with_recovery("page", "item")

        self.assertEqual(client._ensure_chameleon_modal.await_count, DELIVERY_MODAL_REOPEN_LIMIT)
        self.assertEqual(client._reload_chameleon_entry_for_retry.await_count, DELIVERY_MODAL_REOPEN_LIMIT - 1)
        self.assertEqual(
            [call.args[0] for call in client._sleep.await_args_list],
            [2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0, 30.0, 30.0],
        )


if __name__ == "__main__":
    unittest.main()
