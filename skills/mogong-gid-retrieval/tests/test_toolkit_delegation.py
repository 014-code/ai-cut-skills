from __future__ import annotations

import asyncio
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_SKILLS = SKILL_ROOT.parent
TOOLKIT_CORE = REPOSITORY_SKILLS / "douyin-video-toolkit" / "scripts" / "douyin_reference_core.py"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import mogong_gid_retrieval as mogong  # noqa: E402


class ToolkitDelegationTests(unittest.TestCase):
    def test_mogong_source_contains_no_wanbang_or_short_link_implementation(self) -> None:
        source = (SKILL_ROOT / "scripts" / "mogong_gid_retrieval.py").read_text(encoding="utf-8")
        for forbidden in (
            "class WanbangDouyinClient",
            "item_search_video",
            "item_get_video",
            "def resolve_douyin_reference",
            "def extract_gid_from_text",
            "import httpx",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_repository_sibling_toolkit_is_discovered(self) -> None:
        self.assertEqual(mogong.find_douyin_toolkit_core(), TOOLKIT_CORE.resolve())

    def test_gid_input_is_normalized_by_douyin_toolkit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.csv"
            with input_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(["GID"])
                writer.writerow(["7380000000000000001"])
                writer.writerow(["invalid"])
            args = SimpleNamespace(
                input=str(input_path),
                mode="gid",
                url_column=None,
                gid_column=None,
                keyword_column=None,
                douyin_toolkit_core=str(TOOLKIT_CORE),
                wanbang_key=None,
                wanbang_secret=None,
                wanbang_base_url=None,
                wanbang_retry_count=2,
                wanbang_retry_delay_seconds=0,
                wanbang_page=1,
                max_videos_per_keyword=12,
                short_url_timeout=1,
            )
            references, input_count = asyncio.run(mogong.build_references(args))

        self.assertEqual(input_count, 2)
        self.assertEqual(references[0].gid, "7380000000000000001")
        self.assertIsNone(references[1].gid)
        self.assertEqual(references[1].error_message, "invalid Douyin GID")

    def test_download_is_delegated_to_toolkit(self) -> None:
        calls: list[tuple[str, Path]] = []

        class FakeToolkit:
            @staticmethod
            def validate_mp4_file(_path: Path) -> bool:
                return False

            @staticmethod
            def download_file(url: str, path: Path) -> int:
                calls.append((url, path))
                return 2048

        class FakeClient:
            @staticmethod
            def video_download_url(gid: str) -> str:
                return f"https://download.example/{gid}.mp4"

        with tempfile.TemporaryDirectory() as temp_dir:
            args = SimpleNamespace(
                download=True,
                output_dir=temp_dir,
                download_scope="matched",
                skip_existing_downloads=False,
                douyin_toolkit_core=None,
            )
            items = [
                mogong.ResultItem(
                    gid="7380000000000000001",
                    video_url="https://www.douyin.com/video/7380000000000000001",
                    source_url="7380000000000000001",
                    query_status="matched",
                )
            ]
            with (
                mock.patch.object(mogong, "load_douyin_toolkit_core", return_value=FakeToolkit),
                mock.patch.object(mogong, "wanbang_client_from_args", return_value=FakeClient()),
            ):
                asyncio.run(mogong.maybe_download(args, items))

        self.assertEqual(len(calls), 1)
        self.assertEqual(items[0].download_status, "downloaded")
        self.assertTrue(items[0].download_path.endswith("7380000000000000001.mp4"))

    def test_skip_mogong_run_writes_business_outputs_from_toolkit_references(self) -> None:
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            self.skipTest("openpyxl is not installed")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "input.csv"
            output_dir = root / "output"
            with input_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(["GID"])
                writer.writerow(["7380000000000000001"])
                writer.writerow(["invalid"])

            exit_code = mogong.main(
                [
                    "run",
                    "--mode",
                    "gid",
                    "--input",
                    str(input_path),
                    "--output-dir",
                    str(output_dir),
                    "--skip-mogong",
                    "--douyin-toolkit-core",
                    str(TOOLKIT_CORE),
                ]
            )
            summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["input_count"], 2)
        self.assertEqual(summary["parsed_count"], 1)
        self.assertEqual(summary["parse_failed_count"], 1)
        self.assertEqual(summary["result_count"], 1)


if __name__ == "__main__":
    unittest.main()
