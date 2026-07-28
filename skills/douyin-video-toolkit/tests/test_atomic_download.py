from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import douyin_reference_core as core  # noqa: E402
import download_douyin_share_videos as page_capture  # noqa: E402
import wanbang_douyin_batch_download as wanbang_cli  # noqa: E402


class FakeResponse:
    def __init__(self, chunks: list[bytes | BaseException]) -> None:
        self.chunks = iter(chunks)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _size: int) -> bytes:
        value = next(self.chunks, b"")
        if isinstance(value, BaseException):
            raise value
        return value


class AtomicDownloadTests(unittest.TestCase):
    def test_page_capture_reuses_shared_gid_normalization(self) -> None:
        url = "https://www.douyin.com/video/7380000000000000001"
        self.assertEqual(page_capture.video_id_from_url(url), core.extract_gid(url))
        self.assertEqual(
            page_capture.canonical_video_url(url),
            core.build_douyin_video_url("7380000000000000001"),
        )

    def test_nonempty_invalid_file_is_not_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "partial.mp4"
            path.write_bytes(b"not-an-mp4" * 200)

            self.assertFalse(core.validate_mp4_file(path))

    def test_interrupted_download_removes_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "gid.mp4"
            response = FakeResponse([b"x" * 2048, OSError("connection reset")])

            with mock.patch.object(core.urllib.request, "urlopen", return_value=response):
                with self.assertRaisesRegex(OSError, "connection reset"):
                    core.download_file("https://example.test/video", target)

            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".mp4.part").exists())

    def test_success_replaces_part_file_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "gid.mp4"
            response = FakeResponse([b"x" * 2048, b""])

            with (
                mock.patch.object(core.urllib.request, "urlopen", return_value=response),
                mock.patch.object(core, "validate_mp4_file", return_value=True),
            ):
                size = core.download_file("https://example.test/video", target)

            self.assertEqual(size, 2048)
            self.assertTrue(target.exists())
            self.assertFalse(target.with_suffix(".mp4.part").exists())

    def test_reference_manifest_round_trip(self) -> None:
        references = core.resolve_references(
            gids=["7380000000000000001"],
            urls=["not-a-douyin-reference"],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "references.json"
            core.write_reference_manifest(references, path)
            loaded = core.load_reference_manifest(path)

        self.assertEqual(loaded, references)
        resolved = next(item for item in loaded if item.gid)
        failed = next(item for item in loaded if not item.gid)
        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error, "no Douyin GID found")

    def test_no_download_cli_writes_canonical_reference_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "output"
            exit_code = wanbang_cli.main(
                [
                    "--gid",
                    "7380000000000000001",
                    "--url",
                    "not-a-douyin-reference",
                    "--no-download",
                    "--out-dir",
                    str(output_dir),
                ]
            )
            references = core.load_reference_manifest(output_dir / "references.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(sum(1 for item in references if item.status == "resolved"), 1)
        self.assertEqual(sum(1 for item in references if item.status == "failed"), 1)


if __name__ == "__main__":
    unittest.main()
