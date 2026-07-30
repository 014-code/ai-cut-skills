from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable

from .usergrowth_excel import load_song_records, match_song_record
from .usergrowth_models import UserGrowthOrderPlan, UserGrowthRunConfig, UserGrowthVideoItem, VIDEO_SUFFIXES
from .usergrowth_rules import (
    classification_path_for_material,
    custom_tags_for_material,
    detect_material_type,
    extract_song_name,
    normalize_text,
    optional_tags_for_file,
)

ProgressCallback = Callable[[str], None]


def scan_video_files(folder: Path, recursive: bool = True) -> list[Path]:
    """扫描视频文件夹，返回可处理的视频文件列表。"""
    if not folder.is_dir():
        return []
    iterator = folder.rglob("*") if recursive else folder.iterdir()
    return sorted(path for path in iterator if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES)


def load_song_records_for_split(
        config: UserGrowthRunConfig,
        *,
        progress: ProgressCallback | None = None,
) -> list:
    """自动拆批时优先用歌曲库识别标准歌名；歌曲库缺失时仍允许文件名兜底拆批。"""
    if not config.song_excel or not config.song_excel.is_file():
        return []
    try:
        return load_song_records(config.song_excel)
    except Exception as exc:  # noqa: BLE001
        if progress:
            progress(f"自动拆批读取歌曲库失败，改用文件名兜底：{exc}")
        return []


def build_song_batches_from_paths(
        config: UserGrowthRunConfig,
        video_paths: Iterable[Path],
        *,
        song_records=None,
) -> list[UserGrowthRunConfig]:
    """按歌曲名把视频路径拆成多个单曲批次。"""
    grouped: dict[str, tuple[str, list[Path]]] = {}
    for path_value in video_paths:
        path = Path(path_value)
        material_type = detect_material_type(path.name)
        song_name = song_name_for_split(path, material_type, song_records or [])
        group_key = song_group_key(song_name)
        if group_key not in grouped:
            grouped[group_key] = (song_name, [])
        label, paths = grouped[group_key]
        better_label = prefer_song_label(label, song_name)
        if better_label != label:
            grouped[group_key] = (better_label, paths)
        paths.append(path.resolve())

    return _configs_from_song_groups(config, grouped.values())


def build_song_batches_from_items(
        config: UserGrowthRunConfig,
        items: Iterable[UserGrowthVideoItem],
) -> list[UserGrowthRunConfig]:
    """把预检出的可上传素材按歌曲名拆成多个单曲批次。"""
    grouped: dict[str, tuple[str, list[UserGrowthVideoItem]]] = {}
    for item in items:
        if item.status == "skipped" or not item.order_id:
            continue
        song_name = (item.song_name or Path(item.file_name).stem).strip() or "未识别歌曲"
        group_key = song_group_key(song_name)
        if group_key not in grouped:
            grouped[group_key] = (song_name, [])
        label, group_items = grouped[group_key]
        better_label = prefer_song_label(label, song_name)
        if better_label != label:
            grouped[group_key] = (better_label, group_items)
        group_items.append(item)

    base_task_name = config.task_name or "usergrowth_upload"
    batches: list[UserGrowthRunConfig] = []
    for index, (song_name, group_items) in enumerate(grouped.values(), start=1):
        suffix = safe_usergrowth_batch_suffix(song_name)
        task_name = f"{base_task_name}_{index:02d}_{suffix}" if suffix else f"{base_task_name}_{index:02d}"
        batches.append(
            replace(
                config,
                batch_name=song_name,
                selected_video_paths=[item.path for item in group_items],
                task_name=task_name,
            )
        )
    return batches


def song_name_for_split(path: Path, material_type: str, song_records) -> str:
    fallback = extract_song_name(path.name, material_type).strip() or path.stem
    if song_records and material_type not in {"金币VIP", "金币SVIP"}:
        record, _ = match_song_record(path.name, song_records)
        if record:
            return record.song_name
    return fallback


def song_group_key(song_name: str) -> str:
    normalized = normalize_text(song_name)
    compact = "".join(char for char in normalized if char.isalnum())
    return compact or normalized or str(song_name or "").strip().lower()


def prefer_song_label(current: str, candidate: str) -> str:
    current_text = str(current or "").strip()
    candidate_text = str(candidate or "").strip()
    if not current_text:
        return candidate_text
    if not candidate_text:
        return current_text
    current_score = song_label_score(current_text)
    candidate_score = song_label_score(candidate_text)
    if candidate_score > current_score:
        return candidate_text
    return current_text


def song_label_score(value: str) -> tuple[int, int, int]:
    return (
        sum(1 for char in value if char in ".·（）()《》"),
        sum(1 for char in value if char.isalnum()),
        len(value),
    )


def safe_usergrowth_batch_suffix(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value.strip())
    return cleaned[:24].strip("_")


def usergrowth_batch_preview_text(
        batches: list[UserGrowthRunConfig],
        total_items: int,
        output_root: Path | str,
) -> str:
    lines = [f"已读取 {total_items} 个视频，按歌曲拆为 {len(batches)} 批；输出目录：{output_root}"]
    for index, config in enumerate(batches[:8], start=1):
        lines.append(f"{index}. {config.batch_name or '整文件夹'}：{len(config.selected_video_paths) or '全部'} 个视频")
    if len(batches) > 8:
        lines.append(f"... 还有 {len(batches) - 8} 批未展开显示。")
    return "\n".join(lines)


def _configs_from_song_groups(
        config: UserGrowthRunConfig,
        groups,
) -> list[UserGrowthRunConfig]:
    base_task_name = config.task_name or "usergrowth_upload"
    batches: list[UserGrowthRunConfig] = []
    for index, (song_name, paths) in enumerate(groups, start=1):
        suffix = safe_usergrowth_batch_suffix(song_name)
        task_name = f"{base_task_name}_{index:02d}_{suffix}" if suffix else f"{base_task_name}_{index:02d}"
        batches.append(
            replace(
                config,
                batch_name=song_name,
                selected_video_paths=paths,
                task_name=task_name,
            )
        )
    return batches


def build_usergrowth_plan(
        config: UserGrowthRunConfig,
        *,
        duplicate_song_output_path: Path | None = None,
) -> tuple[list[UserGrowthOrderPlan], list[UserGrowthVideoItem]]:
    """根据视频文件、歌曲库和订单 ID 生成上传计划。"""
    scanned_videos = [
        (path, detect_material_type(path.name))
        for path in _scan_config_video_files(config)
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

    plans = [UserGrowthOrderPlan(order_id=order_id, items=items) for order_id, items in grouped.items()]
    if skipped_items:
        plans.append(UserGrowthOrderPlan(order_id="未分配/跳过", items=skipped_items, status="skipped", message="这些素材不会进入上传流程"))
    return plans, items


def _attach_song(item: UserGrowthVideoItem, song_records, month_tag: str, template_tags: object = None) -> None:
    """为视频条目匹配歌曲 ID，并生成自定义标签和禁投状态。"""
    if item.material_type in {"金币VIP", "金币SVIP"}:
        item.song_match_message = f"{item.material_type} 不需要歌曲 ID"
        item.custom_tags = custom_tags_for_material(
            item.material_type,
            "",
            item.file_name,
            month_tag=month_tag,
            template_tags=template_tags,
        )
        return

    record, candidates = match_song_record(item.song_name, song_records)
    if not record:
        if candidates:
            candidate_text = "、".join(
                f"{candidate.song_name}({candidate.song_id})"
                for candidate in candidates[:5]
            )
            item.song_match_message = f"匹配到多个候选，未填写歌曲 ID：{candidate_text}"
            item.message = f"歌曲名匹配到多个候选，未填写歌曲 ID 自定义标签：{candidate_text}"
        else:
            item.song_match_message = "未匹配到歌曲 ID"
            item.message = "歌曲库中未匹配到歌曲 ID，未填写歌曲 ID 自定义标签"
        item.custom_tags = custom_tags_for_material(
            item.material_type,
            "",
            item.file_name,
            month_tag=month_tag,
            template_tags=template_tags,
        )
        return

    item.song_name = record.song_name
    item.song_id = record.song_id
    item.blocked = record.blocked
    item.song_match_message = f"匹配到歌曲 ID：{record.song_id}"
    item.custom_tags = custom_tags_for_material(
        item.material_type,
        item.song_id,
        item.file_name,
        month_tag=month_tag,
        template_tags=template_tags,
    )
    if record.blocked:
        item.status = "skipped"
        item.song_match_message = f"匹配到歌曲 ID：{record.song_id}，歌曲库标记禁投"
        item.message = "歌曲库标记禁投，已跳过"


def _attach_order(item: UserGrowthVideoItem, order_id: str) -> None:
    """把客户端输入的订单 ID 绑定到可上传素材上。"""
    if item.status == "skipped":
        return
    if not item.material_type:
        item.status = "skipped"
        item.message = "文件名未识别到素材类型"
        return

    if order_id:
        item.order_id = order_id
        return

    item.status = "skipped"
    item.message = "请填写订单ID"


def _scan_config_video_files(config: UserGrowthRunConfig) -> list[Path]:
    """按配置扫描视频；自动拆批时只扫描当前批次选中的文件。"""
    if not config.selected_video_paths:
        return scan_video_files(config.video_folder, recursive=config.recursive)

    selected: list[Path] = []
    seen: set[str] = set()
    for value in config.selected_video_paths:
        path = Path(value)
        if not path.is_absolute():
            path = config.video_folder / path
        if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        selected.append(path.resolve())
    return sorted(selected)
