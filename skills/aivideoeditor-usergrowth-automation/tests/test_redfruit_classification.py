from __future__ import annotations

from pathlib import Path
import sys
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from usergrowth_automation.usergrowth_redfruit import redfruit_material_type_path


class RedfruitClassificationTests(unittest.TestCase):
    def test_regular_material_uses_information_feed_pure_original_path(self) -> None:
        self.assertEqual(
            redfruit_material_type_path("原片"),
            ["番茄/红果小说素材类型", "信息流素材类型", "纯原片剪辑"],
        )

    def test_ai_material_keeps_ai_path(self) -> None:
        self.assertEqual(
            redfruit_material_type_path("AI前贴"),
            ["番茄/红果小说素材类型", "信息流素材类型", "AI素材", "AI前贴/后贴"],
        )


if __name__ == "__main__":
    unittest.main()
