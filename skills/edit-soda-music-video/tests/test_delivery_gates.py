from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import soda_pipeline  # noqa: E402


class DeliveryGateTests(unittest.TestCase):
    def environment_report(self, path: Path, profile: str = "soda-scripted-render") -> None:
        path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "status": "ready",
                    "profile": profile,
                    "current_environment": {"python_executable": sys.executable},
                    "checks": {
                        "ffmpeg": {"ok": True},
                        "ffprobe": {"ok": True},
                        "whisper": {"ok": True},
                        "manage_visual_asset_library": {"ok": True},
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_environment_report_must_match_active_python_and_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "video_environment.json"
            self.environment_report(report_path)

            report = soda_pipeline.validate_environment_report(
                report_path,
                require_whisper=True,
            )

            self.assertTrue(report["ok"], report)
            self.assertEqual(report["status"], "ready")

            source = json.loads(report_path.read_text(encoding="utf-8"))
            source["current_environment"]["python_executable"] = "/different/python"
            report_path.write_text(json.dumps(source), encoding="utf-8")
            mismatch = soda_pipeline.validate_environment_report(
                report_path,
                require_whisper=True,
            )
            self.assertFalse(mismatch["ok"])
            self.assertTrue(any("当前 Python" in item for item in mismatch["errors"]))

    def render_args(self, root: Path) -> argparse.Namespace:
        return argparse.Namespace(
            input=root / "input.mp4",
            output=root / "output.mp4",
            timeline_json=root / "timeline.json",
            asset_manifest=root / "visual_assets_manifest.json",
            environment_report=root / "video_environment.json",
            compliance_report=root / "compliance.json",
            preflight_report=root / "preflight.json",
            qa_report=root / "qa.json",
            delivery_report=root / "delivery.json",
            script_file=None,
            text=None,
            quick_qa=False,
        )

    def test_formal_delivery_requires_every_stage_and_official_renderer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self.render_args(root)
            preflight = {
                "ok": True,
                "environment": {"ok": True},
                "asset_understanding": {"ok": True},
            }
            delivery = soda_pipeline.build_delivery_report(
                args,
                status="ready",
                subtitle_repair=None,
                compliance={"ok": True},
                preflight=preflight,
                renderer_report={"renderer": "standalone-ffmpeg"},
                qa={"ok": True, "scope": "technical_media_only"},
            )

            self.assertTrue(delivery["formal_delivery_ready"])
            self.assertEqual(delivery["pipeline_entry"], "soda_pipeline.py render")

            bypassed = soda_pipeline.build_delivery_report(
                args,
                status="ready",
                subtitle_repair=None,
                compliance={"ok": True},
                preflight=preflight,
                renderer_report={"renderer": "custom-ffmpeg"},
                qa={"ok": True, "scope": "technical_media_only"},
            )
            self.assertFalse(bypassed["formal_delivery_ready"])
            self.assertEqual(bypassed["status"], "blocked")

    def test_standalone_qa_contract_never_claims_formal_delivery(self) -> None:
        source = (SKILL_ROOT / "scripts" / "soda_pipeline.py").read_text(encoding="utf-8")

        self.assertIn('"scope": "technical_media_only"', source)
        self.assertIn('"formal_delivery_ready": False', source)
        self.assertIn("only_soda_pipeline_render_may_claim_formal_delivery", source)

    def test_bundled_tiny_is_discovered_for_whisper_cli(self) -> None:
        model_dir = soda_pipeline.resolve_whisper_model_dir("tiny")

        self.assertIsNotNone(model_dir)
        self.assertTrue((model_dir / "tiny.pt").is_file())
        self.assertIsNone(soda_pipeline.resolve_whisper_model_dir("base"))

    def test_parser_exposes_environment_and_delivery_reports(self) -> None:
        parser = soda_pipeline.build_parser()
        help_text = parser.format_help()
        render_parser = next(
            action.choices["render"]
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        render_help = render_parser.format_help()

        self.assertIn("render", help_text)
        self.assertIn("--environment-report", render_help)
        self.assertIn("--delivery-report", render_help)

    def test_soda_asset_gate_uses_generic_validator_report(self) -> None:
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
                                "media": {
                                    "probe_ok": True,
                                    "width": 100,
                                    "height": 100,
                                },
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

            report = soda_pipeline.validate_asset_understanding(
                manifest,
                assets,
                {"materials": [], "logo": {}, "tail": {}},
            )

            self.assertTrue(report["ok"], report)
            self.assertTrue(report["validator"].endswith("validate_manifest.py"))


if __name__ == "__main__":
    unittest.main()
