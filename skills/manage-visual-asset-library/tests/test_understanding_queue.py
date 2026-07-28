from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_understanding_queue  # noqa: E402


class UnderstandingQueueTests(unittest.TestCase):
    def test_queue_only_contains_records_that_still_need_read_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            assets = root / "assets"
            assets.mkdir()
            (assets / "complete.png").write_bytes(b"complete")
            (assets / "pending.png").write_bytes(b"pending")
            manifest = root / "visual_assets_manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "asset_root": str(assets.resolve()),
                        "assets": [
                            {
                                "relative_path": "complete.png",
                                "kind": "image",
                                "media": {"probe_ok": True, "width": 100, "height": 100},
                                "description": "完整素材。",
                                "effective_region": {
                                    "x": 0,
                                    "y": 0,
                                    "width": 100,
                                    "height": 100,
                                    "coordinate_space": "source_pixels",
                                },
                            },
                            {
                                "relative_path": "pending.png",
                                "kind": "image",
                                "media": {"probe_ok": True, "width": 100, "height": 100},
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = build_understanding_queue.build_queue(
                manifest,
                assets,
                root / "frames",
                extract_pending_video_frames=False,
            )

            self.assertEqual(report["summary"]["total"], 2)
            self.assertEqual(report["summary"]["complete"], 1)
            self.assertEqual(report["summary"]["pending"], 1)
            self.assertEqual(
                [item["relative_path"] for item in report["items"]],
                ["pending.png"],
            )
            self.assertIn("missing_description", report["items"][0]["reasons"])
            self.assertIn("missing_effective_region", report["items"][0]["reasons"])

    def test_completed_manifest_produces_empty_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            assets = root / "assets"
            assets.mkdir()
            (assets / "screen.png").write_bytes(b"screen")
            manifest = root / "visual_assets_manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "asset_root": str(assets.resolve()),
                        "assets": [
                            {
                                "relative_path": "screen.png",
                                "kind": "image",
                                "media": {"probe_ok": True, "width": 100, "height": 100},
                                "description": "完整页面。",
                                "effective_region": {
                                    "x": 0,
                                    "y": 0,
                                    "width": 100,
                                    "height": 100,
                                    "coordinate_space": "source_pixels",
                                },
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = build_understanding_queue.build_queue(
                manifest,
                assets,
                root / "frames",
                extract_pending_video_frames=False,
            )

            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["items"], [])
            self.assertEqual(report["summary"]["progress_percent"], 100.0)


if __name__ == "__main__":
    unittest.main()
