from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from run_pre_roll_standalone import generate_ass, strip_rendered_subtitle_punctuation  # noqa: E402


class SubtitleNumericPunctuationTests(unittest.TestCase):
    def test_preserves_punctuation_that_carries_numeric_meaning(self) -> None:
        cases = {
            "满 0.3 元，马上到账！": "满 0.3 元马上到账",
            "每天 12:30 开抢。": "每天 12:30 开抢",
            "最高 1,000 元！": "最高 1,000 元",
            "活动时间 2026/07/30。": "活动时间 2026/07/30",
            "连续 3-5 天，收益更高。": "连续 3-5 天收益更高",
            "温度低至 -3 度。": "温度低至 -3 度",
        }

        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(strip_rendered_subtitle_punctuation(source), expected)

    def test_generate_ass_uses_safe_display_text_and_keeps_source_text(self) -> None:
        source_text = "满 0.3 元，12:30 前到账！"
        subtitle_events = [{"start": 0.0, "end": 1.5, "text": source_text}]
        subtitle_config = {
            "fontName": "Test Body",
            "brandFontName": "Test Brand",
            "fontSize": 46,
            "maxLines": 2,
        }
        disclaimer_config = {"fontName": "Test Disclaimer", "fontSize": 22}

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "subtitles.ass"
            result = generate_ass(
                output_path=output_path,
                text=source_text,
                duration=2.0,
                width=1080,
                height=1920,
                subtitle_config=subtitle_config,
                disclaimer_text=None,
                disclaimer_config=disclaimer_config,
                brand_text=None,
                subtitle_events=subtitle_events,
            )

            self.assertEqual(result["events"][0]["text"], "满 0.3 元12:30 前到账")
            self.assertEqual(result["events"][0]["sourceText"], source_text)
            ass_text = output_path.read_text(encoding="utf-8")
            self.assertIn("满 0.3 元12:30 前到账", ass_text)


if __name__ == "__main__":
    unittest.main()
