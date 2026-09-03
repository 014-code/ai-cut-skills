from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from run_pre_roll_standalone import (  # noqa: E402
    RunnerError,
    generate_seedance_video,
    resolve_ark_video_generation_auth,
)


class VirtualKeyRuntimeTests(unittest.TestCase):
    def test_uses_virtual_key_only_with_platform_runtime_proxy(self) -> None:
        api_key, base_url, mode = resolve_ark_video_generation_auth(
            ark_api_key=None,
            ark_base_url="https://ark.cn-beijing.volces.com/api/v3",
            ark_virtual_key="vk_test_only",
            ark_virtual_runtime_base_url="https://platform.example/api/v1/api-key-distribution/runtime/ark/api/v3",
        )

        self.assertEqual(api_key, "vk_test_only")
        self.assertEqual(base_url, "https://platform.example/api/v1/api-key-distribution/runtime/ark/api/v3")
        self.assertEqual(mode, "virtual_key_runtime_proxy")

    def test_rejects_direct_and_virtual_key_together(self) -> None:
        with self.assertRaisesRegex(RunnerError, "either"):
            resolve_ark_video_generation_auth(
                ark_api_key="ark_test_only",
                ark_base_url="https://ark.cn-beijing.volces.com/api/v3",
                ark_virtual_key="vk_test_only",
                ark_virtual_runtime_base_url="https://platform.example/api/v1/api-key-distribution/runtime/ark/api/v3",
            )

    def test_generated_request_uses_virtual_key_at_runtime_proxy(self) -> None:
        calls = []

        def fake_ark_request(method, url, api_key, body=None):
            calls.append({"method": method, "url": url, "apiKey": api_key, "body": body})
            return {
                "id": "task-test",
                "status": "succeeded",
                "content": {"video_url": "https://download.example/generated.mp4"},
            }

        with self.subTest("runtime proxy request"):
            with patch("run_pre_roll_standalone.ark_request_json", side_effect=fake_ark_request), patch(
                "run_pre_roll_standalone.download_url"
            ):
                result = generate_seedance_video(
                    api_key="vk_test_only",
                    base_url="https://platform.example/api/v1/api-key-distribution/runtime/ark/api/v3",
                    model="doubao-seedance-1-0-pro-250528",
                    prompt="无人物的竖屏夜景",
                    duration=5,
                    ratio="9:16",
                    resolution="720p",
                    output_path=Path("generated.mp4"),
                    poll_interval=0,
                    timeout_seconds=1,
                )

        self.assertEqual(result["taskId"], "task-test")
        self.assertEqual(calls[0]["apiKey"], "vk_test_only")
        self.assertEqual(
            calls[0]["url"],
            "https://platform.example/api/v1/api-key-distribution/runtime/ark/api/v3/contents/generations/tasks",
        )


if __name__ == "__main__":
    unittest.main()
