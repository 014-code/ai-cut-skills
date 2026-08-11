"""Windows-safe alpha capability and filter-chain checks for Soda renders.

HEVC Alpha is an auxiliary layer.  ``ffprobe`` often reports the colour
layer's ``yuv420p`` even when FFmpeg can expose the auxiliary layer as RGBA,
so the gate below deliberately samples decoded frames instead of trusting a
codec/pixel-format version heuristic.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable


SKILL_VERSION = "2026.08.11-alpha-safe.4"
FORBIDDEN_ALPHA_FILTERS = ("colorkey", "chromakey", "screen", "blend")
_STAT_RE = re.compile(r"lavfi\.signalstats\.Y(MIN|MAX)=(-?\d+(?:\.\d+)?)")
_FORMAT_RE = re.compile(r"\bfmt:([A-Za-z0-9_]+)")


class AlphaSafetyError(RuntimeError):
    pass


def validate_video_material_policy(material: dict[str, Any], *, label: str) -> dict[str, Any]:
    """Validate and normalize the explicit video transparency/playback contract."""

    if str(material.get("kind", "")).casefold() != "video":
        return {}
    transparency_mode = str(material.get("transparency_mode", "")).casefold()
    if transparency_mode not in {"opaque", "embedded_alpha"}:
        raise AlphaSafetyError(
            f"{label}.transparency_mode must be opaque or embedded_alpha for video materials"
        )
    playback_mode = str(material.get("playback_mode", "once_hold_last")).casefold()
    if playback_mode not in {"once", "once_hold_last", "loop"}:
        raise AlphaSafetyError(
            f"{label}.playback_mode must be once, once_hold_last, or loop"
        )
    include_audio = material.get("include_audio", False)
    if not isinstance(include_audio, bool):
        raise AlphaSafetyError(f"{label}.include_audio must be a boolean")
    try:
        audio_gain_db = float(material.get("audio_gain_db", -3.0))
    except (TypeError, ValueError) as exc:
        raise AlphaSafetyError(f"{label}.audio_gain_db must be numeric") from exc
    if not -60.0 <= audio_gain_db <= 12.0:
        raise AlphaSafetyError(f"{label}.audio_gain_db must be between -60 and 12 dB")
    return {
        "transparency_mode": transparency_mode,
        "playback_mode": playback_mode,
        "include_audio": include_audio,
        "audio_gain_db": audio_gain_db,
    }


def binary_path(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise AlphaSafetyError(f"Required binary not found: {name}")
    return str(Path(path).resolve())


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def runtime_evidence() -> dict[str, Any]:
    """Return immutable evidence for the exact FFmpeg binaries used by this process."""

    result: dict[str, Any] = {
        "skill_version": SKILL_VERSION,
        "ffmpeg_path": None,
        "ffprobe_path": None,
        "ffmpeg_version": None,
        "ffprobe_version": None,
        "errors": [],
    }
    for name in ("ffmpeg", "ffprobe"):
        try:
            path = binary_path(name)
        except AlphaSafetyError as exc:
            result["errors"].append(str(exc))
            continue
        result[f"{name}_path"] = path
        version = _run([path, "-version"])
        first_line = (version.stdout or version.stderr).splitlines()
        result[f"{name}_version"] = first_line[0] if first_line else None
        if version.returncode != 0:
            result["errors"].append(f"{name} -version failed ({version.returncode})")
    result["ok"] = not result["errors"]
    return result


def assert_safe_filter_graph(filter_graph: str) -> None:
    """Reject chroma-key/screen fallbacks; alpha must remain an actual channel."""

    lowered = str(filter_graph).casefold()
    found = [token for token in FORBIDDEN_ALPHA_FILTERS if token in lowered]
    if found:
        raise AlphaSafetyError(
            "透明素材禁止使用黑色抠像、Screen 或 blend 降级滤镜：" + ", ".join(found)
        )


def _probe_duration(path: Path) -> float:
    ffprobe = binary_path("ffprobe")
    result = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    if result.returncode != 0:
        raise AlphaSafetyError(f"ffprobe failed for {path}: {result.stderr.strip()}")
    try:
        return max(0.0, float(json.loads(result.stdout).get("format", {}).get("duration") or 0.0))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AlphaSafetyError(f"Unable to read duration for {path}") from exc


def _probe_video_size(path: Path) -> tuple[int, int]:
    ffprobe = binary_path("ffprobe")
    result = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(path),
        ]
    )
    if result.returncode != 0:
        raise AlphaSafetyError(f"ffprobe failed for {path}: {result.stderr.strip()}")
    try:
        stream = json.loads(result.stdout).get("streams", [])[0]
        return int(stream["width"]), int(stream["height"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AlphaSafetyError(f"Unable to read video size for {path}") from exc


def _parse_sample(stderr: str) -> dict[str, Any]:
    values = {match.group(1).lower(): float(match.group(2)) for match in _STAT_RE.finditer(stderr)}
    formats = _FORMAT_RE.findall(stderr)
    alpha_modes = re.findall(r"\balpha_mode:([A-Za-z0-9_]+)", stderr)
    return {
        "decoded_pixel_formats": sorted(set(formats)),
        "alpha_modes": sorted(set(alpha_modes)),
        "alpha_min": values.get("min"),
        "alpha_max": values.get("max"),
    }


def _rgba_pixel_evidence(
    ffmpeg: str,
    source: Path,
    timestamp: float,
    width: int,
    height: int,
) -> dict[str, Any]:
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{timestamp:.6f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            "format=rgba",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgba",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    expected = width * height * 4
    if result.returncode != 0 or len(result.stdout) < expected:
        return {
            "ok": False,
            "error": result.stderr.decode("utf-8", errors="replace").strip(),
        }
    pixels = memoryview(result.stdout)[:expected]
    total = width * height
    transparent = 0
    opaque = 0
    opaque_black = 0
    for offset in range(0, expected, 4):
        red, green, blue, alpha = pixels[offset : offset + 4]
        if alpha <= 5:
            transparent += 1
        if alpha >= 250:
            opaque += 1
            if max(red, green, blue) <= 32:
                opaque_black += 1
    return {
        "ok": True,
        "timestamp": timestamp,
        "total_pixels": total,
        "transparent_pixels": transparent,
        "opaque_pixels": opaque,
        "opaque_black_pixels": opaque_black,
        "transparent_ratio": round(transparent / total, 6),
        "opaque_ratio": round(opaque / total, 6),
        "opaque_black_ratio": round(opaque_black / total, 6),
    }


def inspect_embedded_alpha(
    path: Path,
    *,
    sample_points: Iterable[float] = (0.1, 0.5, 0.9),
) -> dict[str, Any]:
    """Decode representative frames and inspect the actual Alpha plane.

    The ``showinfo`` filter is intentionally before ``format=rgba``.  A
    decoder that ignores HEVC's auxiliary layer will report a YUV format and
    the subsequent ``alphaextract`` will be constant 255, which fails closed.
    """

    source = Path(path).expanduser().resolve()
    if not source.is_file():
        return {"ok": False, "path": str(source), "error": "file_not_found"}
    try:
        duration = _probe_duration(source)
        width, height = _probe_video_size(source)
        ffmpeg = binary_path("ffmpeg")
    except AlphaSafetyError as exc:
        return {"ok": False, "path": str(source), "error": str(exc)}

    points = [max(0.0, min(1.0, float(point))) for point in sample_points]
    samples: list[dict[str, Any]] = []
    errors: list[str] = []
    for point in points:
        timestamp = 0.0 if duration <= 0 else min(duration, duration * point)
        filter_graph = "showinfo,format=rgba,alphaextract,signalstats,metadata=print"
        result = _run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "info",
                "-ss",
                f"{timestamp:.6f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                filter_graph,
                "-f",
                "null",
                "-",
            ]
        )
        parsed = _parse_sample((result.stdout or "") + "\n" + (result.stderr or ""))
        parsed["point"] = point
        parsed["timestamp"] = round(timestamp, 6)
        parsed["command"] = [*result.args]
        samples.append(parsed)
        if result.returncode != 0:
            errors.append(f"sample {point:g} failed ({result.returncode})")
        if parsed["alpha_min"] is None or parsed["alpha_max"] is None:
            errors.append(f"sample {point:g} did not expose alpha statistics")

    alpha_mins = [float(item["alpha_min"]) for item in samples if item.get("alpha_min") is not None]
    alpha_maxes = [float(item["alpha_max"]) for item in samples if item.get("alpha_max") is not None]
    decoded_formats = sorted({fmt for item in samples for fmt in item.get("decoded_pixel_formats", [])})
    alpha_modes = sorted({mode for item in samples for mode in item.get("alpha_modes", [])})
    alpha_min = min(alpha_mins) if alpha_mins else None
    alpha_max = max(alpha_maxes) if alpha_maxes else None
    non_constant = bool(alpha_min is not None and alpha_min < 255.0 and alpha_max is not None and alpha_max > 0.0)
    decoded_alpha = any("a" in fmt.casefold() for fmt in decoded_formats) or bool(alpha_modes)
    pixel_evidence = _rgba_pixel_evidence(
        ffmpeg,
        source,
        duration * 0.5,
        width,
        height,
    )
    ok = not errors and decoded_alpha and non_constant
    if not decoded_alpha:
        errors.append(f"decoded frames exposed no alpha-capable pixel format: {decoded_formats}")
    if not non_constant:
        errors.append(f"decoded alpha is constant/opaque (min={alpha_min}, max={alpha_max})")
    if not pixel_evidence.get("ok"):
        errors.append("unable to inspect decoded RGBA pixels at the midpoint")
        ok = False
    return {
        "ok": ok,
        "path": str(source),
        "duration": duration,
        "sample_points": points,
        "decoded_pixel_formats": decoded_formats,
        "alpha_modes": alpha_modes,
        "alpha_min": alpha_min,
        "alpha_max": alpha_max,
        "non_constant_alpha": non_constant,
        "midpoint_pixel_evidence": pixel_evidence,
        "samples": samples,
        "errors": errors,
        "filter_graph": filter_graph,
    }


def render_alpha_qa_frames(path: Path, output_dir: Path) -> dict[str, Any]:
    """Composite the decoded alpha frame on checker, light, and dark backgrounds."""

    source = Path(path).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    try:
        duration = _probe_duration(source)
        width, height = _probe_video_size(source)
        ffmpeg = binary_path("ffmpeg")
    except AlphaSafetyError as exc:
        return {"ok": False, "path": str(source), "error": str(exc), "outputs": {}}
    timestamp = duration * 0.5
    backgrounds = {
        "checker": (
            f"nullsrc=s={width}x{height}:d=0.1,format=rgb24,"
            "geq=r='if(eq(mod(floor(X/144)+floor(Y/144),2),0),240,150)':"
            "g='if(eq(mod(floor(X/144)+floor(Y/144),2),0),240,150)':"
            "b='if(eq(mod(floor(X/144)+floor(Y/144),2),0),240,150)'"
        ),
        "light": f"color=c=#F2F2F2:s={width}x{height}:d=0.1",
        "dark": f"color=c=#202020:s={width}x{height}:d=0.1",
    }
    filter_graph = "[0:v]format=rgba[bg];[1:v]format=rgba[fg];[bg][fg]overlay=0:0:format=auto[vout]"
    assert_safe_filter_graph(filter_graph)
    outputs: dict[str, str] = {}
    errors: list[str] = []
    for name, background in backgrounds.items():
        target = output / f"{name}.png"
        result = _run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                background,
                "-ss",
                f"{timestamp:.6f}",
                "-i",
                str(source),
                "-filter_complex",
                filter_graph,
                "-map",
                "[vout]",
                "-frames:v",
                "1",
                "-update",
                "1",
                "-y",
                str(target),
            ]
        )
        if result.returncode != 0:
            errors.append(f"{name}: {result.stderr.strip()}")
        else:
            outputs[name] = str(target)
    return {
        "ok": not errors and len(outputs) == len(backgrounds),
        "path": str(source),
        "timestamp": timestamp,
        "outputs": outputs,
        "filter_graph": filter_graph,
        "errors": errors,
    }
