from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
