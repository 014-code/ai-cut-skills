from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


REFERENCE_SCHEMA_VERSION = 1
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
GID_PATTERN = re.compile(r"(?<!\d)(\d{16,22})(?!\d)")


@dataclass
class VideoReference:
    source_url: str
    gid: str = ""
    video_url: str = ""
    keyword: str = ""
    status: str = "resolved"
    error: str = ""


def build_douyin_video_url(gid: str) -> str:
    return f"https://www.douyin.com/video/{gid}"


def resolve_redirect_url(url: str, timeout: int = 20) -> str:
    parsed = urllib.parse.urlparse(url)
    if not parsed.netloc.endswith("v.douyin.com"):
        return url
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.geturl() or url


def extract_gid(value: str, *, resolve_short_url: bool = True, timeout: int = 20) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if GID_PATTERN.fullmatch(text):
        return text

    parsed = urllib.parse.urlparse(text)
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("modal_id", "gid", "video_id", "item_id", "aweme_id"):
        candidate = str((query.get(key) or [""])[0]).strip()
        if GID_PATTERN.fullmatch(candidate):
            return candidate

    path_match = re.search(r"/(?:share/)?(?:video|note)/(\d{16,22})", parsed.path)
    if path_match:
        return path_match.group(1)

    text_match = GID_PATTERN.search(text)
    if text_match:
        return text_match.group(1)

    if resolve_short_url and parsed.netloc.endswith("v.douyin.com"):
        resolved = resolve_redirect_url(text, timeout=timeout)
        if resolved != text:
            return extract_gid(resolved, resolve_short_url=False, timeout=timeout)
    return ""


def resolve_url_reference(value: str, *, timeout: int = 20) -> VideoReference:
    source = str(value or "").strip()
    if not source:
        return VideoReference(source_url=source, status="failed", error="empty input")
    try:
        gid = extract_gid(source, timeout=timeout)
    except Exception as exc:
        return VideoReference(
            source_url=source,
            status="failed",
            error=f"failed to resolve Douyin reference: {exc}",
        )
    if not gid:
        return VideoReference(source_url=source, status="failed", error="no Douyin GID found")
    return VideoReference(source_url=source, gid=gid, video_url=build_douyin_video_url(gid))


def resolve_gid_reference(value: str) -> VideoReference:
    source = str(value or "").strip()
    gid = extract_gid(source, resolve_short_url=False)
    if not gid:
        return VideoReference(source_url=source, status="failed", error="invalid Douyin GID")
    return VideoReference(source_url=source, gid=gid, video_url=build_douyin_video_url(gid))


class WanbangClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str,
        timeout: int = 90,
        retry_count: int = 2,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retry_count = max(0, retry_count)
        self.retry_delay_seconds = max(0.0, retry_delay_seconds)
        if not self.api_key or not self.api_secret or not self.base_url:
            raise RuntimeError("Set WANBANG_API_KEY, WANBANG_API_SECRET, and WANBANG_DOUYIN_BASE_URL.")

    def get_json(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        full_params = {
            "key": self.api_key,
            "secret": self.api_secret,
            "cache": "no",
            "result_type": "json",
            **params,
        }
        url = f"{self.base_url.rstrip('/')}/{endpoint.strip('/')}/?{urllib.parse.urlencode(full_params)}"
        last_error: Exception | None = None
        for attempt in range(self.retry_count + 1):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    text = response.read().decode("utf-8", errors="replace")
                payload = json.loads(text)
                error_code = str(payload.get("error_code") or "")
                if error_code and error_code != "0000":
                    reason = payload.get("reason") or payload.get("error") or "unknown error"
                    raise RuntimeError(f"Wanbang {endpoint} failed: {error_code} {reason}")
                return payload
            except Exception as exc:
                last_error = exc
                if attempt >= self.retry_count:
                    break
                time.sleep(self.retry_delay_seconds)
        assert last_error is not None
        raise last_error

    def search_videos(self, keyword: str, *, page: int, max_videos: int) -> list[VideoReference]:
        if max_videos <= 0:
            return []
        payload = self.get_json("item_search_video", {"q": keyword, "page": page})
        items = payload.get("items") or {}
        raw_results = items.get("item") if isinstance(items, dict) else None
        if raw_results is None:
            raw_results = payload.get("item") or []
        if isinstance(raw_results, dict):
            raw_results = [raw_results]

        references: list[VideoReference] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            gid = str(item.get("num_iid") or item.get("item_id") or "").strip()
            if not gid:
                continue
            video_url = build_douyin_video_url(gid)
            references.append(
                VideoReference(
                    source_url=str(item.get("detail_url") or video_url),
                    gid=gid,
                    video_url=video_url,
                    keyword=keyword,
                )
            )
            if len(references) >= max(0, max_videos):
                break
        return references

    def video_download_url(self, gid: str) -> str:
        payload = self.get_json("item_get_video", {"item_id": gid})
        item = payload.get("item") or payload
        video = item.get("video") or {}
        video_url = video.get("url") or video.get("video_url")
        if not video_url:
            raise RuntimeError("Wanbang item_get_video response missing item.video.url")
        return str(video_url)


def dedupe_references(references: Iterable[VideoReference]) -> list[VideoReference]:
    seen: set[str] = set()
    deduped: list[VideoReference] = []
    for reference in references:
        key = reference.gid or f"{reference.source_url}\0{reference.keyword}"
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(reference)
    return deduped


def resolve_references(
    *,
    urls: Iterable[str] = (),
    gids: Iterable[str] = (),
    keywords: Iterable[str] = (),
    client: WanbangClient | None = None,
    page: int = 1,
    max_per_keyword: int = 12,
    short_url_timeout: int = 20,
) -> list[VideoReference]:
    references: list[VideoReference] = []
    references.extend(resolve_url_reference(value, timeout=short_url_timeout) for value in urls)
    references.extend(resolve_gid_reference(value) for value in gids)
    for raw_keyword in keywords:
        keyword = str(raw_keyword or "").strip()
        if not keyword:
            continue
        if client is None:
            raise RuntimeError("Wanbang credentials are required for keyword search.")
        references.extend(client.search_videos(keyword, page=page, max_videos=max_per_keyword))
    return dedupe_references(references)


def validate_mp4_file(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < 1024:
            return False
        with path.open("rb") as file:
            if b"ftyp" not in file.read(64):
                return False
    except OSError:
        return False

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return True
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0 and float(result.stdout.strip() or 0) > 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def download_file(url: str, target_path: Path, *, referer: str = "https://www.douyin.com/") -> int:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": referer,
            "Accept": "*/*",
        },
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = target_path.with_suffix(target_path.suffix + ".part")
    part_path.unlink(missing_ok=True)
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            with part_path.open("wb") as file:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    file.write(chunk)
                    size += len(chunk)
        if not validate_mp4_file(part_path):
            raise RuntimeError("Downloaded video failed MP4 validation")
        part_path.replace(target_path)
        return target_path.stat().st_size
    except BaseException:
        part_path.unlink(missing_ok=True)
        raise


def reference_manifest(references: Iterable[VideoReference]) -> dict[str, Any]:
    return {
        "schema_version": REFERENCE_SCHEMA_VERSION,
        "generator": "douyin-video-toolkit",
        "items": [asdict(reference) for reference in references],
    }


def write_reference_manifest(references: Iterable[VideoReference], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reference_manifest(references), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_reference_manifest(path: Path) -> list[VideoReference]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != REFERENCE_SCHEMA_VERSION:
        raise RuntimeError(f"Unsupported Douyin reference schema: {payload.get('schema_version')}")
    if payload.get("generator") != "douyin-video-toolkit":
        raise RuntimeError("Reference manifest was not generated by douyin-video-toolkit")
    items = payload.get("items")
    if not isinstance(items, list):
        raise RuntimeError("Douyin reference manifest items must be a list")
    return [VideoReference(**item) for item in items]
