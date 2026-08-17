from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = REPO_ROOT / "skills" / "aivideoeditor-usergrowth-automation" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))

from usergrowth_automation.usergrowth_browser import UserGrowthBrowserClient


class ExistingRecordedCidTests(unittest.TestCase):
    def test_parse_already_recorded_material_cids(self) -> None:
        text = (
            "以下创意已录入为素材"
            "创意id=213749285已录入,cid=2f4e95b098e4d645382c5f28c699b0cb;"
            "创意id=213749404已录入,cid=a47961fb82a4b65ae9dc7fb9c6614ee9;"
            "取消 确定"
        )

        self.assertEqual(
            UserGrowthBrowserClient._parse_already_recorded_material_cids(text),
            {
                "213749285": "2f4e95b098e4d645382c5f28c699b0cb",
                "213749404": "a47961fb82a4b65ae9dc7fb9c6614ee9",
            },
        )

    def test_redfruit_empty_classification_modal_refreshes_before_reopen(self) -> None:
        events: list[str] = []

        class Client(UserGrowthBrowserClient):
            def __init__(self) -> None:
                super().__init__("", "", reuse_saved_session=False)
                self.probes = [
                    {"root": object(), "empty_message": "暂无数据", "field_names": []},
                    {"root": object(), "empty_message": "", "field_names": ["分类标签"]},
                ]

            async def _classification_modal_probe(self, page, required_fields, *, scan_all=False):
                return self.probes.pop(0)

            async def _cancel_classification_modal(self, page, root) -> None:
                events.append("cancel")

            async def _sleep(self, seconds: float) -> None:
                events.append(f"sleep:{seconds}")

            async def _reload_classification_modal_host(self, page, context_label: str) -> None:
                events.append("reload")

            def _write_run_log(self, message: str) -> None:
                return None

        async def run() -> None:
            client = Client()

            async def opener() -> None:
                events.append("open")

            async def restored() -> None:
                events.append("restore")

            await client._open_classification_modal_ready(
                object(),
                opener,
                required_fields=["分类标签"],
                context_label="红果短剧测试",
                refresh_before_reopen=True,
                on_page_refreshed=restored,
            )

        asyncio.run(run())
        self.assertEqual(events, ["open", "cancel", "sleep:2.0", "reload", "restore", "open"])


if __name__ == "__main__":
    unittest.main()
