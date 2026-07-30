from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
import fnmatch
import json
import os
from pathlib import Path
import sys
import traceback
from typing import Any, Iterable

from usergrowth_automation.usergrowth_browser import UserGrowthBrowserClient
from usergrowth_automation.usergrowth_excel import load_song_records, write_back_results
from usergrowth_automation.usergrowth_models import (
    VIDEO_SUFFIXES,
    UserGrowthOrderPlan,
    UserGrowthRunConfig,
    UserGrowthVideoItem,
)
from usergrowth_automation.usergrowth_planner import (
    _attach_order,
    _attach_song,
    build_song_batches_from_paths,
    load_song_records_for_split,
    scan_video_files,
)
from usergrowth_automation.usergrowth_rules import (
    classification_path_for_material,
    detect_material_type,
    extract_song_name,
    optional_tags_for_file,
)
from usergrowth_automation.usergrowth_tag_templates import (
    DEFAULT_CUSTOM_TAG_TEMPLATE_NAME,
    combine_template_tags,
    default_custom_tag_template_tags,
    normalise_template_payload,
    normalise_template_tags,
)
from usergrowth_automation.usergrowth_runner import (
    _backfill_lock,
    _build_payload,
    _clamp_batch_concurrency,
    _emit,
    _emit_song_match_logs,
    _safe_name,
    _write_log,
)


ProgressCallback = Any
SELECTION_MANIFEST_KEYS = {"videos", "video_globs", "video_glob", "video_list", "all_videos"}


@dataclass
class SelectedBatchSpec:
    index: int
    label: str
    config: UserGrowthRunConfig
    video_paths: list[Path]


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    args = parse_args(argv)
    cli_error_output_root: Path | None = None
    try:
        manifest_path = Path(args.manifest).resolve() if args.manifest else None
        manifest = _read_manifest(manifest_path)
        base_dir = manifest_path.parent if manifest_path else Path.cwd()
        if _manifest_has_batches(manifest):
            cli_error_output_root = _manifest_output_root(args, manifest, base_dir)
            payload = run_batch_manifest_task(
                args,
                manifest,
                base_dir,
                progress=lambda message: print(message, flush=True),
            )
            print(json.dumps(_public_payload(payload), ensure_ascii=False, indent=2), flush=True)
            summary = payload.get("summary", {})
            return 1 if summary.get("failed") or summary.get("cancelled") else 0

        config = _config_from_args(args, manifest, base_dir)
        cli_error_output_root = config.output_root
        selectors = _video_selectors_from_args(args, manifest)
        glob_patterns = _list_value(args.video_glob) + _list_from_manifest(manifest, "video_globs")
        auto_split = _auto_split_requested(args, manifest)
        has_explicit_selection = bool(
            selectors
            or glob_patterns
            or args.all_videos
            or manifest.get("all_videos")
        )
        video_paths = resolve_video_selection(
            config.video_folder,
            selectors=selectors,
            glob_patterns=glob_patterns,
            recursive=config.recursive,
            all_videos=bool(args.all_videos or manifest.get("all_videos") or (auto_split and not has_explicit_selection)),
        )
        if not video_paths:
            raise RuntimeError("没有选中任何视频。请使用 --video/--video-glob/--video-list，或显式传 --all-videos。")
        if auto_split:
            payload = run_auto_split_task(
                args,
                manifest,
                base_dir,
                config,
                video_paths,
                progress=lambda message: print(message, flush=True),
            )
            print(json.dumps(_public_payload(payload), ensure_ascii=False, indent=2), flush=True)
            summary = payload.get("summary", {})
            return 1 if summary.get("failed") or summary.get("cancelled") else 0
        if not config.dry_run and not (args.confirm_live or manifest.get("confirm_live")):
            raise RuntimeError("正式上传需要同时传 --live --confirm-live。")
        if not config.dry_run and (not config.account or not config.password):
            raise RuntimeError("正式上传需要账号密码。可用 --account/--password 或 USERGROWTH_ACCOUNT/USERGROWTH_PASSWORD。")

        payload = run_selected_usergrowth_task(
            config,
            video_paths,
            progress=lambda message: print(message, flush=True),
        )
        print(json.dumps(_public_payload(payload), ensure_ascii=False, indent=2), flush=True)
        return 0
    except KeyboardInterrupt as exc:
        _write_cli_error(cli_error_output_root, exc)
        print("ERROR: interrupted", file=sys.stderr, flush=True)
        return 130
    except Exception as exc:  # noqa: BLE001
        _write_cli_error(cli_error_output_root, exc)
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Standalone UserGrowth upload runner bundled with the Codex skill.",
    )
    parser.add_argument("--manifest", help="JSON config file. Relative paths inside it resolve from the manifest folder.")
    parser.add_argument("--video-folder", help="Folder containing source videos.")
    parser.add_argument("--video", action="append", default=[], help="Selected video path/name/stem. Can be repeated.")
    parser.add_argument("--video-glob", action="append", default=[], help="Glob matched against relative path and file name.")
    parser.add_argument("--video-list", help="Text file with one selected video path/name per line.")
    parser.add_argument("--all-videos", action="store_true", help="Select all videos in video-folder.")
    parser.add_argument("--backfill-excel", help="Backfill Excel path.")
    parser.add_argument("--song-excel", help="Song library Excel path.")
    parser.add_argument("--output-root", help="Output folder for task.json, logs, dry-run result.xlsx, and debug files.")
    parser.add_argument("--order-id", help="UserGrowth order ID.")
    parser.add_argument("--task-name", default=None, help="Task folder name suffix.")
    parser.add_argument("--month-tag", default=None, help="Custom month tag, e.g. 26年7月dxqs.")
    parser.add_argument("--custom-tag-template-name", help="Custom tag template name stored in task.json.")
    parser.add_argument("--custom-tag", action="append", default=[], help="Custom tag template line. Can be repeated.")
    parser.add_argument("--recursive", dest="recursive", action="store_true", default=None)
    parser.add_argument("--no-recursive", dest="recursive", action="store_false")
    parser.add_argument("--live", action="store_true", help="Run real browser upload. Omit for dry-run.")
    parser.add_argument("--confirm-live", action="store_true", help="Required with --live to allow real upload/review/backfill.")
    parser.add_argument("--headless", action="store_true", help="Run browser headless in live mode.")
    parser.add_argument("--split-by-song", action="store_true", help="Auto-split selected videos into one batch per song before running.")
    parser.add_argument("--concurrency", type=int, default=None, help="Batch concurrency. Defaults to batch count, clamped to 1..10; multi-batch runs use at least 2.")
    parser.add_argument("--account", help="UserGrowth account. Prefer USERGROWTH_ACCOUNT env var.")
    parser.add_argument("--password", help="UserGrowth password. Prefer USERGROWTH_PASSWORD env var.")
    parser.add_argument("--max-status-retries", type=int, default=None)
    parser.add_argument("--refresh-interval-seconds", type=float, default=None)
    parser.add_argument("--browser-slow-mo-ms", type=int, default=None)
    return parser.parse_args(argv)


def _read_manifest(path: Path | None) -> dict[str, Any]:
    if not path:
        return {}
    with path.open("r", encoding="utf-8") as fp:
        payload = json.load(fp)
    if not isinstance(payload, dict):
        raise RuntimeError("manifest 必须是 JSON object。")
    return payload


def _manifest_has_batches(manifest: dict[str, Any]) -> bool:
    return "batches" in manifest


def _auto_split_requested(args: argparse.Namespace, manifest: dict[str, Any]) -> bool:
    return bool(args.split_by_song or manifest.get("split_by_song") or manifest.get("auto_split_by_song"))


def run_batch_manifest_task(
    args: argparse.Namespace,
    manifest: dict[str, Any],
    base_dir: Path,
    progress: ProgressCallback | None = None,
) -> dict:
    """执行包含 batches 的 manifest；每个批次可选择不同视频、订单和目录。"""
    specs = build_batch_specs_from_manifest(args, manifest, base_dir)
    return run_batch_specs_task(
        args,
        manifest,
        base_dir,
        specs,
        progress=progress,
        batch_source="manifest",
    )


def run_auto_split_task(
    args: argparse.Namespace,
    manifest: dict[str, Any],
    base_dir: Path,
    config: UserGrowthRunConfig,
    video_paths: list[Path],
    progress: ProgressCallback | None = None,
) -> dict:
    song_records = load_song_records_for_split(config, progress=progress)
    batches = build_song_batches_from_paths(config, video_paths, song_records=song_records)
    if not batches:
        raise RuntimeError("自动拆批未生成可执行批次。")
    specs = [
        SelectedBatchSpec(
            index=index,
            label=_batch_label(index, batch_config, {"name": batch_config.batch_name}),
            config=batch_config,
            video_paths=list(batch_config.selected_video_paths),
        )
        for index, batch_config in enumerate(batches)
    ]
    return run_batch_specs_task(
        args,
        manifest,
        base_dir,
        specs,
        progress=progress,
        batch_source="auto_split_by_song",
    )


def run_batch_specs_task(
    args: argparse.Namespace,
    manifest: dict[str, Any],
    base_dir: Path,
    specs: list[SelectedBatchSpec],
    *,
    progress: ProgressCallback | None = None,
    batch_source: str = "manifest",
) -> dict:
    """执行已解析好的多批任务。"""
    if not specs:
        raise RuntimeError("manifest.batches 中没有可执行批次。")
    if any(not spec.config.dry_run for spec in specs) and not _live_confirmed(args, manifest):
        raise RuntimeError("多批正式上传需要在 manifest 顶层写 confirm_live=true，或命令行传 --live --confirm-live。")
    missing_credentials = [spec.label for spec in specs if not spec.config.dry_run and (not spec.config.account or not spec.config.password)]
    if missing_credentials:
        raise RuntimeError("以下批次缺少正式上传账号密码：" + "；".join(missing_credentials))

    requested_concurrency = _pick(args.concurrency, manifest, "concurrency")
    worker_count = _clamp_concurrency(requested_concurrency, len(specs))
    batch_root = _create_batch_root(args, manifest, specs, base_dir)
    source_text = "自动拆批" if batch_source == "auto_split_by_song" else "多批次"
    _emit(progress, f"开始{source_text}执行：{len(specs)} 批，并发 {worker_count}，汇总目录：{batch_root}")
    results = run_selected_usergrowth_batches(
        specs,
        concurrency=worker_count,
        progress=progress,
    )
    payload = _build_batch_payload(batch_root, specs, results, worker_count)
    payload["batch_source"] = batch_source
    if batch_source == "auto_split_by_song":
        payload["auto_split_by_song"] = True
    (batch_root / "batch_summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_batch_log(batch_root, payload)
    _emit(progress, f"{source_text}执行完成：成功 {payload['summary']['success']} 批，失败 {payload['summary']['failed']} 批")
    return payload


def build_batch_specs_from_manifest(
    args: argparse.Namespace,
    manifest: dict[str, Any],
    base_dir: Path,
) -> list[SelectedBatchSpec]:
    batch_entries = manifest.get("batches")
    if not isinstance(batch_entries, list) or not batch_entries:
        raise RuntimeError("manifest.batches 必须是非空数组。")
    common_manifest = {
        key: value
        for key, value in manifest.items()
        if key not in {"batches", "concurrency", *SELECTION_MANIFEST_KEYS}
    }
    specs: list[SelectedBatchSpec] = []
    for index, entry in enumerate(batch_entries):
        if not isinstance(entry, dict):
            raise RuntimeError(f"第 {index + 1} 个批次必须是 JSON object。")
        batch_manifest = _batch_manifest(common_manifest, entry, index)
        config = _config_from_args(args, batch_manifest, base_dir)
        selectors = _video_selectors_from_manifest(batch_manifest, base_dir)
        glob_patterns = (
            _list_from_manifest(batch_manifest, "video_globs")
            + _list_from_manifest(batch_manifest, "video_glob")
        )
        all_videos = bool(batch_manifest.get("all_videos"))
        video_paths = resolve_video_selection(
            config.video_folder,
            selectors=selectors,
            glob_patterns=glob_patterns,
            recursive=config.recursive,
            all_videos=all_videos,
        )
        if not video_paths:
            raise RuntimeError(f"第 {index + 1} 批没有选中任何视频，请设置 videos/video_globs/video_list 或 all_videos=true。")
        label = _batch_label(index, config, batch_manifest)
        specs.append(SelectedBatchSpec(index=index, label=label, config=config, video_paths=video_paths))
    return specs


def _batch_manifest(common_manifest: dict[str, Any], entry: dict[str, Any], index: int) -> dict[str, Any]:
    merged = dict(common_manifest)
    merged.update(entry)
    if entry.get("dry_run") is True and "live" not in entry:
        merged["live"] = False
    if not str(entry.get("task_name") or "").strip():
        name = str(entry.get("name") or entry.get("label") or "").strip()
        common_task_name = str(common_manifest.get("task_name") or "").strip()
        if name:
            merged["task_name"] = name
        elif common_task_name:
            merged["task_name"] = f"{common_task_name}_{index + 1}"
        else:
            merged["task_name"] = f"usergrowth_batch_{index + 1}"
    return merged


def _video_selectors_from_manifest(manifest: dict[str, Any], base_dir: Path) -> list[str]:
    selectors = _list_from_manifest(manifest, "videos")
    video_list = manifest.get("video_list")
    if video_list:
        selectors.extend(_read_video_list(_resolve_manifest_path(video_list, base_dir)))
    return selectors


def _resolve_manifest_path(value: Any, base_dir: Path) -> Path:
    path = Path(str(value))
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _live_confirmed(args: argparse.Namespace, manifest: dict[str, Any]) -> bool:
    return bool(args.confirm_live or manifest.get("confirm_live"))


def _clamp_concurrency(value: Any, batch_count: int) -> int:
    return _clamp_batch_concurrency(value, batch_count)


def _batch_label(index: int, config: UserGrowthRunConfig, manifest: dict[str, Any]) -> str:
    name = str(manifest.get("name") or manifest.get("label") or "").strip()
    order = config.order_id or f"批次{index + 1}"
    suffix = f":{name}" if name else ""
    return f"{index + 1}:{order}{suffix}"


def _create_batch_root(
    args: argparse.Namespace,
    manifest: dict[str, Any],
    specs: list[SelectedBatchSpec],
    base_dir: Path,
) -> Path:
    output_root = _manifest_output_root(args, manifest, base_dir) or specs[0].config.output_root
    task_name = str(_pick(args.task_name, manifest, "task_name") or "usergrowth_batches").strip()
    safe_task_name = _safe_name(task_name or "usergrowth_batches")
    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    batch_root = output_root / "batch_runs" / f"{batch_id}_{safe_task_name}"
    batch_root.mkdir(parents=True, exist_ok=True)
    return batch_root


def _manifest_output_root(args: argparse.Namespace, manifest: dict[str, Any], base_dir: Path) -> Path | None:
    value = _pick(args.output_root, manifest, "output_root")
    if value in (None, ""):
        batches = manifest.get("batches")
        if isinstance(batches, list):
            for entry in batches:
                if isinstance(entry, dict) and entry.get("output_root") not in (None, ""):
                    value = entry.get("output_root")
                    break
    if value in (None, ""):
        return None
    return _resolve_manifest_path(value, base_dir)


def _config_from_args(args: argparse.Namespace, manifest: dict[str, Any], base_dir: Path) -> UserGrowthRunConfig:
    video_folder = _required_path(_pick(args.video_folder, manifest, "video_folder"), base_dir, "video_folder")
    backfill_excel = _required_path(
        _pick(args.backfill_excel, manifest, "backfill_excel", "order_excel"),
        base_dir,
        "backfill_excel",
    )
    song_excel = _required_path(_pick(args.song_excel, manifest, "song_excel"), base_dir, "song_excel")
    output_root = _required_path(_pick(args.output_root, manifest, "output_root"), base_dir, "output_root")
    dry_run = not bool(args.live or manifest.get("live") or manifest.get("dry_run") is False)
    recursive = args.recursive if args.recursive is not None else bool(manifest.get("recursive", True))
    return UserGrowthRunConfig(
        video_folder=video_folder,
        order_excel=backfill_excel,
        song_excel=song_excel,
        output_root=output_root,
        account=_pick(args.account, manifest, "account") or os.environ.get("USERGROWTH_ACCOUNT", ""),
        password=_pick(args.password, manifest, "password") or os.environ.get("USERGROWTH_PASSWORD", ""),
        order_id=str(_pick(args.order_id, manifest, "order_id") or "").strip(),
        task_name=str(_pick(args.task_name, manifest, "task_name") or "usergrowth_upload").strip() or "usergrowth_upload",
        batch_name=str(_pick(None, manifest, "batch_name", "name", "label") or "").strip(),
        custom_tag_template_name=str(
            _pick(args.custom_tag_template_name, manifest, "custom_tag_template_name", "tag_template_name")
            or DEFAULT_CUSTOM_TAG_TEMPLATE_NAME
        ).strip() or DEFAULT_CUSTOM_TAG_TEMPLATE_NAME,
        custom_tag_template_tags=_custom_tag_template_tags_from_args(args, manifest),
        month_tag=str(_pick(args.month_tag, manifest, "month_tag") or "").strip(),
        recursive=recursive,
        dry_run=dry_run,
        headless=bool(args.headless or manifest.get("headless", False)),
        max_status_retries=int(_pick(args.max_status_retries, manifest, "max_status_retries") or 3),
        refresh_interval_seconds=float(_pick(args.refresh_interval_seconds, manifest, "refresh_interval_seconds") or 12.0),
        browser_slow_mo_ms=int(_pick(args.browser_slow_mo_ms, manifest, "browser_slow_mo_ms") or 600),
    )


def _pick(value: Any, manifest: dict[str, Any], *keys: str) -> Any:
    if value not in (None, ""):
        return value
    for key in keys:
        if manifest.get(key) not in (None, ""):
            return manifest[key]
    return None


def _custom_tag_template_tags_from_args(args: argparse.Namespace, manifest: dict[str, Any]) -> list[str]:
    cli_tags = _list_value(args.custom_tag)
    if cli_tags:
        return normalise_template_tags(cli_tags)
    if "custom_tag_template_fixed_tags" in manifest or "custom_tag_template_optional_tags" in manifest:
        return combine_template_tags(
            manifest.get("custom_tag_template_fixed_tags", []),
            manifest.get("custom_tag_template_optional_tags", []),
        )
    for key in ("custom_tag_template_tags", "custom_tags", "tag_template_tags"):
        if key in manifest:
            return normalise_template_tags(manifest.get(key))
    template_payload = manifest.get("custom_tag_template")
    if isinstance(template_payload, dict):
        payload = normalise_template_payload(template_payload)
        return combine_template_tags(payload["fixed_tags"], payload["optional_tags"])
    return default_custom_tag_template_tags()


def _required_path(value: Any, base_dir: Path, name: str) -> Path:
    if value in (None, ""):
        raise RuntimeError(f"缺少 {name}。")
    path = Path(str(value))
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _video_selectors_from_args(args: argparse.Namespace, manifest: dict[str, Any]) -> list[str]:
    selectors = [*_list_value(args.video), *_list_from_manifest(manifest, "videos")]
    video_list = _pick(args.video_list, manifest, "video_list")
    if video_list:
        list_path = Path(str(video_list))
        if not list_path.is_absolute() and args.manifest:
            list_path = Path(args.manifest).resolve().parent / list_path
        selectors.extend(_read_video_list(list_path))
    return selectors


def _read_video_list(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def _list_value(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value if str(item).strip()]


def _list_from_manifest(manifest: dict[str, Any], key: str) -> list[str]:
    return _list_value(manifest.get(key))


def resolve_video_selection(
    video_folder: Path,
    *,
    selectors: Iterable[str],
    glob_patterns: Iterable[str],
    recursive: bool,
    all_videos: bool,
) -> list[Path]:
    if not video_folder.is_dir():
        raise RuntimeError(f"视频文件夹不存在：{video_folder}")
    scanned = scan_video_files(video_folder, recursive=recursive)
    if all_videos:
        return scanned

    selected: list[Path] = []
    missing: list[str] = []
    scanned_by_name = defaultdict(list)
    scanned_by_stem = defaultdict(list)
    scanned_by_rel = {}
    for path in scanned:
        rel = _rel_key(path, video_folder)
        scanned_by_rel[rel] = path
        scanned_by_name[path.name.lower()].append(path)
        scanned_by_stem[path.stem.lower()].append(path)

    for selector in selectors:
        matches = _match_selector(selector, video_folder, scanned_by_rel, scanned_by_name, scanned_by_stem)
        if matches:
            selected.extend(matches)
        else:
            missing.append(selector)

    for pattern in glob_patterns:
        selected.extend(_match_glob(pattern, video_folder, scanned))

    deduped = _dedupe_paths(selected)
    if missing:
        raise RuntimeError("以下视频没有匹配到：" + "；".join(missing))
    return deduped


def _match_selector(
    selector: str,
    video_folder: Path,
    scanned_by_rel: dict[str, Path],
    scanned_by_name: dict[str, list[Path]],
    scanned_by_stem: dict[str, list[Path]],
) -> list[Path]:
    value = str(selector or "").strip().strip('"')
    if not value:
        return []
    if any(char in value for char in "*?[]"):
        return _match_glob(value, video_folder, scanned_by_rel.values())
    path = Path(value)
    if not path.is_absolute():
        path = video_folder / path
    if path.is_file():
        if path.suffix.lower() not in VIDEO_SUFFIXES:
            return []
        return [path.resolve()]
    rel_key = value.replace("\\", "/").lower()
    if rel_key in scanned_by_rel:
        return [scanned_by_rel[rel_key]]
    return [*scanned_by_name.get(Path(value).name.lower(), []), *scanned_by_stem.get(Path(value).stem.lower(), [])]


def _match_glob(pattern: str, video_folder: Path, scanned: Iterable[Path]) -> list[Path]:
    wanted = str(pattern or "").replace("\\", "/").lower()
    matches = []
    for path in scanned:
        rel = _rel_key(path, video_folder)
        name = path.name.lower()
        if fnmatch.fnmatch(rel, wanted) or fnmatch.fnmatch(name, wanted):
            matches.append(path)
    return matches


def _rel_key(path: Path, video_folder: Path) -> str:
    try:
        return path.resolve().relative_to(video_folder.resolve()).as_posix().lower()
    except ValueError:
        return path.name.lower()


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve()).lower()
        if key not in seen:
            seen.add(key)
            deduped.append(path.resolve())
    return deduped


def run_selected_usergrowth_task(
    config: UserGrowthRunConfig,
    video_paths: list[Path],
    progress: ProgressCallback | None = None,
) -> dict:
    task_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_task_name = _safe_name(config.task_name or "usergrowth_upload")
    task_root = config.output_root / f"{task_id}_{safe_task_name}"
    debug_dir = task_root / "debug"
    duplicate_song_excel = task_root / "duplicate_songs.xlsx"
    task_root.mkdir(parents=True, exist_ok=True)

    try:
        _emit(progress, f"已选中 {len(video_paths)} 个视频，开始读取歌曲库和回填模板")
        plans, items = build_selected_usergrowth_plan(
            config,
            video_paths,
            duplicate_song_output_path=duplicate_song_excel,
        )
        if not items:
            raise RuntimeError("没有可处理的视频")

        ready_count = sum(1 for item in items if item.status != "skipped")
        skipped_count = sum(1 for item in items if item.status == "skipped")
        _emit(progress, f"预检完成：待上传 {ready_count} 个，跳过 {skipped_count} 个")
        _emit_song_match_logs(progress, items)

        if config.dry_run:
            for item in items:
                if item.status == "pending":
                    item.status = "ready"
                    item.message = "预检通过，未执行上传"
            result_excel = task_root / "result.xlsx"
            write_back_results(config.order_excel, result_excel, items, include_ready=True)
            _emit(progress, f"预检结果已写入：{result_excel}")
        else:
            active_plans = [plan for plan in plans if plan.status != "skipped"]

            def write_order_backfill(plan: UserGrowthOrderPlan) -> None:
                with _backfill_lock(config.order_excel):
                    write_back_results(config.order_excel, config.order_excel, plan.items, include_ready=False)
                _emit(progress, f"订单 {plan.order_id} 已写回回填 Excel")

            browser = UserGrowthBrowserClient(
                config.account,
                config.password,
                headless=config.headless,
                debug_dir=debug_dir,
                refresh_interval_seconds=config.refresh_interval_seconds,
                max_status_retries=config.max_status_retries,
                browser_slow_mo_ms=config.browser_slow_mo_ms,
                order_complete=write_order_backfill,
            )
            asyncio.run(browser.run(active_plans, progress))
            result_excel = config.order_excel
            _emit(progress, f"正式上传完成，CID 已写回：{result_excel}")

        payload = _build_payload(config, task_id, task_root, plans, items, result_excel, duplicate_song_excel)
        payload["selected_videos"] = [str(path) for path in video_paths]
        (task_root / "task.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_log(task_root, payload)
        return payload
    except BaseException as exc:
        _write_task_error(task_root, config, video_paths, exc)
        try:
            setattr(exc, "_usergrowth_task_root", str(task_root))
        except Exception:
            pass
        raise


def run_selected_usergrowth_batches(
    specs: list[SelectedBatchSpec],
    *,
    concurrency: int,
    progress: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    """并发执行多批已选择视频的 UserGrowth 任务。"""
    if not specs:
        return []
    worker_count = _clamp_concurrency(concurrency, len(specs))
    results: list[dict[str, Any] | None] = [None] * len(specs)

    def run_one(spec: SelectedBatchSpec) -> dict[str, Any]:
        def batch_progress(message: str) -> None:
            _emit(progress, f"[{spec.label}] {message}")

        batch_progress(f"开始执行，已选中 {len(spec.video_paths)} 个视频")
        try:
            payload = run_selected_usergrowth_task(spec.config, spec.video_paths, batch_progress)
            batch_progress("执行完成")
            return _batch_success_result(spec, payload)
        except Exception as exc:  # noqa: BLE001
            batch_progress(f"执行失败：{exc}")
            return _batch_failed_result(spec, exc)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(run_one, spec): spec.index
            for spec in specs
        }
        for future in as_completed(future_map):
            index = future_map[future]
            results[index] = future.result()

    return [result for result in results if result is not None]


def _batch_success_result(spec: SelectedBatchSpec, payload: dict[str, Any]) -> dict[str, Any]:
    task_root = str(payload.get("task_root") or "")
    return {
        "index": spec.index,
        "label": spec.label,
        "status": "success",
        "message": "完成",
        "order_id": spec.config.order_id,
        "video_folder": str(spec.config.video_folder),
        "selected_count": len(spec.video_paths),
        "selected_videos": [str(path) for path in spec.video_paths],
        "task_root": task_root,
        "task_json": str(Path(task_root) / "task.json") if task_root else "",
        "run_log": str(Path(task_root) / "run.log") if task_root else "",
        "summary": payload.get("summary", {}),
        "result_excel": payload.get("result_excel", ""),
        "duplicate_song_excel": payload.get("duplicate_song_excel", ""),
    }


def _batch_failed_result(spec: SelectedBatchSpec, exc: Exception) -> dict[str, Any]:
    task_root = str(getattr(exc, "_usergrowth_task_root", "") or "")
    return {
        "index": spec.index,
        "label": spec.label,
        "status": "failed",
        "message": str(exc),
        "error_type": type(exc).__name__,
        "order_id": spec.config.order_id,
        "video_folder": str(spec.config.video_folder),
        "selected_count": len(spec.video_paths),
        "selected_videos": [str(path) for path in spec.video_paths],
        "task_root": task_root,
        "error_json": str(Path(task_root) / "error.json") if task_root else "",
        "error_log": str(Path(task_root) / "error.log") if task_root else "",
        "config": _safe_config_dict(spec.config),
    }


def _build_batch_payload(
    batch_root: Path,
    specs: list[SelectedBatchSpec],
    results: list[dict[str, Any]],
    concurrency: int,
) -> dict[str, Any]:
    total_items = 0
    ready_items = 0
    success_items = 0
    skipped_items = 0
    failed_items = 0
    for result in results:
        summary = result.get("summary") or {}
        total_items += int(summary.get("total") or result.get("selected_count") or 0)
        ready_items += int(summary.get("ready") or 0)
        success_items += int(summary.get("success") or 0)
        skipped_items += int(summary.get("skipped") or 0)
        failed_items += int(summary.get("failed") or 0)
    return {
        "batch_id": batch_root.name,
        "batch_root": str(batch_root),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "concurrency": concurrency,
        "summary": {
            "total_batches": len(specs),
            "success": sum(1 for result in results if result.get("status") == "success"),
            "failed": sum(1 for result in results if result.get("status") == "failed"),
            "cancelled": sum(1 for result in results if result.get("status") == "cancelled"),
            "total_items": total_items,
            "ready_items": ready_items,
            "success_items": success_items,
            "skipped_items": skipped_items,
            "failed_items": failed_items,
        },
        "batches": results,
    }


def _write_batch_log(batch_root: Path, payload: dict[str, Any]) -> None:
    lines = [
        f"batch_id: {payload['batch_id']}",
        f"created_at: {payload['created_at']}",
        f"batch_source: {payload.get('batch_source', 'manifest')}",
        f"concurrency: {payload['concurrency']}",
        f"batch_root: {payload['batch_root']}",
        "",
        "[summary]",
    ]
    lines.extend(f"{key}: {value}" for key, value in payload["summary"].items())
    lines.append("")
    lines.append("[batches]")
    for batch in payload["batches"]:
        lines.append(
            f"{batch['index'] + 1}. {batch['label']} | status={batch['status']} | "
            f"order={batch.get('order_id', '')} | selected={batch.get('selected_count', 0)} | "
            f"{batch.get('message', '')}"
        )
        if batch.get("task_root"):
            lines.append(f"   task_root: {batch['task_root']}")
        if batch.get("result_excel"):
            lines.append(f"   result_excel: {batch['result_excel']}")
        if batch.get("error_log"):
            lines.append(f"   error_log: {batch['error_log']}")
    (batch_root / "run.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_selected_usergrowth_plan(
    config: UserGrowthRunConfig,
    video_paths: list[Path],
    *,
    duplicate_song_output_path: Path | None = None,
) -> tuple[list[UserGrowthOrderPlan], list[UserGrowthVideoItem]]:
    scanned_videos = [
        (path, detect_material_type(path.name))
        for path in _dedupe_paths(video_paths)
    ]
    batch_song_names = [
        extract_song_name(path.name, material_type)
        for path, material_type in scanned_videos
        if material_type not in {"金币VIP", "金币SVIP"}
    ]
    song_records = load_song_records(
        config.song_excel,
        duplicate_output_path=duplicate_song_output_path,
        duplicate_song_names=batch_song_names,
    )
    default_order_id = config.order_id.strip()
    if not default_order_id:
        raise ValueError("请填写订单ID。")

    items: list[UserGrowthVideoItem] = []
    for path, material_type in scanned_videos:
        song_name = extract_song_name(path.name, material_type)
        item = UserGrowthVideoItem(
            path=path,
            file_name=path.name,
            material_type=material_type,
            song_name=song_name,
            classification_path=classification_path_for_material(path.name),
            optional_tags=optional_tags_for_file(path.name),
        )
        _attach_song(item, song_records, config.month_tag, config.custom_tag_template_tags)
        _attach_order(item, default_order_id)
        items.append(item)

    grouped: dict[str, list[UserGrowthVideoItem]] = defaultdict(list)
    skipped_items: list[UserGrowthVideoItem] = []
    for item in items:
        if item.status == "skipped" or not item.order_id:
            skipped_items.append(item)
            continue
        grouped[item.order_id].append(item)

    plans = [UserGrowthOrderPlan(order_id=order_id, items=group_items) for order_id, group_items in grouped.items()]
    if skipped_items:
        plans.append(
            UserGrowthOrderPlan(
                order_id="未分配/跳过",
                items=skipped_items,
                status="skipped",
                message="这些素材不会进入上传流程",
            )
        )
    return plans, items


def _public_payload(payload: dict) -> dict:
    public = dict(payload)
    if "config" in public:
        config = dict(public.get("config") or {})
        config.pop("account", None)
        config.pop("password", None)
        public["config"] = config
    return public


def _write_task_error(
    task_root: Path,
    config: UserGrowthRunConfig,
    video_paths: list[Path],
    exc: BaseException,
) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    payload = {
        "status": "failed",
        "timestamp": timestamp,
        "error_type": type(exc).__name__,
        "error_message": str(exc),
        "task_root": str(task_root),
        "mode": "dry_run" if config.dry_run else "browser_upload",
        "selected_videos": [str(path) for path in video_paths],
        "config": _safe_config_dict(config),
        "traceback": trace,
    }
    try:
        task_root.mkdir(parents=True, exist_ok=True)
        (task_root / "error.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            f"timestamp: {timestamp}",
            f"status: failed",
            f"mode: {payload['mode']}",
            f"error_type: {type(exc).__name__}",
            f"error_message: {exc}",
            "",
            "[selected_videos]",
            *[str(path) for path in video_paths],
            "",
            "[traceback]",
            trace.rstrip(),
        ]
        (task_root / "error.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def _write_cli_error(output_root: Path | None, exc: BaseException) -> None:
    if not output_root or getattr(exc, "_usergrowth_task_root", None):
        return
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    error_dir = output_root / "_cli_errors"
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    payload = {
        "status": "failed",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "error_type": type(exc).__name__,
        "error_message": str(exc),
        "traceback": trace,
    }
    try:
        error_dir.mkdir(parents=True, exist_ok=True)
        (error_dir / f"{timestamp}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (error_dir / f"{timestamp}.log").write_text(trace, encoding="utf-8")
    except Exception:
        pass


def _safe_config_dict(config: UserGrowthRunConfig) -> dict[str, Any]:
    return {
        "video_folder": str(config.video_folder),
        "backfill_excel": str(config.order_excel),
        "song_excel": str(config.song_excel),
        "output_root": str(config.output_root),
        "order_id": config.order_id,
        "task_name": config.task_name,
        "batch_name": config.batch_name,
        "selected_videos": [str(path) for path in config.selected_video_paths],
        "custom_tag_template_name": config.custom_tag_template_name,
        "custom_tag_template_tags": list(config.custom_tag_template_tags),
        "month_tag": config.month_tag,
        "recursive": config.recursive,
        "dry_run": config.dry_run,
        "headless": config.headless,
        "refresh_interval_seconds": config.refresh_interval_seconds,
        "browser_slow_mo_ms": config.browser_slow_mo_ms,
    }


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
