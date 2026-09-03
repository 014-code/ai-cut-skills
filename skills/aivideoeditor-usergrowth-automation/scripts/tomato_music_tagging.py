from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import threading
import traceback

from usergrowth_automation.usergrowth_tomato_music import (
    MAX_SEARCH_CHUNK_SIZE,
    TomatoMusicChunkResult,
    TomatoMusicTagBatch,
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
    write_tomato_tag_status,
)
from usergrowth_automation.usergrowth_models import UserGrowthCancelled
from usergrowth_automation.feishu_oauth import (
    DEFAULT_FEISHU_OAUTH_REDIRECT_URI,
    DEFAULT_FEISHU_OAUTH_SCOPE,
    FeishuOAuthConfig,
    obtain_user_access_token,
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
    parser.add_argument(
        "--feishu-library-url",
        action="append",
        default=[],
        help="在线飞书歌名/bookid 库 URL；可重复传入多个审核人员单曲查询表。",
    )
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
        "--feishu-user-oauth",
        action="store_true",
        help=(
            "通过国内飞书 OAuth + PKCE 获取 user_access_token；默认仅在本次进程内存中使用，"
            "可配合 --feishu-oauth-persist 启用长期授权。"
        ),
    )
    parser.add_argument(
        "--feishu-oauth-bootstrap",
        action="store_true",
        help=(
            "首次运行时用 FEISHU_BOOTSTRAP_ACCOUNT/FEISHU_BOOTSTRAP_PASSWORD（缺失则安全提示输入）"
            "自动完成国内飞书登录和授权，并启用加密长期 Token 缓存；缓存命中后跳过账号密码和浏览器。"
        ),
    )
    parser.add_argument(
        "--feishu-oauth-redirect-uri",
        default=os.environ.get("FEISHU_OAUTH_REDIRECT_URI") or DEFAULT_FEISHU_OAUTH_REDIRECT_URI,
        help=f"OAuth 本地回调地址，默认 {DEFAULT_FEISHU_OAUTH_REDIRECT_URI}；需在飞书安全设置中预先登记。",
    )
    parser.add_argument(
        "--feishu-oauth-port",
        type=int,
        help="OAuth 回调端口的便捷覆盖项；传入后会替换 redirect URI 中的端口。",
    )
    parser.add_argument(
        "--feishu-oauth-scope",
        default=os.environ.get("FEISHU_OAUTH_SCOPE") or DEFAULT_FEISHU_OAUTH_SCOPE,
        help="OAuth scope，空格分隔；默认覆盖 Wiki 节点读取和电子表格读写。",
    )
    parser.add_argument(
        "--feishu-oauth-timeout-seconds",
        type=float,
        default=300.0,
        help="等待浏览器授权回调的最长秒数。",
    )
    parser.add_argument(
        "--feishu-oauth-no-browser",
        action="store_true",
        help="不自动唤起系统浏览器，只输出不含密钥的授权 URL，便于交给受控浏览器打开。",
    )
    parser.add_argument(
        "--feishu-oauth-url-file",
        help="可选：把短时授权 URL 写入此临时文件；授权完成或超时后自动删除。",
    )
    parser.add_argument(
        "--feishu-oauth-persist",
        action="store_true",
        help="用 Windows 当前用户 DPAPI 加密保存 OAuth Token，并在后续进程自动刷新。",
    )
    parser.add_argument(
        "--feishu-oauth-cache",
        help="可选：DPAPI 加密 Token 缓存路径；默认保存到当前用户 LOCALAPPDATA。",
    )
    parser.add_argument(
        "--feishu-oauth-reauthorize",
        action="store_true",
        help="忽略现有 DPAPI 缓存并重新进行一次浏览器授权。",
    )
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
    parser.add_argument(
        "--recover-status-from-task",
        help="从已通过精确成功门槛的番茄 task.json 补回飞书“已打标”，不重复提交墨攻任务。",
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
    parser.add_argument(
        "--storage-state",
        help="可选：复用受控执行器提供的 Playwright storage state JSON。",
    )
    parser.add_argument(
        "--storage-state-output",
        help="可选：把已验证的 Playwright storage state 写到受控临时路径。",
    )
    parser.add_argument("--account", help="优先使用 USERGROWTH_ACCOUNT 环境变量。")
    parser.add_argument("--password", help="优先使用 USERGROWTH_PASSWORD 环境变量。")
    parser.add_argument("--refresh-interval-seconds", type=float, default=12.0)
    parser.add_argument("--browser-slow-mo-ms", type=int, default=120)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="同时运行的独立 BID 浏览器批次数，范围 1-10；默认 1。",
    )
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
        wanted_bids = {normalise_bid(value) for value in args.bid if normalise_bid(value)}
        if args.feishu_source_url:
            if not args.feishu_library_url:
                raise RuntimeError("使用 --feishu-source-url 时必须至少传一个 --feishu-library-url。")
            if args.feishu_writeback and not args.confirm_feishu_writeback:
                raise RuntimeError("在线回填飞书需要同时传 --feishu-writeback --confirm-feishu-writeback。")
            if args.live and not (args.feishu_writeback and args.confirm_feishu_writeback):
                raise RuntimeError(
                    "在线飞书真实打标会在墨攻成功后把打标状态更新为已打标，"
                    "需要同时传 --feishu-writeback --confirm-feishu-writeback。"
                )
            if args.feishu_writeback and args.max_batches and not wanted_bids:
                raise RuntimeError(
                    "在线飞书写回使用 --max-batches 时必须同时通过 --bid 明确限定写入 BID，"
                    "避免写回未进入本次测试的其他批次。"
                )
            if args.recover_status_from_task:
                if args.live:
                    raise RuntimeError("补回飞书打标状态时不要同时传 --live。")
                if not (args.feishu_writeback and args.confirm_feishu_writeback):
                    raise RuntimeError(
                        "补回飞书打标状态需要同时传 --feishu-writeback --confirm-feishu-writeback。"
                    )
                if not wanted_bids:
                    raise RuntimeError("补回飞书打标状态必须通过 --bid 明确限定 BID。")
            app_id = str(args.feishu_app_id or os.environ.get("FEISHU_APP_ID") or "").strip()
            app_secret = str(args.feishu_app_secret or os.environ.get("FEISHU_APP_SECRET") or "").strip()
            access_token = str(
                args.feishu_access_token
                or os.environ.get("FEISHU_ACCESS_TOKEN")
                or os.environ.get("FEISHU_TENANT_ACCESS_TOKEN")
                or os.environ.get("FEISHU_USER_ACCESS_TOKEN")
                or ""
            ).strip()
            token_kind = ""
            use_feishu_user_oauth = bool(args.feishu_user_oauth or args.feishu_oauth_bootstrap)
            persist_feishu_oauth = bool(args.feishu_oauth_persist or args.feishu_oauth_bootstrap)
            if (args.feishu_oauth_persist or args.feishu_oauth_reauthorize) and not use_feishu_user_oauth:
                raise RuntimeError(
                    "--feishu-oauth-persist/--feishu-oauth-reauthorize 需要与 --feishu-user-oauth 一起使用。"
                )
            if use_feishu_user_oauth:
                if access_token:
                    raise RuntimeError("--feishu-user-oauth 不能与现有 FEISHU_ACCESS_TOKEN 同时使用。")
                oauth_redirect_uri = str(args.feishu_oauth_redirect_uri)
                if args.feishu_oauth_port:
                    from urllib.parse import urlsplit, urlunsplit

                    parsed_redirect = urlsplit(oauth_redirect_uri)
                    if parsed_redirect.scheme != "http" or not parsed_redirect.hostname:
                        raise RuntimeError("--feishu-oauth-port 只能用于带主机名的 HTTP 本地回调地址。")
                    host = parsed_redirect.hostname
                    if ":" in host and not host.startswith("["):
                        host = f"[{host}]"
                    netloc = f"{host}:{int(args.feishu_oauth_port)}"
                    oauth_redirect_uri = urlunsplit(
                        (
                            parsed_redirect.scheme,
                            netloc,
                            parsed_redirect.path,
                            parsed_redirect.query,
                            parsed_redirect.fragment,
                        )
                    )
                access_token = obtain_user_access_token(
                    FeishuOAuthConfig(
                        app_id=app_id,
                        app_secret=app_secret,
                        redirect_uri=oauth_redirect_uri,
                        scope=str(args.feishu_oauth_scope),
                        timeout_seconds=float(args.feishu_oauth_timeout_seconds),
                        open_browser=not bool(args.feishu_oauth_no_browser),
                        authorize_url_file=str(args.feishu_oauth_url_file or ""),
                        persist_token=persist_feishu_oauth,
                        token_cache_path=str(args.feishu_oauth_cache or ""),
                        force_reauthorize=bool(args.feishu_oauth_reauthorize),
                        bootstrap_credentials=bool(args.feishu_oauth_bootstrap),
                    )
                )
                token_kind = "user_access_token"
            feishu_client = FeishuSheetsClient(
                access_token=access_token,
                app_id=app_id,
                app_secret=app_secret,
                token_kind=token_kind,
                base_url=str(args.feishu_api_base_url or DEFAULT_FEISHU_BASE_URL),
                timeout_seconds=float(args.feishu_timeout_seconds),
            )
            feishu_result = sync_tomato_music_from_feishu(
                feishu_client,
                source_url=str(args.feishu_source_url),
                library_urls=args.feishu_library_url,
                source_sheet_filters=args.feishu_source_sheet,
                library_sheet_filters=args.feishu_library_sheet,
                writeback=bool(args.feishu_writeback),
                overwrite_existing_bid=bool(args.feishu_overwrite_existing_bid),
                verify_writeback=True,
                bid_filters=wanted_bids,
            )
            batches = feishu_result.batches
            feishu_metadata = feishu_result.metadata()
            feishu_metadata["auth"] = {
                "token_kind": feishu_client.token_kind,
                "token_persisted": persist_feishu_oauth,
                "storage": "windows_dpapi" if persist_feishu_oauth else "memory_only",
                "bootstrap": bool(args.feishu_oauth_bootstrap),
            }
        else:
            if input_path is None:
                raise RuntimeError("缺少 --input 或 --feishu-source-url。")
            if args.feishu_writeback:
                raise RuntimeError("--feishu-writeback 只能与 --feishu-source-url 一起使用。")
            if args.recover_status_from_task:
                raise RuntimeError("--recover-status-from-task 只能与 --feishu-source-url 一起使用。")
            batches = load_tomato_music_batches(input_path)
        if wanted_bids:
            batches = [batch for batch in batches if batch.bid in wanted_bids]
        if args.max_batches and args.max_batches > 0:
            batches = batches[:args.max_batches]
        if not batches:
            raise RuntimeError("筛选后没有可处理的番茄音乐 BID 批次。")
        chunk_size = max(1, min(int(args.chunk_size or MAX_SEARCH_CHUNK_SIZE), MAX_SEARCH_CHUNK_SIZE))
        concurrency = max(1, min(int(args.concurrency or 1), 10))

        if args.recover_status_from_task:
            recovery = _recover_success_status(
                Path(args.recover_status_from_task).resolve(),
                feishu_client,
                feishu_result,
                wanted_bids=wanted_bids,
            )
            payload = {
                "workflow": "tomato_music_bid_tagging_status_recovery",
                "dry_run": False,
                "input": input_label,
                "source_task": str(Path(args.recover_status_from_task).resolve()),
                "feishu": feishu_metadata,
                "recovery": recovery,
                "summary": {
                    "verified_tasks": len(recovery["task_ids"]),
                    "verified_cids": len(recovery["cids"]),
                    "status_updated_rows": recovery["status_updated_rows"],
                },
            }
            _write_json(task_root / "task.json", payload)
            with (task_root / "run.log").open("w", encoding="utf-8") as fp:
                fp.write(
                    "番茄音乐飞书打标状态恢复完成\n"
                    f"已验证任务：{','.join(recovery['task_ids'])}\n"
                    f"CID：{len(recovery['cids'])}\n"
                    f"已更新行：{recovery['status_updated_rows']}\n"
                )
            print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
            return 0

        if not args.live:
            payload = build_dry_run_payload(
                batches,
                input_path=input_label,
                customer_id=str(args.customer_id or "").strip(),
                chunk_size=chunk_size,
            )
            if feishu_metadata:
                payload["feishu"] = feishu_metadata
            payload["concurrency"] = concurrency
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
            "concurrency": concurrency,
            "batches": [
                {
                    "bid": batch.bid,
                    "tag": batch.tag,
                    "cid_count": len(batch.cids),
                    "song_names": batch.song_names,
                    "tracks": batch.tracks,
                }
                for batch in batches
            ],
            "results": [],
        }
        if feishu_metadata:
            checkpoint_payload["feishu"] = feishu_metadata

        def progress(message: str) -> None:
            line = f"[{datetime.now().isoformat(timespec='seconds')}] {message}"
            try:
                print(message, flush=True)
            except OSError:
                # Detached Windows runs can expose an invalid console handle;
                # workflow progress is still persisted through run.log/checkpoints.
                pass
            with (task_root / "run.log").open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")

        def checkpoint(update: dict) -> None:
            checkpoint_payload.update(update)
            checkpoint_payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
            _write_json(task_root / "tomato_music_tagging_checkpoint.json", checkpoint_payload)

        results = asyncio.run(
            _run_live_batches(
                batches,
                account=account,
                password=password,
                customer_id=str(args.customer_id or "").strip(),
                material_url=str(args.material_url or "").strip(),
                chunk_size=chunk_size,
                concurrency=concurrency,
                headless=bool(args.headless),
                storage_state_path=(
                    Path(args.storage_state).resolve() if args.storage_state else None
                ),
                storage_state_output_path=(
                    Path(args.storage_state_output).resolve()
                    if args.storage_state_output
                    else None
                ),
                debug_dir=debug_dir,
                refresh_interval_seconds=float(args.refresh_interval_seconds),
                browser_slow_mo_ms=int(args.browser_slow_mo_ms),
                progress=progress,
                checkpoint=checkpoint,
                on_chunk_success=(
                    lambda _batch, result: _write_success_status(
                        feishu_client,
                        feishu_result,
                        result,
                        progress,
                    )
                ) if args.feishu_source_url else None,
            )
        )
        result_rows = serialise_results(results)
        summary = {
            "batches": len(batches),
            "concurrency": concurrency,
            "chunks": len(result_rows),
            "success_chunks": sum(1 for item in result_rows if item["status"] == "success"),
            "skipped_chunks": sum(1 for item in result_rows if item["status"] == "skipped"),
            "failed_chunks": sum(1 for item in result_rows if item["status"] == "failed"),
            "matched_materials": sum(int(item["matched_count"]) for item in result_rows),
            "successful_materials": sum(int(item["success"]) for item in result_rows),
            "status_updated_rows": sum(int(item.get("status_updated_rows") or 0) for item in result_rows),
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


async def _run_live_batches(
        batches: list[TomatoMusicTagBatch],
        *,
        account: str,
        password: str,
        customer_id: str,
        material_url: str,
        chunk_size: int,
        concurrency: int,
        headless: bool,
        storage_state_path: Path | None,
        storage_state_output_path: Path | None,
        debug_dir: Path,
        refresh_interval_seconds: float,
        browser_slow_mo_ms: int,
        progress,
        checkpoint,
        on_chunk_success=None,
        playwright_instance=None,
) -> list[TomatoMusicChunkResult]:
    """按输入顺序收集结果，同时最多运行指定数量的独立 BID 浏览器。"""
    limit = max(1, min(int(concurrency or 1), 10))
    semaphore = asyncio.Semaphore(limit)
    cancel_event = threading.Event()
    partial_results: list[list[dict]] = [[] for _ in batches]

    def merged_result_rows() -> list[dict]:
        return [row for batch_rows in partial_results for row in batch_rows]

    async def run_batch(batch_index: int, batch: TomatoMusicTagBatch, shared_playwright):
        async with semaphore:
            if cancel_event.is_set():
                raise UserGrowthCancelled("并发番茄任务已取消，未启动后续 BID 批次")

            slot = batch_index + 1
            batch_progress = (
                lambda message: progress(f"[并发 {slot}/{len(batches)}][BID {batch.bid}] {message}")
            )

            def batch_checkpoint(update: dict) -> None:
                rows = update.get("results")
                if isinstance(rows, list):
                    partial_results[batch_index] = rows
                checkpoint({"results": merged_result_rows()})

            batch_debug_dir = debug_dir / f"{slot:03d}_bid_{_safe_name(batch.bid)}"
            client = TomatoMusicTaggingClient(
                account,
                password,
                headless=headless,
                storage_state_path=storage_state_path,
                storage_state_output_path=storage_state_output_path,
                debug_dir=batch_debug_dir,
                refresh_interval_seconds=refresh_interval_seconds,
                browser_slow_mo_ms=browser_slow_mo_ms,
                cancel_event=cancel_event,
            )
            batch_progress("开始独立浏览器批次")
            try:
                batch_results = await client.run_tagging(
                    [batch],
                    customer_id=customer_id,
                    material_url=material_url,
                    chunk_size=chunk_size,
                    progress=batch_progress,
                    checkpoint=batch_checkpoint,
                    on_chunk_success=on_chunk_success,
                    playwright_instance=shared_playwright,
                )
            except UserGrowthCancelled:
                cancel_event.set()
                raise
            except Exception as exc:  # noqa: BLE001
                failure = TomatoMusicChunkResult(
                    bid=batch.bid,
                    tag=batch.tag,
                    chunk_index=0,
                    requested_cids=list(batch.cids),
                    status="failed",
                    message=f"批次执行失败：{exc}",
                )
                batch_results = [failure]
                batch_progress(f"独立浏览器批次失败，继续其他 BID：{exc}")

            partial_results[batch_index] = serialise_results(batch_results)
            checkpoint({"results": merged_result_rows()})
            return batch_results

    async with AsyncExitStack() as playwright_stack:
        shared_playwright = playwright_instance
        if shared_playwright is None:
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise RuntimeError("需要先安装 playwright，并执行 playwright install chromium") from exc
            shared_playwright = await playwright_stack.enter_async_context(async_playwright())
        outcomes = await asyncio.gather(
            *(
                run_batch(index, batch, shared_playwright)
                for index, batch in enumerate(batches)
            ),
            return_exceptions=True,
        )
    cancelled = [item for item in outcomes if isinstance(item, UserGrowthCancelled)]
    if cancelled:
        raise cancelled[0]

    results: list[TomatoMusicChunkResult] = []
    for outcome in outcomes:
        if isinstance(outcome, Exception):
            raise outcome
        results.extend(outcome)
    return results


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


def _write_success_status(feishu_client, feishu_result, chunk_result, progress) -> int:
    """将已经通过墨攻任务验收的本组 CID 标记为已打标。"""
    updated = write_tomato_tag_status(
        feishu_client,
        feishu_result,
        chunk_result.requested_cids,
        status="已打标",
        verify=True,
    )
    if progress:
        progress(
            f"飞书打标状态回写：BID {chunk_result.bid} 第 {chunk_result.chunk_index} 组，"
            f"已打标 {updated}/{len(chunk_result.requested_cids)} 行"
        )
    return updated


def _recover_success_status(
        task_path: Path,
        feishu_client,
        feishu_result,
        *,
        wanted_bids: set[str],
) -> dict:
    """从旧任务的精确成功证据补回状态；任何计数不一致都停止。"""
    with task_path.open("r", encoding="utf-8") as fp:
        payload = json.load(fp)
    if str(payload.get("workflow") or "") != "tomato_music_bid_tagging":
        raise RuntimeError("恢复文件不是番茄音乐打标 task.json。")

    available_by_bid = {
        batch.bid: set(batch.cids)
        for batch in feishu_result.batches
        if batch.bid in wanted_bids
    }
    verified_cids: list[str] = []
    task_ids: list[str] = []
    for row in payload.get("results") or []:
        bid = normalise_bid(row.get("bid"))
        if bid not in wanted_bids:
            continue
        cids = [str(value).strip().lower() for value in row.get("requested_cids") or [] if str(value).strip()]
        expected = len(cids)
        task_id = str(row.get("task_id") or "").strip()
        exact_success = (
            expected > 0
            and task_id
            and str(row.get("status") or "") == "success"
            and int(row.get("matched_count") or 0) == expected
            and int(row.get("total") or 0) == expected
            and int(row.get("success") or 0) == expected
            and int(row.get("failed") or 0) == 0
        )
        if not exact_success:
            raise RuntimeError(f"旧任务 {task_id or '<无任务ID>'} 未通过精确成功门槛，禁止回写状态。")
        missing = set(cids) - available_by_bid.get(bid, set())
        if missing:
            raise RuntimeError(
                f"旧任务 {task_id} 的 CID 不在当前飞书待打标 BID {bid} 批次中：{','.join(sorted(missing))}"
            )
        task_ids.append(task_id)
        for cid in cids:
            if cid not in verified_cids:
                verified_cids.append(cid)

    if not verified_cids:
        raise RuntimeError("旧任务中没有找到目标 BID 的精确成功 CID。")
    updated = write_tomato_tag_status(
        feishu_client,
        feishu_result,
        verified_cids,
        status="已打标",
        verify=True,
    )
    return {
        "bids": sorted(wanted_bids),
        "task_ids": task_ids,
        "cids": verified_cids,
        "status_updated_rows": updated,
        "verified": True,
    }


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
