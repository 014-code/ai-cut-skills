#!/usr/bin/env python3

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import package_short_drama  # noqa: E402


def parse_overlay_args(values: list[str]):
    return package_short_drama.build_parser().parse_args(
        [
            "source.mp4",
            "--tailboard",
            "tail.mp4",
            "--output",
            "output.mp4",
            *values,
        ]
    )


class ShortDramaOverlayTests(unittest.TestCase):
    def test_existing_title_and_risk_adds_only_missing_ai_notice(self) -> None:
        args = parse_overlay_args(
            [
                "--source-title-present",
                "--title-bottom",
                "92",
                "--source-risk-present",
                "--ai-text",
                "本故事由AI生成",
            ]
        )

        plan = package_short_drama.resolve_overlay_plan(args, 1080, 1920)

        self.assertEqual(plan["added_title_text"], "")
        self.assertEqual(plan["added_risk_text"], "")
        self.assertEqual(plan["added_ai_text"], "本故事由AI生成")
        self.assertEqual(plan["notice_text"], "本故事由AI生成")
        self.assertGreaterEqual(plan["benefit_y"], 92 + plan["title_gap"])
        self.assertFalse(plan["notice_background"])
        self.assertEqual(plan["notice_shadow"], 0)

    def test_missing_source_title_is_added_before_benefit(self) -> None:
        args = parse_overlay_args(
            [
                "--title-text",
                "《离婚那天我多了个董事长妈妈》",
                "--risk-text",
                "本故事纯属虚构",
                "--ai-text",
                "本故事由AI生成",
            ]
        )

        plan = package_short_drama.resolve_overlay_plan(args, 1080, 1920)

        self.assertEqual(plan["added_title_text"], "《离婚那天我多了个董事长妈妈》")
        self.assertGreaterEqual(
            plan["benefit_y"],
            plan["title_bottom"] + plan["title_gap"],
        )

    def test_long_added_title_is_fitted_to_narrow_canvas(self) -> None:
        args = parse_overlay_args(
            [
                "--title-text",
                "《离婚那天我多了个董事长妈妈》",
                "--risk-text",
                "本故事纯属虚构",
                "--ai-text",
                "本故事由AI生成",
            ]
        )

        plan = package_short_drama.resolve_overlay_plan(args, 360, 640)

        self.assertLess(plan["title_size"], 28)
        estimated_width = (
            package_short_drama.estimated_text_units(plan["added_title_text"])
            * plan["title_size"]
            * 1.06
        )
        self.assertLessEqual(estimated_width, 360 - 2 * max(12, round(360 * 0.04)))

    def test_source_title_requires_visual_bottom_measurement(self) -> None:
        args = parse_overlay_args(
            [
                "--source-title-present",
                "--source-risk-present",
                "--ai-text",
                "本故事由AI生成",
            ]
        )

        with self.assertRaisesRegex(SystemExit, "--title-bottom"):
            package_short_drama.resolve_overlay_plan(args, 1080, 1920)

    def test_benefit_override_cannot_overlap_title(self) -> None:
        args = parse_overlay_args(
            [
                "--source-title-present",
                "--title-bottom",
                "100",
                "--benefit-y",
                "105",
                "--source-risk-present",
                "--ai-text",
                "本故事由AI生成",
            ]
        )

        with self.assertRaisesRegex(SystemExit, "clear the title"):
            package_short_drama.resolve_overlay_plan(args, 1080, 1920)

    def test_overlay_audit_choices_are_required(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_overlay_args([])

    def test_drawtext_has_outline_but_no_background_or_shadow(self) -> None:
        expression = package_short_drama.drawtext(
            font=Path("/tmp/font.ttf"),
            text="本故事由AI生成",
            size=24,
            color="FFFFFF",
            x="(w-text_w)/2",
            y="1800",
            border=2,
        )

        self.assertIn("borderw=2", expression)
        self.assertIn("shadowx=0", expression)
        self.assertIn("shadowy=0", expression)
        self.assertNotIn("box=1", expression)
        self.assertNotIn("boxcolor=", expression)


if __name__ == "__main__":
    unittest.main()
