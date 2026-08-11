from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import traceback

from usergrowth_automation.usergrowth_tomato_music import (
    MAX_SEARCH_CHUNK_SIZE,
    TomatoMusicTaggingClient,
    build_dry_run_payload,
    load_tomato_music_batches,
    normalise_bid,
    serialise_results,
)
from usergrowth_automation.usergrowth_feishu_sheets import (
    DEFAULT_FEISHU_BASE_URL,
    FeishuSheetsClient,
    sync_tomato_music_from_feishu,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="番茄音乐 CID -> bid_... 自定义标签自动化。默认只生成预检计划。",
    )
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--input", help="包含 BID/CID 的 JSON、XLSX 或 XLSM。")
    input_group.add_argument(
        "--feishu-source-url",
        help="在线飞书源表 URL；支持 wiki 或 sheets 地址，默认读取其中全部工作表。",
    )
    parser.add_argument("--feishu-library-url", help="在线飞书歌名/bookid 库 URL。")
    parser.add_argument(
        "--feishu-source-sheet",
        action="append",
        default=[],
        help="仅处理指定源工作表 ID 或标题；可重复，默认全部。",
    )
    parser.add_argument(
        "--feishu-library-sheet",
        action="append",
        default=[],
        help="仅处理指定 BID 库工作表 ID 或标题；可重复，默认全部。",
    )
    parser.add_argument("--feishu-access-token", help="优先使用 FEISHU_ACCESS_TOKEN 环境变量。")
    parser.add_argument("--feishu-app-id", help="优先使用 FEISHU_APP_ID 环境变量。")
    parser.add_argument("--feishu-app-secret", help="优先使用 FEISHU_APP_SECRET 环境变量。")
    parser.add_argument(
        "--feishu-api-base-url",
        default=os.environ.get("FEISHU_API_BASE_URL") or DEFAULT_FEISHU_BASE_URL,
        help="飞书 OpenAPI 根地址；通常无需修改。",
    )
    parser.add_argument("--feishu-timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--feishu-writeback",
        action="store_true",
        help="将匹配出的 BID 通过官方 Sheets API 回填源飞书表格。",
    )
    parser.add_argument(
        "--confirm-feishu-writeback",
        action="store_true",
        help="与 --feishu-writeback 同时传入才允许在线修改飞书表格。",
    )
    parser.add_argument(
        "--feishu-overwrite-existing-bid",
        action="store_true",
        help="允许用 BID 库结果覆盖源表中不一致的已有 BID；默认保留已有值并报告冲突。",
    )
    parser.add_argument("--output-root", help="task.json、run.log 和 debug 输出目录。")
    parser.add_argument("--task-name", default="tomato_music_bid_tagging")
    parser.add_argument("--customer-id", default="", help="客户列表中的客户 ID，例如 3681575。")
    parser.add_argument("--material-url", default="", help="可选：已进入目标客户后的素材管理 URL。")
    parser.add_argument("--bid", action="append", default=[], help="只处理指定 BID，可重复传入。")
    parser.add_argument("--max-batches", type=int, default=0, help="只处理前 N 个 BID 批次；0 表示不限制。")
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=MAX_SEARCH_CHUNK_SIZE,
        help=f"单次搜索 CID 数，平台实测上限 {MAX_SEARCH_CHUNK_SIZE}。",
    )
    parser.add_argument("--live", action="store_true", help="真实写入 UserGrowth 自定义标签。")
    parser.add_argument("--confirm-live", action="store_true", help="与 --live 同时传入才允许真实提交。")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--account", help="优先使用 USERGROWTH_ACCOUNT 环境变量。")
    parser.add_argument("--password", help="优先使用 USERGROWTH_PASSWORD 环境变量。")
    parser.add_argument("--refresh-interval-seconds", type=float, default=12.0)
    parser.add_argument("--browser-slow-mo-ms", type=int, default=120)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    args = parse_args(argv)
    input_path = Path(args.input).resolve() if args.input else None
    input_label = str(input_path) if input_path else str(args.feishu_source_url or "").strip()
    default_output_root = input_path.parent if input_path else Path.cwd()
    output_root = Path(args.output_root).resolve() if args.output_root else default_output_root / "番茄音乐打标输出"
    task_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    task_root = output_root / f"{task_id}_{_safe_name(args.task_name)}"
    debug_dir = task_root / "debug"
    task_root.mkdir(parents=True, exist_ok=True)

    try:
        feishu_metadata: dict = {}
        if args.feishu_source_url:
            if not args.feishu_library_url:
                raise RuntimeError("使用 --feishu-source-url 时必须同时传 --feishu-library-url。")
            if args.feishu_writeback and not args.confirm_feishu_writeback:
                raise RuntimeError("在线回填飞书需要同时传 --feishu-writeback --confirm-feishu-writeback。")
            access_token = str(
                args.feishu_access_token
                or os.environ.get("FEISHU_ACCESS_TOKEN")
                or os.environ.get("FEISHU_TENANT_ACCESS_TOKEN")
                or os.environ.get("FEISHU_USER_ACCESS_TOKEN")
                or ""
            ).strip()
            app_id = str(args.feishu_app_id or os.environ.get("FEISHU_APP_ID") or "").strip()
            app_secret = str(args.feishu_app_secret or os.environ.get("FEISHU_APP_SECRET") or "").strip()
            feishu_client = FeishuSheetsClient(
                access_token=access_token,
                app_id=app_id,
                app_secret=app_secret,
                base_url=str(args.feishu_api_base_url or DEFAULT_FEISHU_BASE_URL),
                timeout_seconds=float(args.feishu_timeout_seconds),
            )
            feishu_result = sync_tomato_music_from_feishu(
                feishu_client,
                source_url=str(args.feishu_source_url),
                library_url=str(args.feishu_library_url),
                source_sheet_filters=args.feishu_source_sheet,
                library_sheet_filters=args.feishu_library_sheet,
                writeback=bool(args.feishu_writeback),
                overwrite_existing_bid=bool(args.feishu_overwrite_existing_bid),
                verify_writeback=True,
            )
            batches = feishu_result.batches
            feishu_metadata = feishu_result.metadata()
        else:
            if input_path is None:
                raise RuntimeError("缺少 --input 或 --feishu-source-url。")
            if args.feishu_writeback:
                raise RuntimeError("--feishu-writeback 只能与 --feishu-source-url 一起使用。")
            batches = load_tomato_music_batches(input_path)
        wanted_bids = {normalise_bid(value) for value in args.bid if normalise_bid(value)}
        if wanted_bids:
            batches = [batch for batch in batches if batch.bid in wanted_bids]
        if args.max_batches and args.max_batches > 0:
            batches = batches[:args.max_batches]
        if not batches:
            raise RuntimeError("筛选后没有可处理的番茄音乐 BID 批次。")
        chunk_size = max(1, min(int(args.chunk_size or MAX_SEARCH_CHUNK_SIZE), MAX_SEARCH_CHUNK_SIZE))

        if not args.live:
            payload = build_dry_run_payload(
                batches,
                input_path=input_label,
                customer_id=str(args.customer_id or "").strip(),
                chunk_size=chunk_size,
            )
            if feishu_metadata:
                payload["feishu"] = feishu_metadata
            _write_json(task_root / "task.json", payload)
            _write_log(task_root / "run.log", payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
            return 0

        if not args.confirm_live:
            raise RuntimeError("真实番茄音乐打标需要同时传 --live --confirm-live。")
        account = str(args.account or os.environ.get("USERGROWTH_ACCOUNT") or "").strip()
        password = str(args.password or os.environ.get("USERGROWTH_PASSWORD") or "").strip()
        if not account or not password:
            raise RuntimeError("真实番茄音乐打标需要账号密码；请使用环境变量或 --account/--password。")
        if not args.customer_id and not args.material_url:
            raise RuntimeError("真实番茄音乐打标需要 --customer-id 或 --material-url。")

        checkpoint_payload = {
            "workflow": "tomato_music_bid_tagging",
            "dry_run": False,
            "input": input_label,
            "customer_id": str(args.customer_id or "").strip(),
            "chunk_size": chunk_size,
            "batches": [
                {"bid": batch.bid, "tag": batch.tag, "cid_count": len(batch.cids), "song_names": batch.song_names}
                for batch in batches
            ],
            "results": [],
        }
        if feishu_metadata:
            checkpoint_payload["feishu"] = feishu_metadata

        def progress(message: str) -> None:
            line = f"[{datetime.now().isoformat(timespec='seconds')}] {message}"
            print(message, flush=True)
            with (task_root / "run.log").open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")

        def checkpoint(update: dict) -> None:
            checkpoint_payload.update(update)
            checkpoint_payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
            _write_json(task_root / "tomato_music_tagging_checkpoint.json", checkpoint_payload)

        client = TomatoMusicTaggingClient(
            account,
            password,
            headless=bool(args.headless),
            debug_dir=debug_dir,
            refresh_interval_seconds=float(args.refresh_interval_seconds),
            browser_slow_mo_ms=int(args.browser_slow_mo_ms),
        )
        results = asyncio.run(
            client.run_tagging(
                batches,
                customer_id=str(args.customer_id or "").strip(),
                material_url=str(args.material_url or "").strip(),
                chunk_size=chunk_size,
                progress=progress,
                checkpoint=checkpoint,
            )
        )
        result_rows = serialise_results(results)
        summary = {
            "batches": len(batches),
            "chunks": len(result_rows),
            "success_chunks": sum(1 for item in result_rows if item["status"] == "success"),
            "skipped_chunks": sum(1 for item in result_rows if item["status"] == "skipped"),
            "failed_chunks": sum(1 for item in result_rows if item["status"] == "failed"),
            "matched_materials": sum(int(item["matched_count"]) for item in result_rows),
            "successful_materials": sum(int(item["success"]) for item in result_rows),
        }
        payload = {**checkpoint_payload, "summary": summary, "results": result_rows}
        _write_json(task_root / "task.json", payload)
        checkpoint(payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
        return 1 if summary["failed_chunks"] else 0
    except Exception as exc:  # noqa: BLE001
        error = {
            "error_type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
        _write_json(task_root / "error.json", error)
        with (task_root / "error.log").open("w", encoding="utf-8") as fp:
            fp.write(error["traceback"])
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    temporary.replace(path)


def _write_log(path: Path, payload: dict) -> None:
    summary = payload.get("summary") or {}
    with path.open("w", encoding="utf-8") as fp:
        fp.write("番茄音乐 BID-CID 打标预检\n")
        fp.write(f"输入：{payload.get('input', '')}\n")
        fp.write(f"BID 批次：{summary.get('batches', 0)}\n")
        fp.write(f"CID：{summary.get('cids', 0)}\n")
        fp.write(f"搜索分组：{summary.get('chunks', 0)}\n")
        fp.write(f"单组上限：{payload.get('chunk_size', MAX_SEARCH_CHUNK_SIZE)}\n")


def _safe_name(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in str(value or ""))
    return cleaned.strip("_") or "tomato_music_bid_tagging"


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
