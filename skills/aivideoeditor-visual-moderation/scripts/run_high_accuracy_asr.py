#!/usr/bin/env python3
"""Generate timestamped transcript JSON for keyword redaction workflows."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".m4v"}
FUNASR_DEFAULT_MODEL = "paraformer-zh"
WHISPER_DEFAULT_MODEL = "large-v3"


def resolve_ffmpeg(value: str | Path | None = None) -> Path:
    if value:
        path = Path(value)
        if path.exists():
            return path
        resolved = shutil.which(str(value))
        if resolved:
            return Path(resolved)
        raise RuntimeError(f"ffmpeg not found: {value}")

    resolved = shutil.which("ffmpeg")
    if resolved:
        return Path(resolved)

    candidates = (
        Path.cwd() / "material_remix_desktop_source" / "bin" / "ffmpeg.exe",
        Path.cwd() / "bin" / "ffmpeg.exe",
        Path(__file__).resolve().parents[4] / "material_remix_desktop_source" / "bin" / "ffmpeg.exe",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError("ffmpeg not found. Pass --ffmpeg or put ffmpeg on PATH.")


def normalize_time_seconds(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        number = 0.0
    # FunASR sentence_info normally reports milliseconds.
    if number > 1000.0:
        number /= 1000.0
    return number


def normalize_pair(pair: Any) -> Optional[Tuple[float, float]]:
    if not isinstance(pair, (list, tuple)) or len(pair) < 2:
        return None
    start = normalize_time_seconds(pair[0])
    end = normalize_time_seconds(pair[1])
    if start is None or end is None:
        return None
    if end < start:
        start, end = end, start
    return round(start, 3), round(end, 3)


def clean_text(value: Any) -> str:
    text = str(value or "").replace("\u3000", " ").strip()
    return " ".join(text.split()) if " " in text else text


def text_units(text: str) -> List[str]:
    # Chinese policy matching works best when each CJK character can carry its
    # own timestamp; whitespace is not meaningful for keyword spans.
    return [char for char in text if not char.isspace()]


def words_from_timestamps(text: str, timestamps: Any) -> List[Dict[str, Any]]:
    if not isinstance(timestamps, list):
        return []
    pairs = [normalize_pair(item) for item in timestamps]
    pairs = [item for item in pairs if item is not None]
    if not pairs:
        return []

    units = text_units(text)
    if not units:
        return []

    words: List[Dict[str, Any]] = []
    if len(pairs) == len(units):
        for unit, (start, end) in zip(units, pairs):
            words.append({"word": unit, "start_time": start, "end_time": end})
        return words

    if len(pairs) < len(units):
        # Some FunASR models emit timestamps for acoustic tokens rather than
        # literal characters. Spread characters across the returned intervals
        # so downstream matching still gets narrower-than-segment windows.
        for index, unit in enumerate(units):
            pair_index = min(len(pairs) - 1, int(index * len(pairs) / max(1, len(units))))
            start, end = pairs[pair_index]
            words.append({"word": unit, "start_time": start, "end_time": end})
        return words

    # More timestamps than characters: keep the first matching count and ignore
    # non-text acoustic tokens.
    for unit, (start, end) in zip(units, pairs[: len(units)]):
        words.append({"word": unit, "start_time": start, "end_time": end})
    return words


def segment_from_words(text: str, words: Sequence[Dict[str, Any]], fallback_start: float, fallback_end: float) -> Dict[str, Any]:
    if words:
        start = min(float(item["start_time"]) for item in words)
        end = max(float(item["end_time"]) for item in words)
    else:
        start = fallback_start
        end = fallback_end
    return {
        "start_time": round(float(start), 3),
        "end_time": round(float(max(start, end)), 3),
        "text": clean_text(text),
        "words": list(words),
    }


def select_backend(value: str) -> str:
    if value != "auto":
        return value
    try:
        import funasr  # noqa: F401

        return "funasr"
    except Exception:
        pass
    try:
        import whisper  # noqa: F401

        return "whisper"
    except Exception:
        pass
    raise RuntimeError("No ASR backend found. Install funasr or openai-whisper.")


def default_model(backend: str, model: Optional[str]) -> str:
    if model:
        return model
    if backend == "funasr":
        return FUNASR_DEFAULT_MODEL
    if backend == "whisper":
        return WHISPER_DEFAULT_MODEL
    raise RuntimeError(f"Unsupported ASR backend: {backend}")


def resolve_device(value: str) -> str:
    if value != "auto":
        return value
    try:
        import torch  # type: ignore

        return "cuda" if bool(torch.cuda.is_available()) else "cpu"
    except Exception:
        return "cpu"


def normalize_model_alias(backend: str, model: str) -> str:
    value = model.strip()
    if backend == "funasr":
        aliases = {
            "sensevoice": "iic/SenseVoiceSmall",
            "sensevoice-small": "iic/SenseVoiceSmall",
            "sensevoicesmall": "iic/SenseVoiceSmall",
            "paraformer": "paraformer-zh",
            "paraformer-large": "paraformer-zh",
        }
        return aliases.get(value.casefold(), value)
    return value


def extract_audio(input_path: Path, audio_path: Path, ffmpeg: Path) -> None:
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        str(ffmpeg),
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-acodec",
        "pcm_s16le",
        str(audio_path),
    ]
    completed = subprocess.run(args, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()[-8:]
        raise RuntimeError("ffmpeg audio extraction failed: " + "\n".join(detail))


def normalize_whisper_result(
    result: Dict[str, Any],
    *,
    source: Path,
    backend: str,
    model: str,
    language: str,
    audio_path: Path,
) -> Dict[str, Any]:
    segments: List[Dict[str, Any]] = []
    for raw in result.get("segments") or []:
        text = clean_text(raw.get("text"))
        if not text:
            continue
        words: List[Dict[str, Any]] = []
        for item in raw.get("words") or []:
            word = clean_text(item.get("word"))
            start = normalize_time_seconds(item.get("start"))
            end = normalize_time_seconds(item.get("end"))
            if not word or start is None or end is None:
                continue
            if end < start:
                start, end = end, start
            words.append(
                {
                    "word": word,
                    "start_time": round(float(start), 3),
                    "end_time": round(float(end), 3),
                    "confidence": item.get("probability"),
                }
            )
        start = normalize_time_seconds(raw.get("start")) or 0.0
        end = normalize_time_seconds(raw.get("end")) or start
        segments.append(segment_from_words(text, words, start, end))

    return transcript_payload(source, backend, model, language, audio_path, segments)


def normalize_funasr_item(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    segments: List[Dict[str, Any]] = []
    sentence_info = raw.get("sentence_info") or raw.get("sentences")
    if isinstance(sentence_info, list) and sentence_info:
        for item in sentence_info:
            if not isinstance(item, dict):
                continue
            text = clean_text(item.get("text") or item.get("sentence"))
            if not text:
                continue
            start = normalize_time_seconds(item.get("start_time", item.get("start"))) or 0.0
            end = normalize_time_seconds(item.get("end_time", item.get("end"))) or start
            timestamps = item.get("timestamp") or item.get("timestamps")
            words = words_from_timestamps(text, timestamps)
            segments.append(segment_from_words(text, words, start, end))
        return segments

    text = clean_text(raw.get("text"))
    if text:
        words = words_from_timestamps(text, raw.get("timestamp") or raw.get("timestamps"))
        start = min([float(item["start_time"]) for item in words], default=0.0)
        end = max([float(item["end_time"]) for item in words], default=start)
        segments.append(segment_from_words(text, words, start, end))
    return segments


def normalize_funasr_result(
    result: Any,
    *,
    source: Path,
    backend: str,
    model: str,
    language: str,
    audio_path: Path,
) -> Dict[str, Any]:
    records = result if isinstance(result, list) else [result]
    segments: List[Dict[str, Any]] = []
    for record in records:
        if isinstance(record, dict):
            segments.extend(normalize_funasr_item(record))
    segments.sort(key=lambda item: (float(item["start_time"]), float(item["end_time"])))
    return transcript_payload(source, backend, model, language, audio_path, segments)


def transcript_payload(
    source: Path,
    backend: str,
    model: str,
    language: str,
    audio_path: Path,
    segments: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "source": str(source),
        "engine": f"{backend}:{model}",
        "backend": backend,
        "model": model,
        "language": language,
        "audio_path": str(audio_path),
        "word_timestamps_available": any(bool(item.get("words")) for item in segments),
        "segments": segments,
    }


def transcribe_with_whisper(audio_path: Path, *, source: Path, model: str, language: str, device: str) -> Dict[str, Any]:
    try:
        import whisper  # type: ignore
    except Exception as exc:
        raise RuntimeError("openai-whisper is not installed. Install with: pip install -U openai-whisper") from exc

    loaded = whisper.load_model(model, device=device)
    result = loaded.transcribe(
        str(audio_path),
        language=language or None,
        task="transcribe",
        word_timestamps=True,
        verbose=False,
        fp16=device.startswith("cuda"),
    )
    return normalize_whisper_result(result, source=source, backend="whisper", model=model, language=language, audio_path=audio_path)


def ensure_ffmpeg_on_path(ffmpeg_path: Path) -> None:
    directory = str(ffmpeg_path.parent)
    current = os.environ.get("PATH") or ""
    parts = current.split(os.pathsep) if current else []
    if directory not in parts:
        os.environ["PATH"] = directory + (os.pathsep + current if current else "")


def call_generate(model_obj: Any, audio_path: Path, language: str, batch_size_s: int) -> Any:
    candidates = [
        {
            "input": str(audio_path),
            "language": language,
            "use_itn": True,
            "batch_size_s": batch_size_s,
            "sentence_timestamp": True,
            "merge_vad": True,
            "merge_length_s": 15,
        },
        {
            "input": str(audio_path),
            "language": language,
            "use_itn": True,
            "batch_size_s": batch_size_s,
            "sentence_timestamp": True,
        },
        {"input": str(audio_path), "batch_size_s": batch_size_s, "sentence_timestamp": True},
        {"input": str(audio_path), "batch_size_s": batch_size_s},
    ]
    last_error: Optional[Exception] = None
    for kwargs in candidates:
        try:
            return model_obj.generate(**kwargs)
        except TypeError as exc:
            last_error = exc
    if last_error:
        raise last_error
    raise RuntimeError("FunASR generate failed before invocation.")


def transcribe_with_funasr(
    audio_path: Path,
    *,
    source: Path,
    model: str,
    language: str,
    device: str,
    batch_size_s: int,
) -> Dict[str, Any]:
    try:
        from funasr import AutoModel  # type: ignore
    except Exception as exc:
        raise RuntimeError("funasr is not installed. Install with: pip install -U funasr") from exc

    model_name = normalize_model_alias("funasr", model)
    init_candidates: List[Dict[str, Any]] = []
    if "sensevoice" in model_name.casefold():
        init_candidates.append(
            {
                "model": model_name,
                "trust_remote_code": True,
                "vad_model": "fsmn-vad",
                "vad_kwargs": {"max_single_segment_time": 30000},
                "device": device,
            }
        )
    init_candidates.extend(
        [
            {"model": model_name, "vad_model": "fsmn-vad", "punc_model": "ct-punc-c", "device": device},
            {"model": model_name, "vad_model": "fsmn-vad", "device": device},
            {"model": model_name, "device": device},
            {"model": model_name},
        ]
    )

    last_error: Optional[Exception] = None
    model_obj: Any = None
    for kwargs in init_candidates:
        try:
            model_obj = AutoModel(**kwargs)
            break
        except TypeError as exc:
            last_error = exc
    if model_obj is None:
        if last_error:
            raise last_error
        raise RuntimeError(f"Failed to initialize FunASR model: {model_name}")

    result = call_generate(model_obj, audio_path, language, batch_size_s)
    return normalize_funasr_result(result, source=source, backend="funasr", model=model_name, language=language, audio_path=audio_path)


def transcribe_source_to_json(
    input_path: Path,
    output_path: Path,
    *,
    backend: str = "auto",
    model: Optional[str] = None,
    language: str = "zh",
    device: str = "auto",
    ffmpeg: str | Path | None = None,
    batch_size_s: int = 300,
    keep_audio: bool = False,
    human_report: Path | None = None,
) -> Dict[str, Any]:
    source = input_path.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Input media not found: {source}")
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    selected_backend = select_backend(backend)
    selected_model = normalize_model_alias(selected_backend, default_model(selected_backend, model))
    selected_device = resolve_device(device)
    audio_path = output_path.with_name(output_path.stem + "_16k.wav")
    ffmpeg_path = resolve_ffmpeg(ffmpeg)
    extract_audio(source, audio_path, ffmpeg_path)

    try:
        if selected_backend == "funasr":
            payload = transcribe_with_funasr(
                audio_path,
                source=source,
                model=selected_model,
                language=language,
                device=selected_device,
                batch_size_s=batch_size_s,
            )
        elif selected_backend == "whisper":
            ensure_ffmpeg_on_path(ffmpeg_path)
            payload = transcribe_with_whisper(
                audio_path,
                source=source,
                model=selected_model,
                language=language,
                device=selected_device,
            )
        else:
            raise RuntimeError(f"Unsupported ASR backend: {selected_backend}")
    finally:
        if not keep_audio:
            try:
                audio_path.unlink(missing_ok=True)
            except TypeError:
                if audio_path.exists():
                    audio_path.unlink()

    if not keep_audio:
        payload["audio_path"] = None
    payload["device"] = selected_device
    payload["segment_count"] = len(payload.get("segments") or [])
    payload["word_count"] = sum(len(item.get("words") or []) for item in payload.get("segments") or [])
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if human_report is None:
        human_report = output_path.parent / "音频转写说明.txt"
    write_human_report(human_report, payload, output_path)
    payload["human_report"] = str(human_report)
    return payload


def write_human_report(path: Path, payload: Dict[str, Any], output_path: Path) -> None:
    lines = [
        "音频转写说明",
        "",
        f"源文件: {payload.get('source')}",
        f"字幕JSON: {output_path}",
        f"转写引擎: {payload.get('engine')}",
        f"运行设备: {payload.get('device')}",
        f"片段数量: {payload.get('segment_count')}",
        f"词/字级时间戳数量: {payload.get('word_count')}",
        f"词/字级时间戳可用: {bool(payload.get('word_timestamps_available'))}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate timestamped transcript JSON with open-source ASR.")
    parser.add_argument("--input", type=Path, required=True, help="Input video or audio file.")
    parser.add_argument("--output", type=Path, required=True, help="Output transcript JSON.")
    parser.add_argument("--backend", choices=["auto", "funasr", "whisper"], default="auto")
    parser.add_argument("--model", help="Backend model. Defaults: funasr=paraformer-zh, whisper=large-v3.")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--device", default="auto", help="auto, cpu, cuda, cuda:0, etc.")
    parser.add_argument("--ffmpeg")
    parser.add_argument("--batch-size-s", type=int, default=300)
    parser.add_argument("--keep-audio", action="store_true")
    parser.add_argument("--human-report", type=Path)
    args = parser.parse_args()

    payload = transcribe_source_to_json(
        args.input,
        args.output,
        backend=args.backend,
        model=args.model,
        language=args.language,
        device=args.device,
        ffmpeg=args.ffmpeg,
        batch_size_s=args.batch_size_s,
        keep_audio=args.keep_audio,
        human_report=args.human_report,
    )
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "human_report": payload.get("human_report"),
                "engine": payload.get("engine"),
                "segments": payload.get("segment_count"),
                "word_timestamps_available": payload.get("word_timestamps_available"),
                "word_count": payload.get("word_count"),
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
