#!/usr/bin/env python3

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import alpha_safety  # noqa: E402
import soda_pipeline  # noqa: E402
import standalone_renderer  # noqa: E402


class VideoPolicyTests(unittest.TestCase):
    def test_embedded_alpha_transparent_perimeter_is_not_a_fixed_black_bar(self) -> None:
        with patch.object(soda_pipeline, "detect_embedded_black_bars") as detector:
            result = soda_pipeline.detect_material_black_bars(
                Path("/tmp/alpha.mp4"),
                "embedded_alpha",
            )

        detector.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "managed_by_alpha_gate")

    def test_opaque_video_keeps_fixed_black_bar_detection(self) -> None:
        expected = {"ok": False, "status": "fixed_black_bars_detected"}
        with patch.object(
            soda_pipeline,
            "detect_embedded_black_bars",
            return_value=expected,
        ) as detector:
            result = soda_pipeline.detect_material_black_bars(
                Path("/tmp/opaque.mp4"),
                "opaque",
            )

        detector.assert_called_once_with(Path("/tmp/opaque.mp4"))
        self.assertEqual(result, expected)

    def test_pipeline_decodes_ffmpeg_output_as_utf8(self) -> None:
        with patch.object(soda_pipeline.subprocess, "run") as mocked_run:
            mocked_run.return_value.returncode = 0
            soda_pipeline.run_command(["ffmpeg", "-version"])

        mocked_run.assert_called_once_with(
            ["ffmpeg", "-version"],
            cwd=None,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=soda_pipeline.subprocess.PIPE,
            stderr=soda_pipeline.subprocess.PIPE,
            check=False,
        )

    def test_text_subprocesses_decode_ffmpeg_output_as_utf8(self) -> None:
        with patch.object(alpha_safety.subprocess, "run") as mocked_run:
            alpha_safety._run(["ffmpeg", "-version"])

        mocked_run.assert_called_once_with(
            ["ffmpeg", "-version"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )

    def test_video_requires_explicit_transparency_mode(self) -> None:
        with self.assertRaisesRegex(alpha_safety.AlphaSafetyError, "transparency_mode"):
            alpha_safety.validate_video_material_policy(
                {"kind": "video"},
                label="materials[0]",
            )

    def test_embedded_alpha_defaults_to_once_hold_last(self) -> None:
        policy = alpha_safety.validate_video_material_policy(
            {
                "kind": "video",
                "transparency_mode": "embedded_alpha",
                "include_audio": True,
            },
            label="materials[0]",
        )
        self.assertEqual(policy["playback_mode"], "once_hold_last")
        self.assertEqual(policy["audio_gain_db"], -3.0)

    def test_alpha_fallback_filters_are_rejected(self) -> None:
        for graph in ("colorkey=black", "chromakey=black", "blend=all_mode=screen"):
            with self.subTest(graph=graph):
                with self.assertRaises(alpha_safety.AlphaSafetyError):
                    alpha_safety.assert_safe_filter_graph(graph)


class VideoRenderGraphTests(unittest.TestCase):
    def test_renderer_decodes_ffmpeg_output_as_utf8(self) -> None:
        with patch.object(standalone_renderer.subprocess, "run") as mocked_run:
            mocked_run.return_value.returncode = 0
            standalone_renderer.run(["ffmpeg", "-version"], label="encoding-test")

        mocked_run.assert_called_once_with(
            ["ffmpeg", "-version"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )

    def test_video_resets_pts_holds_last_frame_and_mixes_source_audio(self) -> None:
        captured: dict[str, list[str]] = {}

        def capture(command: list[str], *, label: str) -> None:
            self.assertEqual(label, "main-render")
            captured["command"] = command

        material = {
            "layout": "phone",
            "path": Path("/tmp/alpha.mp4"),
            "kind": "video",
            "transparency_mode": "embedded_alpha",
            "playback_mode": "once_hold_last",
            "include_audio": True,
            "audio_gain_db": -3,
            "mapped_start": 3.0,
            "mapped_end": 8.0,
        }
        assets = {
            "font": Path("/tmp/body.ttf"),
            "logo": Path("/tmp/logo.png"),
        }
        with (
            patch.object(standalone_renderer, "run", side_effect=capture),
            patch.object(
                standalone_renderer,
                "media_summary",
                return_value={
                    "duration": 2.0,
                    "has_audio": True,
                    "audio_codec": "aac",
                },
            ),
        ):
            evidence = standalone_renderer.render_main(
                Path("/tmp/input.mp4"),
                Path("/tmp/captions.ass"),
                Path("/tmp/output.mp4"),
                {"width": 1080, "height": 1920, "fps": 30, "speed": 1.0},
                assets,
                Path("/tmp/fonts"),
                [material],
                10.0,
                show_warning=True,
                logo_mode="full_canvas",
            )

        graph = evidence["filter_graph"]
        command = captured["command"]
        self.assertNotIn("-stream_loop", command)
        self.assertIn("trim=duration=2.000000,setpts=PTS-STARTPTS", graph)
        self.assertIn("tpad=stop_mode=clone:stop_duration=3.000000", graph)
        self.assertIn("setpts=PTS+3.000000/TB", graph)
        self.assertIn("volume=-3.000dB,adelay=3000|3000", graph)
        self.assertIn("source_audio_codec", str(evidence["material_audio"]))
        self.assertNotIn("sine=", graph)
        self.assertIn(
            f"fontfile='{standalone_renderer.escape_filter_path(Path('/tmp/fonts/body.ttf'))}'",
            graph,
        )
        self.assertNotIn(
            f"fontfile='{standalone_renderer.escape_filter_path(Path('/tmp/body.ttf'))}'",
            graph,
        )

    def test_loop_is_the_only_video_mode_that_uses_stream_loop(self) -> None:
        self.assertIn(
            "-stream_loop",
            standalone_renderer.input_args(
                Path("/tmp/alpha.mp4"),
                "video",
                30,
                "loop",
            ),
        )
        self.assertNotIn(
            "-stream_loop",
            standalone_renderer.input_args(
                Path("/tmp/alpha.mp4"),
                "video",
                30,
                "once_hold_last",
            ),
        )


if __name__ == "__main__":
    unittest.main()
