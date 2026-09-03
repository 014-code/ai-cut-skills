from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = REPO_ROOT / "skills" / "aivideoeditor-usergrowth-automation" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))

from usergrowth_automation.usergrowth_redfruit import (
    REDFRUIT_ARLP_STAGES,
    REDFRUIT_DELIVERY_PLATFORMS,
    REDFRUIT_DELIVERY_PRODUCTS,
    build_redfruit_metadata,
    default_arlp_stages,
    default_delivery_platforms,
    default_delivery_products,
    redfruit_content_kind,
    redfruit_drama_title,
    redfruit_drama_type,
    redfruit_layout_label,
    require_redfruit_content_kind,
)


class RedfruitContentKindTests(unittest.TestCase):
    def test_redfruit_delivery_and_arlp_defaults_match_current_contract(self) -> None:
        self.assertEqual(
            REDFRUIT_DELIVERY_PRODUCTS,
            [
                "红果免费短剧(8662)",
                "红果免费漫剧(8704)",
                "蛋花免费小说(507427)",
                "番茄免费小说(1967)",
                "短剧端原生IAA(796433)",
                "番茄畅听(3040)",
            ],
        )
        self.assertEqual(REDFRUIT_DELIVERY_PLATFORMS, ["头条内广"])
        self.assertEqual(default_delivery_products("redfruit_short_drama"), REDFRUIT_DELIVERY_PRODUCTS)
        self.assertEqual(default_delivery_platforms("redfruit_short_drama"), ["头条内广"])
        self.assertEqual(default_arlp_stages("redfruit_short_drama"), [dict(stage) for stage in REDFRUIT_ARLP_STAGES])

    def test_all_redfruit_classification_fields_are_filled_during_entry(self) -> None:
        metadata = build_redfruit_metadata(
            Path("dxzc-动态漫-父爱无价-0818-无剧名-六部-zc-抽.mp4"),
        )
        roots = {path[0] for path in metadata["classification_paths"]}
        self.assertTrue(
            {
                "番茄/红果小说素材版式",
                "番茄/红果小说功能卖点",
                "番茄/红果小说素材类型",
                "IOS/非IOS",
                "番茄畅听素材类型",
                "番茄畅听IOS/非IOS",
                "尺度素材",
                "有无logo",
                "自动过审",
                "是否为AI素材",
                "番茄/红果短剧素材题材",
                "免费短剧-素材剪辑形式",
                "番茄畅听功能卖点",
                "是否带免费利益点",
                "有无歌曲名露出(非音乐类素材不要打)",
                "小程序系产品-内容体裁",
            }.issubset(roots)
        )
        self.assertIn(
            ["免费短剧-素材剪辑形式", "原片剪辑"],
            metadata["classification_paths"],
        )
        self.assertIn(
            ["是否带免费利益点", "是"],
            metadata["classification_paths"],
        )

    def test_regular_material_uses_information_feed_pure_original_path(self) -> None:
        metadata = build_redfruit_metadata(
            Path("dxzc-动态漫-父爱无价-0818-无剧名-六部-zc-抽.mp4"),
        )

        self.assertIn(
            ["番茄/红果小说素材类型", "信息流素材类型", "纯原片剪辑"],
            metadata["classification_paths"],
        )
        self.assertNotIn(
            ["番茄/红果小说素材类型", "剪辑制作", "常规剪辑"],
            metadata["classification_paths"],
        )

    def test_legacy_source_material_leaf_is_migrated_to_new_path(self) -> None:
        for source_leaf in ("常规剪辑", "原片剪辑", "纯原片剪辑"):
            with self.subTest(source_leaf=source_leaf):
                metadata = build_redfruit_metadata(
                    Path("dxzc-动态漫-父爱无价-0818-无剧名-六部-zc-抽.mp4"),
                    source_category_tag_names=[source_leaf],
                )
                self.assertIn(
                    ["番茄/红果小说素材类型", "信息流素材类型", "纯原片剪辑"],
                    metadata["classification_paths"],
                )

    def test_source_leaf_maps_to_entry_classification_leaf(self) -> None:
        metadata = build_redfruit_metadata(
            Path("dxzc-仿真人-父爱无价，少爷归家-0818-无剧名-六部-zc-抽.mp4"),
            source_category_tag_names=["纯原片剪辑"],
        )

        path = next(path for path in metadata["classification_paths"] if path[0] == "免费短剧-素材剪辑形式")
        self.assertEqual(path, ["免费短剧-素材剪辑形式", "原片剪辑"])

    def test_layout_uses_video_dimensions_when_filename_has_no_layout_token(self) -> None:
        with patch(
            "usergrowth_automation.usergrowth_redfruit._probe_video_dimensions",
            return_value=(1920, 1080),
        ):
            self.assertEqual(
                redfruit_layout_label(Path("dxzc-动态漫-父爱无价-0818-zc-抽.mp4")),
                "横版-纯横版",
            )

    def test_layout_infers_horizontal_to_vertical_from_source_tags(self) -> None:
        with patch(
            "usergrowth_automation.usergrowth_redfruit._probe_video_dimensions",
            return_value=(1080, 1920),
        ):
            metadata = build_redfruit_metadata(
                Path("dxzc-动态漫-父爱无价-0818-无剧名-zc-抽.mp4"),
                source_category_tag_names=["横版-纯横版"],
            )

        self.assertEqual(metadata["layout"], "竖版-横改竖")
        self.assertIn(
            ["番茄/红果小说素材版式", "视频版式", "竖版-横改竖"],
            metadata["classification_paths"],
        )

    def test_explicit_vertical_to_horizontal_token_has_priority(self) -> None:
        self.assertEqual(
            redfruit_layout_label(Path("dxzc-动态漫-父爱无价-竖改横-zc-抽.mp4")),
            "横版-竖改横",
        )

    def test_fission_filename_short_drama_aliases_pure_short_drama(self) -> None:
        file_name = "dxzc-短剧-沧海珠碎两世别-0818-无剧名-六部-zc-抽.mp4"

        self.assertEqual(redfruit_content_kind(file_name), "")
        self.assertEqual(redfruit_drama_type(file_name), "纯短剧")
        self.assertEqual(redfruit_drama_title(file_name), "沧海珠碎两世别")
        self.assertEqual(
            require_redfruit_content_kind(file_name, allow_bare_short_drama=True),
            "纯短剧",
        )

    def test_existing_supported_types_remain_unchanged(self) -> None:
        self.assertEqual(redfruit_content_kind("动态漫"), "动态漫")
        self.assertEqual(redfruit_content_kind("仿真人"), "仿真人")
        self.assertEqual(redfruit_content_kind("纯短剧"), "纯短剧")
        self.assertEqual(redfruit_content_kind("真人实拍短剧"), "纯短剧")

    def test_unknown_or_non_token_short_drama_values_remain_blocked(self) -> None:
        self.assertEqual(redfruit_content_kind("短剧洞察", allow_bare_short_drama=True), "")
        with self.assertRaises(ValueError):
            require_redfruit_content_kind("dxzc-未定义类型-剧名.mp4", allow_bare_short_drama=True)


if __name__ == "__main__":
    unittest.main()
