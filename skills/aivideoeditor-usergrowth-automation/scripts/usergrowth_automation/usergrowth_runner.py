from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .usergrowth_browser import UserGrowthBrowserClient
from .usergrowth_excel import write_back_results
from .usergrowth_models import (
    UserGrowthBatchResult,
    UserGrowthCancelled,
    UserGrowthOrderPlan,
    UserGrowthRunConfig,
    UserGrowthVideoItem,
)
from .usergrowth_planner import build_usergrowth_plan
from .usergrowth_redfruit import is_redfruit_workflow


ProgressCallback = Callable[[str], None]
_BACKFILL_LOCKS_GUARD = threading.Lock()
_BACKFILL_LOCKS: dict[str, threading.Lock] = {}
ARTIFACT_SCHEMA_VERSION = 1
WORKFLOW_CONTRACT_VERSION = "stable-v1"


def _clamp_batch_concurrency(value: Any, batch_count: int) -> int:
    """默认多批并行；显式传 1 时按队列串行，最高 10 个 worker。"""
    safe_batch_count = max(int(batch_count or 0), 1)
    if value is None or str(value).strip() == "":
        requested_workers = safe_batch_count
    else:
        try:
            requested_workers = int(value)
        except (TypeError, ValueError):
            requested_workers = safe_batch_count
    worker_count = max(1, min(requested_workers, 10, safe_batch_count))
    # 用户显式指定 1 表示串行。其他情况保留原有多批默认并发行为。
    if safe_batch_count > 1 and requested_workers != 1:
        worker_count = max(2, worker_count)
    return worker_count


def run_usergrowth_batches(
        configs: list[UserGrowthRunConfig],
        *,
        concurrency: int = 10,
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
) -> list[UserGrowthBatchResult]:
    """并发执行多批 UserGrowth 任务；每批独立浏览器，同一回填 Excel 串行写入。"""
    if not configs:
        return []
    worker_count = _clamp_batch_concurrency(concurrency, len(configs))
    results: list[UserGrowthBatchResult | None] = [None] * len(configs)

    def run_one(index: int, config: UserGrowthRunConfig) -> UserGrowthBatchResult:
        batch_label = _batch_label(index, config)

        def batch_progress(message: str) -> None:
            _emit(progress, f"[{batch_label}] {message}")

        if _is_cancelled(cancel_event):
            return UserGrowthBatchResult(
                index=index,
                order_id=config.order_id,
                video_folder=str(config.video_folder),
                status="cancelled",
                message="已取消",
            )
        batch_progress("开始执行")
        try:
            payload = run_usergrowth_task(config, batch_progress, cancel_event=cancel_event)
            summary = payload.get("summary", {})
            batch_progress("执行完成")
            return UserGrowthBatchResult(
                index=index,
                order_id=config.order_id,
                video_folder=str(config.video_folder),
                status="success",
                summary=summary,
                payload=payload,
                message="完成",
            )
        except UserGrowthCancelled as exc:
            batch_progress("执行已取消")
            return UserGrowthBatchResult(
                index=index,
                order_id=config.order_id,
                video_folder=str(config.video_folder),
                status="cancelled",
                message=str(exc) or "已取消",
            )
        except Exception as exc:  # noqa: BLE001
            batch_progress(f"执行失败：{exc}")
            return UserGrowthBatchResult(
                index=index,
                order_id=config.order_id,
                video_folder=str(config.video_folder),
                status="failed",
                message=str(exc),
            )

    if worker_count == 1:
        for index, config in enumerate(configs):
            result = run_one(index, config)
            results[index] = result
            if result.status == "failed" and index < len(configs) - 1:
                _emit(
                    progress,
                    f"[{_batch_label(index, config)}] 本批失败已记录，"
                    f"串行模式继续第 {index + 2} 批；不得让失败批次阻断后续队列",
                )
            if result.status == "cancelled":
                for remaining_index in range(index + 1, len(configs)):
                    remaining = configs[remaining_index]
                    results[remaining_index] = UserGrowthBatchResult(
                        index=remaining_index,
                        order_id=remaining.order_id,
                        video_folder=str(remaining.video_folder),
                        status="cancelled",
                        message="前一批已取消，串行队列停止",
                    )
                break
        return [result for result in results if result is not None]

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(run_one, index, config): index
            for index, config in enumerate(configs)
        }
        for future in as_completed(future_map):
            index = future_map[future]
            results[index] = future.result()

    return [result for result in results if result is not None]


def run_usergrowth_task(
        config: UserGrowthRunConfig,
        progress: ProgressCallback | None = None,
        *,
        cancel_event: threading.Event | None = None,
) -> dict:
    """执行一次 UserGrowth 任务：预检、浏览器上传、回填 Excel、写日志。"""
    _raise_if_cancelled(cancel_event)
    task_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_task_name = _safe_name(config.task_name or "usergrowth_upload")
    task_root = config.output_root / f"{task_id}_{safe_task_name}"
    debug_dir = task_root / "debug"
    duplicate_song_excel = task_root / "duplicate_songs.xlsx"
    task_root.mkdir(parents=True, exist_ok=True)
    supports_backfill = bool(config.order_excel) and not is_redfruit_workflow(config.workflow)

    if config.selected_video_paths:
        _emit(progress, f"正在读取已拆分批次：{len(config.selected_video_paths)} 个视频")
    else:
        _emit(progress, "正在扫描视频文件夹并读取 Excel")
    _raise_if_cancelled(cancel_event)
    plans, items = build_usergrowth_plan(config, duplicate_song_output_path=duplicate_song_excel)
    _raise_if_cancelled(cancel_event)
    if not items:
        raise RuntimeError("未扫描到可处理视频")

    ready_count = sum(1 for item in items if item.status != "skipped")
    skipped_count = sum(1 for item in items if item.status == "skipped")
    _emit(progress, f"预检完成：待上传 {ready_count} 个，跳过 {skipped_count} 个")
    _emit_song_match_logs(progress, items)

    if config.dry_run:
        _raise_if_cancelled(cancel_event)
        for item in items:
            if item.status == "pending":
                item.status = "ready"
                item.message = "预检通过，未执行上传"
        _emit(progress, "当前是预检模式，不会打开浏览器上传")
    else:
        active_plans = [plan for plan in plans if plan.status != "skipped"]
        def write_order_backfill(plan: UserGrowthOrderPlan) -> None:
            if not supports_backfill or not config.order_excel:
                return
            with _backfill_lock(config.order_excel):
                write_back_results(config.order_excel, config.order_excel, plan.items, include_ready=False)
            _emit(progress, f"订单 {plan.order_id} 已写回回填 Excel")

        browser = UserGrowthBrowserClient(
            config.account,
            config.password,
            headless=config.headless,
            storage_state_path=config.storage_state_path,
            storage_state_output_path=config.storage_state_output_path,
            debug_dir=debug_dir,
            refresh_interval_seconds=config.refresh_interval_seconds,
            max_status_retries=config.max_status_retries,
            browser_slow_mo_ms=config.browser_slow_mo_ms,
            order_complete=write_order_backfill if supports_backfill else None,
            cancel_event=cancel_event,
        )
        _raise_if_cancelled(cancel_event)
        asyncio.run(browser.run(active_plans, progress))

    overall_failed = any(plan.status == "failed" for plan in plans) or any(item.status == "failed" for item in items)
    failure_message = next((plan.message for plan in plans if plan.status == "failed" and plan.message), "")
    result_excel: Path | None = None
    if config.dry_run and supports_backfill and config.order_excel:
        result_excel = task_root / "result.xlsx"
        write_back_results(config.order_excel, result_excel, items, include_ready=True)
    elif (not config.dry_run) and supports_backfill and config.order_excel:
        result_excel = config.order_excel
    payload = _build_payload(config, task_id, task_root, plans, items, result_excel, duplicate_song_excel)
    (task_root / "task.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_log(task_root, payload)
    if config.dry_run:
        if result_excel:
            _emit(progress, f"任务完成，预检结果已保存：{result_excel}")
        else:
            _emit(progress, "任务完成，未生成回填 Excel")
    else:
        if result_excel:
            _emit(progress, f"任务完成，CID 已写回：{result_excel}")
        else:
            _emit(progress, "任务完成，红果短剧流程已结束")
    if overall_failed:
        raise RuntimeError(failure_message or "任务执行失败")
    return payload


def _is_cancelled(cancel_event: threading.Event | None) -> bool:
    return bool(cancel_event and cancel_event.is_set())


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if _is_cancelled(cancel_event):
        raise UserGrowthCancelled("任务已取消")


def _build_payload(
    config: UserGrowthRunConfig,
    task_id: str,
    task_root: Path,
    plans: list[UserGrowthOrderPlan],
    items: list[UserGrowthVideoItem],
    result_excel: Path | None,
    duplicate_song_excel: Path,
) -> dict:
    """组装 task.json 中保存的任务配置、统计和明细。"""
    return {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "workflow_contract_version": WORKFLOW_CONTRACT_VERSION,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "task_id": task_id,
        "task_root": str(task_root),
        "mode": "dry_run" if config.dry_run else "browser_upload",
        "config": {
            "workflow": config.workflow,
            "video_folder": str(config.video_folder),
            "batch_name": config.batch_name,
            "selected_videos": [str(path) for path in config.selected_video_paths],
            "backfill_excel": _path_text(config.order_excel),
            "song_excel": _path_text(config.song_excel),
            "output_root": str(config.output_root),
            "order_id": config.order_id,
            "task_name": config.task_name,
            "single_plan": config.single_plan,
            "delivery_products": list(config.delivery_products),
            "delivery_platforms": list(config.delivery_platforms),
            "delivery_platform_all": config.delivery_platform_all,
            "arlp_products": list(config.arlp_products),
            "arlp_platforms": list(config.arlp_platforms),
            "arlp_platform_all": config.arlp_platform_all,
            "redfruit_default_genre": config.redfruit_default_genre,
            "redfruit_bid_map": dict(config.redfruit_bid_map),
            "redfruit_layout_override": config.redfruit_layout_override,
            "redfruit_material_mode_override": config.redfruit_material_mode_override,
            "redfruit_ai_custom_tag": config.redfruit_ai_custom_tag,
            "redfruit_extra_custom_tags": list(config.redfruit_extra_custom_tags),
            "existing_creative_unit_title": config.existing_creative_unit_title,
            "existing_creative_unit_drama_type": config.existing_creative_unit_drama_type,
            "existing_creative_unit_bid": config.existing_creative_unit_bid,
            "custom_tag_template_name": config.custom_tag_template_name,
            "custom_tag_template_tags": list(config.custom_tag_template_tags),
            "month_tag": config.month_tag,
            "recursive": config.recursive,
            "dry_run": config.dry_run,
            "headless": config.headless,
            "refresh_interval_seconds": config.refresh_interval_seconds,
            "browser_slow_mo_ms": config.browser_slow_mo_ms,
        },
        "summary": {
            "total": len(items),
            "ready": sum(1 for item in items if item.status in {"ready", "pending"}),
            "success": sum(1 for item in items if item.status == "success"),
            "skipped": sum(1 for item in items if item.status == "skipped"),
            "failed": sum(1 for item in items if item.status == "failed"),
        },
        "result_excel": _path_text(result_excel),
        "duplicate_song_excel": str(duplicate_song_excel),
        "plans": [plan.to_dict() for plan in plans],
    }


def _path_text(path: Path | None) -> str:
    return str(path) if path else ""


def _write_log(task_root: Path, payload: dict) -> None:
    """把任务摘要和每个素材的执行结果写入 run.log。"""
    lines = [
        f"task_id: {payload['task_id']}",
        f"mode: {payload['mode']}",
        f"workflow: {payload['config'].get('workflow', '')}",
        f"video_folder: {payload['config']['video_folder']}",
        f"batch_name: {payload['config'].get('batch_name', '')}",
        f"backfill_excel: {payload['config']['backfill_excel']}",
        f"song_excel: {payload['config']['song_excel']}",
        f"order_id: {payload['config']['order_id']}",
        f"refresh_interval_seconds: {payload['config']['refresh_interval_seconds']}",
        f"browser_slow_mo_ms: {payload['config']['browser_slow_mo_ms']}",
        f"result_excel: {payload['result_excel']}",
        f"duplicate_song_excel: {payload.get('duplicate_song_excel', '')}",
        "",
        "[summary]",
    ]
    lines.extend(f"{key}: {value}" for key, value in payload["summary"].items())
    selected_videos = payload["config"].get("selected_videos") or []
    if selected_videos:
        lines.append("")
        lines.append("[selected_videos]")
        lines.extend(str(path) for path in selected_videos)
    lines.append("")
    lines.append("[song_matches]")
    for plan in payload["plans"]:
        for item in plan["items"]:
            lines.append(f"  {_song_match_log_line(item)}")
    lines.append("")
    lines.append("[items]")
    for plan in payload["plans"]:
        lines.append(f"order_id: {plan['order_id']} | status={plan['status']} | {plan.get('message', '')}")
        for item in plan["items"]:
            lines.append(
                f"  {item['status']} | {item['file_name']} | order={item['order_id']} | "
                f"type={item['material_type']} | song={item['song_name']} | id={item['song_id']} | "
                f"cid={item['cid']} | {item['message']}"
            )
            lines.append(f"    分类标签: {' / '.join(item.get('classification_path') or [])}")
            lines.append(f"    自定义标签: {'、'.join(item.get('custom_tags') or [])}")
            lines.append(f"    选填标签: {'、'.join(item.get('optional_tags') or [])}")
    (task_root / "run.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _emit_song_match_logs(progress: ProgressCallback | None, items: list[UserGrowthVideoItem]) -> None:
    """把每个素材是否匹配到歌曲 ID 打印到 UI/CLI 进度日志。"""
    for item in items:
        _emit(progress, _song_match_log_line(item))


def _song_match_log_line(item: UserGrowthVideoItem | dict[str, Any]) -> str:
    """格式化单个素材的歌曲 ID 匹配结果。"""
    file_name = str(_item_value(item, "file_name"))
    workflow = str(_item_value(item, "workflow"))
    material_type = str(_item_value(item, "material_type"))
    song_name = str(_item_value(item, "song_name"))
    song_id = str(_item_value(item, "song_id"))
    message = str(_item_value(item, "song_match_message") or _item_value(item, "message"))
    if is_redfruit_workflow(workflow):
        return f"红果短剧识别：{file_name} | 剧名={song_name or '-'} | BID={song_id or '-'} | 类型={material_type or '-'} | {message or '已识别'}"
    if material_type in {"金币VIP", "金币SVIP"}:
        return f"歌曲匹配跳过：{file_name} | 素材类型={material_type} | {message or '不需要歌曲 ID'}"
    if song_id:
        return f"歌曲匹配成功：{file_name} | 歌曲={song_name or '-'} | ID={song_id} | {message or '匹配成功'}"
    return f"歌曲匹配未命中：{file_name} | 识别歌曲={song_name or '-'} | {message or '未填写歌曲 ID'}"


def _item_value(item: UserGrowthVideoItem | dict[str, Any], key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key, "")
    return getattr(item, key, "")


def _safe_name(value: str) -> str:
    """把任务名转换为可用于文件夹名称的安全字符串。"""
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value.strip())
    return cleaned[:48].strip("_") or "usergrowth_upload"


def _batch_label(index: int, config: UserGrowthRunConfig) -> str:
    order = config.order_id or f"批次{index + 1}"
    suffix = f":{config.batch_name}" if config.batch_name else ""
    return f"{index + 1}:{order}{suffix}"


def _backfill_lock(path: Path) -> threading.Lock:
    key = str(path.resolve()).lower()
    with _BACKFILL_LOCKS_GUARD:
        lock = _BACKFILL_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _BACKFILL_LOCKS[key] = lock
        return lock


def _emit(progress: ProgressCallback | None, message: str) -> None:
    """向 UI 或调用方发送任务进度消息。"""
    if progress:
        progress(message)
