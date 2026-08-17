from __future__ import annotations

import asyncio
import ctypes
import json
import os
import re
import threading
import time
import traceback
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .usergrowth_captcha import UserGrowthCaptchaSolver
from .usergrowth_models import UserGrowthCancelled, UserGrowthOrderPlan, UserGrowthVideoItem
from .usergrowth_redfruit import (
    REDFRUIT_PLAYLET_URL,
    redfruit_content_kind,
    redfruit_expected_order_kind,
    redfruit_extract_order_title,
    redfruit_extract_playlet_card,
    redfruit_format_preflight_failure,
    redfruit_order_kind,
    is_redfruit_workflow,
)
from .usergrowth_rules import display_material_from_label, classification_path_for_material
from .usergrowth_session_cache import (
    clear_session_cache,
    default_session_cache_path,
    load_session_cache,
    save_session_cache,
)

ProgressCallback = Callable[[str], None]
OrderCompleteCallback = Callable[[UserGrowthOrderPlan], None]
CheckpointCallback = Callable[[UserGrowthOrderPlan], None]


def _compact_text(value: str) -> str:
    """去掉空白并归一化常见全角标点，便于比较中文 UI 文案。"""
    compact = re.sub(r"[\s\u00a0]+", "", value or "")
    return compact.translate(str.maketrans({
        "（": "(",
        "）": ")",
        "［": "[",
        "］": "]",
        "【": "[",
        "】": "]",
        "，": ",",
        "：": ":",
    }))


def _compact_cascader_text(value: str) -> str:
    return re.sub(r"[\s\u00a0_/\-－—–、，,（）()\[\]【】《》]+", "", value or "")


def _ui_text_variants(value: str) -> tuple[str, ...]:
    """生成常见全角/半角标点文案变体，供 Playwright 文本定位使用。"""
    base = str(value or "").strip()
    if not base:
        return ()
    variants = [base]
    for candidate in (
            base.translate(str.maketrans({"(": "（", ")": "）", ",": "，", ":": "："})),
            base.translate(str.maketrans({"（": "(", "）": ")", "，": ",", "：": ":"})),
    ):
        if candidate and candidate not in variants:
            variants.append(candidate)
    return tuple(variants)


LOGIN_URL = "https://usergrowth.com.cn/open/login"
HOME_URL = "https://usergrowth.com.cn/home"
WORK_ORDER_URL = "https://usergrowth.com.cn/aigc/manage/order"

# 操作速度
USERGROWTH_OPERATION_SPEED_FACTOR = 1.0
OPERATION_TASK_RETRY_LIMIT = 3
UPLOAD_ROW_RETRY_LIMIT = 3

# 投放信息弹窗选择器：定位 Arco Modal / Dialog / Drawer 等弹窗容器
DELIVERY_MODAL_SELECTOR = ".arco-modal, .arco-modal-content, [role='dialog'], .arco-drawer"
# 表单项选择器：定位包含字段标签和控件的表单行item
FORM_ITEM_SELECTOR = ".arco-form-item, [class*='form-item']"
# 下拉选择控件选择器：定位可点击打开下拉菜单的 Select/Cascader/InputTag 输入区域
SELECT_CONTROL_SELECTOR = (
    ".arco-select-view, .arco-cascader-view, .arco-input-tag, .arco-input-tag-view, "
    "[class*='select-view'], [class*='cascader-view'], [class*='input-tag']"
)
# 下拉菜单容器选择器：定位点击 Select 后展开的选项列表
DROPDOWN_ROOT_SELECTOR = ".arco-trigger-popup, .arco-select-dropdown, .arco-cascader-popup, [role='listbox']"
# 下拉选项元素
DROPDOWN_OPTION_SELECTOR = ".arco-select-option, .arco-cascader-option, [role='option']"


class UserGrowthFatalPageError(RuntimeError):
    """页面明确给出不可重试原因时直接中止当前重试。"""


class UserGrowthOperationTaskFailed(RuntimeError):
    """操作任务明确失败且已达到行级重试边界，禁止 CID 兜底绕过。"""


class UserGrowthUploadRowFailed(RuntimeError):
    """单个上传素材明确失败且已耗尽对应行的重试次数。"""


class UserGrowthUploadPageRetry(RuntimeError):
    """上传弹窗仍可恢复，应留在当前阶段重新选择文件。"""


class UserGrowthBrowserClient:
    """封装 UserGrowth 平台从登录到上传、录入变色龙、送审、回填 CID 的浏览器流程。"""

    def __init__(
            self,
            account: str,
            password: str,
            *,
            headless: bool = False,
            storage_state: dict[str, Any] | None = None,
            storage_state_path: str | Path | None = None,
            storage_state_output_path: str | Path | None = None,
            reuse_saved_session: bool = True,
            session_cache_path: str | Path | None = None,
            debug_dir: Path | None = None,
            timeout_ms: int = 180000,
            refresh_interval_seconds: float = 12.0,
            max_status_retries: int = 3,
            browser_slow_mo_ms: int = 120,
            order_complete: OrderCompleteCallback | None = None,
            checkpoint_callback: CheckpointCallback | None = None,
            cancel_event: threading.Event | None = None,
    ) -> None:
        """保存浏览器自动化运行参数。"""
        self.account = account
        self.password = password
        self.headless = headless
        self.storage_state = dict(storage_state) if isinstance(storage_state, dict) else None
        self.storage_state_path = Path(storage_state_path) if storage_state_path else None
        self.storage_state_output_path = Path(storage_state_output_path) if storage_state_output_path else None
        has_explicit_session_bridge = bool(
            self.storage_state is not None
            or self.storage_state_path
            or self.storage_state_output_path
        )
        if reuse_saved_session and not has_explicit_session_bridge:
            self.session_cache_path = (
                Path(session_cache_path)
                if session_cache_path
                else default_session_cache_path(account)
            )
        else:
            self.session_cache_path = None
        self._storage_state_source = "provided" if self.storage_state is not None else ""
        self._session_cache_saved = False
        self.session_authenticated = False
        self.login_performed = False
        self.debug_dir = debug_dir
        self.timeout_ms = timeout_ms
        self.refresh_interval_seconds = refresh_interval_seconds
        self.max_status_retries = max_status_retries
        self.browser_slow_mo_ms = browser_slow_mo_ms
        self.order_complete = order_complete
        self.checkpoint_callback = checkpoint_callback
        self.cancel_event = cancel_event
        self.operation_speed_factor = max(float(USERGROWTH_OPERATION_SPEED_FACTOR or 1.0), 0.1)
        self._captcha_solver: UserGrowthCaptchaSolver | None = None
        self._browser_process_id: int | None = None
        self._browser_process_handle: int | None = None
        self._tracked_browser = None
        self._browser_disconnected = False
        self._browser_disconnected_at = 0.0
        self._user_closed_headed_page = False
        self._user_closed_headed_page_at = 0.0
        self._crashed_page_ids: set[int] = set()
        self._intentional_page_close_ids: set[int] = set()
        # 红果后置阶段只能在本次正式 run() 正在处理的计划中执行，避免临时
        # 脚本绕过上传/送审/ARLP 检查点直接调用内部方法修改素材。
        self._active_redfruit_state_machine_plan_ids: set[int] = set()

    def _bind_redfruit_state_machine(self, plan: UserGrowthOrderPlan) -> None:
        """把当前正式 runner 正在处理的红果计划绑定到状态机。"""
        self._active_redfruit_state_machine_plan_ids.add(id(plan))

    def _release_redfruit_state_machine(self, plan: UserGrowthOrderPlan) -> None:
        """订单处理结束后撤销红果状态机的临时执行授权。"""
        self._active_redfruit_state_machine_plan_ids.discard(id(plan))

    def _assert_redfruit_state_machine(self, plan: UserGrowthOrderPlan) -> None:
        """拒绝脱离正式 runner 的红果 ARLP、分类和断点恢复调用。"""
        if id(plan) in self._active_redfruit_state_machine_plan_ids:
            return
        raise RuntimeError(
            "红果短剧状态机不可绕过：请仅通过 scripts/usergrowth_upload.py 的正式上传"
            "或 --resume-task 继续原任务；禁止直接调用红果 ARLP、分类标签或断点恢复内部方法。"
        )

    async def run(self, plans: list[UserGrowthOrderPlan], progress: ProgressCallback | None = None) -> None:
        """启动浏览器并按订单计划逐单处理上传流程。"""
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("需要先安装 playwright，并执行 playwright install chromium") from exc

        start_time = time.perf_counter()
        start_ts = datetime.now().isoformat(timespec="seconds")
        self._write_run_log(
            f"[{start_ts}] run start, plans={len(plans)}"
        )
        self._write_event(
            "run_start",
            plan_count=len(plans),
            order_ids=[plan.order_id for plan in plans],
        )

        self._raise_if_cancelled()
        async with async_playwright() as playwright:
            browser = await self._launch_browser(playwright)
            # 用可变容器保存当前浏览器。网络抖动导致目标进程断开时，恢复逻辑
            # 可能重建 browser/context；取消监听必须跟随新的 browser。
            session = {"browser": browser}
            cancel_task = asyncio.create_task(self._watch_cancel(session, progress))
            self._prepare_storage_state()
            context = await browser.new_context(**self._context_options())
            page = await context.new_page()
            self._wrap_page_speed(page)
            page.set_default_timeout(self.timeout_ms)
            page.set_default_navigation_timeout(self.timeout_ms)
            try:
                self._raise_if_cancelled()
                await self._snapshot(page, "00_browser_created")
                while True:
                    try:
                        await self._login(page, progress)
                        break
                    except Exception as exc:
                        loading_stalled = await self._page_is_blank_or_loading(page)
                        if not self._is_recoverable_session_exception(exc) and not loading_stalled:
                            raise
                        page = await self._wait_for_network_recovery(
                            page,
                            context,
                            progress,
                            "登录阶段",
                            playwright=playwright,
                            session=session,
                        )
                        context = page.context
                await self._persist_session_state(context, progress)
                await self._enable_post_login_resource_blocking(context, progress)
                await self._snapshot(page, "02_after_login")
                for plan in plans:
                    self._raise_if_cancelled()
                    if plan.status == "skipped":
                        continue
                    redfruit_state_machine_bound = self._is_redfruit_items(plan.items)
                    if redfruit_state_machine_bound:
                        self._bind_redfruit_state_machine(plan)
                    try:
                        while True:
                            try:
                                if getattr(plan, "_existing_only", False):
                                    active_items = [item for item in plan.items if item.status != "skipped"]
                                    page = await self._process_existing_creative_unit_items(
                                        page,
                                        plan,
                                        active_items,
                                        progress,
                                    )
                                    plan.status = "success"
                                    plan.message = "补录处理完成"
                                else:
                                    await self._process_order(page, plan, progress)
                                break
                            except Exception as exc:
                                loading_stalled = await self._page_is_blank_or_loading(page)
                                if not self._is_recoverable_session_exception(exc) and not loading_stalled:
                                    raise
                                page = await self._wait_for_network_recovery(
                                    page,
                                    context,
                                    progress,
                                    f"订单 {plan.order_id}",
                                    playwright=playwright,
                                    session=session,
                                )
                                context = page.context
                        if (
                                self.order_complete
                                and plan.status == "success"
                                and not getattr(plan, "_pre_review_cid_backfilled", False)
                        ):
                            self.order_complete(plan)
                    except UserGrowthCancelled:
                        raise
                    except Exception as exc:
                        # Resume-only failures can happen before _process_order's
                        # normal error boundary. Record this plan and keep the
                        # browser alive for the remaining user-requested plans.
                        if self._cancel_requested():
                            raise UserGrowthCancelled("任务已取消") from exc
                        plan.status = "failed"
                        plan.message = str(exc) or "订单执行失败"
                        for item in plan.items:
                            if item.status not in {"success", "skipped", "cancelled"}:
                                item.status = "failed"
                                item.message = plan.message
                        try:
                            await self._snapshot_error(
                                page,
                                f"order_{plan.order_id}_failed_continue",
                                exc=exc,
                                extra="该批已记录失败，继续后续用户指定批次",
                            )
                        except Exception:
                            pass
                        self._checkpoint(
                            plan,
                            plan.stage or "pending",
                            f"订单执行失败，继续后续批次：{plan.message}",
                        )
                        self._write_event(
                            "plan_failed_continue",
                            order_id=plan.order_id,
                            stage=plan.stage,
                            message=plan.message,
                        )
                        self._emit(
                            progress,
                            f"订单 {plan.order_id} 执行失败：{plan.message}；"
                            "本批已记录失败，继续后续用户指定批次",
                        )
                        continue
                    finally:
                        if redfruit_state_machine_bound:
                            self._release_redfruit_state_machine(plan)
                    if plan.status == "failed":
                        self._emit(
                            progress,
                            f"订单 {plan.order_id} 本批失败已记录，继续后续用户指定批次",
                        )
            except Exception as exc:
                if self._cancel_requested():
                    raise UserGrowthCancelled("任务已取消") from exc
                await self._raise_if_user_closed_browser(exc, progress)
                raise
            finally:
                cancel_task.cancel()
                try:
                    await cancel_task
                except asyncio.CancelledError:
                    pass
                elapsed = time.perf_counter() - start_time
                end_ts = datetime.now().isoformat(timespec="seconds")
                self._write_run_log(
                    f"[{end_ts}] run finished, elapsed={elapsed:.2f}s, "
                    f"plans={len(plans)}"
                )
                self._write_event(
                    "run_finished",
                    elapsed_seconds=round(elapsed, 2),
                    plans=[
                        {
                            "order_id": plan.order_id,
                            "stage": plan.stage,
                            "status": plan.status,
                        }
                        for plan in plans
                    ],
                )
                try:
                    await self._persist_session_state(context)
                except Exception as exc:
                    self._write_run_log(
                        f"[{datetime.now().isoformat(timespec='seconds')}] "
                        f"storage state export failed: {type(exc).__name__}: {exc}"
                    )
                try:
                    current_browser = session.get("browser", browser)
                    await self._close_browser_intentionally(current_browser)
                except Exception:
                    pass
                self._close_browser_process_probe()

    async def run_existing_creative_units(
            self,
            plan: UserGrowthOrderPlan,
            progress: ProgressCallback | None = None,
    ) -> None:
        """只处理已存在创意单元的补录，不重新创建或上传文件。"""
        setattr(plan, "_existing_only", True)
        await self.run([plan], progress)

    def _write_run_log(self, message: str) -> None:
        """把流程级别的计时日志追加到 run.log。

        debug_dir 不为空时写到 debug_dir/run.log，否则写到当前目录 run.log。
        """
        log_path = (
            (self.debug_dir / "run.log")
            if self.debug_dir
            else Path("run.log")
        )
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        try:
            with open(log_path, "a", encoding="utf-8") as fp:
                fp.write(message + "\n")
        except Exception as exc:
            print(f"[run.log] 写入失败: {exc}")

    def _write_event(self, event_type: str, **fields) -> None:
        """追加结构化诊断事件；写入失败不得影响浏览器业务流程。"""
        event_path = (
            (self.debug_dir / "events.jsonl")
            if self.debug_dir
            else Path("events.jsonl")
        )
        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "event": event_type,
            **fields,
        }
        try:
            event_path.parent.mkdir(parents=True, exist_ok=True)
            with open(event_path, "a", encoding="utf-8") as fp:
                fp.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    def _checkpoint(
            self,
            plan: UserGrowthOrderPlan,
            stage: str,
            message: str = "",
    ) -> None:
        """同步更新订单阶段；回调负责把汽水或红果任务原子写入断点文件。"""
        plan.stage = stage
        if message:
            plan.checkpoint_message = message
        self._write_run_log(
            f"[{datetime.now().isoformat(timespec='seconds')}] checkpoint: "
            f"order_id={plan.order_id}, stage={stage}, "
            f"upload_task_id={plan.upload_task_id or plan.task_id}, "
            f"review_task_id={plan.review_task_id}, arlp_task_id={plan.arlp_task_id}, "
            f"classification_task_id={plan.classification_task_id}, message={plan.checkpoint_message}"
        )
        self._write_event(
            "checkpoint",
            order_id=plan.order_id,
            stage=stage,
            status=plan.status,
            upload_task_id=plan.upload_task_id or plan.task_id,
            review_task_id=plan.review_task_id,
            arlp_task_id=plan.arlp_task_id,
            arlp_stage_index=plan.arlp_stage_index,
            arlp_stage_progress=[dict(value) for value in plan.arlp_stage_progress],
            classification_task_id=plan.classification_task_id,
            classification_progress=dict(plan.classification_progress),
            operation_retry_counts=dict(plan.operation_retry_counts),
            message=plan.checkpoint_message,
        )
        if self.checkpoint_callback:
            try:
                self.checkpoint_callback(plan)
            except Exception as exc:  # noqa: BLE001
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] checkpoint write failed: "
                    f"order_id={plan.order_id}, error={type(exc).__name__}: {exc}"
                )

    @staticmethod
    def _arlp_progress_entry(
            plan: UserGrowthOrderPlan,
            stage_index: int,
            stage_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """返回一个可原地更新的 ARLP 阶段断点条目。"""
        while len(plan.arlp_stage_progress) <= stage_index:
            plan.arlp_stage_progress.append({})
        entry = plan.arlp_stage_progress[stage_index]
        if not isinstance(entry, dict):
            entry = {}
            plan.arlp_stage_progress[stage_index] = entry
        entry.setdefault("stage_index", stage_index)
        if stage_config:
            entry.setdefault("name", str(stage_config.get("name") or f"阶段 {stage_index + 1}"))
            entry.setdefault("products", list(stage_config.get("products") or []))
            entry.setdefault("platforms", list(stage_config.get("platforms") or []))
        entry.setdefault("status", "pending")
        entry.setdefault("step", "pending")
        entry.setdefault("attempt", 0)
        entry.setdefault("task_id", "")
        entry.setdefault("total", 0)
        entry.setdefault("success", 0)
        entry.setdefault("failed", 0)
        entry.setdefault("last_error", "")
        return entry

    def _update_arlp_progress(
            self,
            plan: UserGrowthOrderPlan,
            stage_index: int,
            *,
            stage_config: dict[str, Any] | None = None,
            status: str | None = None,
            step: str | None = None,
            attempt: int | None = None,
            task_id: str | None = None,
            total: int | None = None,
            success: int | None = None,
            failed: int | None = None,
            last_error: str | None = None,
            checkpoint_stage: str | None = None,
            message: str = "",
    ) -> dict[str, Any]:
        entry = self._arlp_progress_entry(plan, stage_index, stage_config)
        for key, value in (
            ("status", status),
            ("step", step),
            ("attempt", attempt),
            ("task_id", task_id),
            ("total", total),
            ("success", success),
            ("failed", failed),
            ("last_error", last_error),
        ):
            if value is not None:
                entry[key] = value
        entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
        if checkpoint_stage:
            self._checkpoint(plan, checkpoint_stage, message)
        elif message:
            self._checkpoint(plan, plan.stage, message)
        return entry

    def _update_classification_progress(
            self,
            plan: UserGrowthOrderPlan,
            *,
            status: str | None = None,
            step: str | None = None,
            field_index: int | None = None,
            field_name: str | None = None,
            field_path: list[str] | None = None,
            attempt: int | None = None,
            save_status: str | None = None,
            last_error: str | None = None,
            checkpoint_stage: str | None = None,
            message: str = "",
    ) -> dict[str, Any]:
        progress = plan.classification_progress
        for key, value in (
            ("status", status),
            ("step", step),
            ("field_index", field_index),
            ("field_name", field_name),
            ("field_path", list(field_path) if field_path else None),
            ("attempt", attempt),
            ("save_status", save_status),
            ("last_error", last_error),
        ):
            if value is not None:
                progress[key] = value
        progress["updated_at"] = datetime.now().isoformat(timespec="seconds")
        if checkpoint_stage:
            self._checkpoint(plan, checkpoint_stage, message)
        elif message:
            self._checkpoint(plan, plan.stage, message)
        return progress

    async def _launch_browser(self, playwright):
        """用本机 Edge/Chrome 启动浏览器；首选 Edge，失败时回退到 Chrome。"""
        last_error: Exception | None = None
        for channel in ("msedge", "chrome"):
            try:
                browser = await playwright.chromium.launch(
                    channel=channel, headless=self.headless, slow_mo=self._scale_ms(self.browser_slow_mo_ms)
                )
                await self._track_browser_process(browser)
                return browser
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
        raise RuntimeError(f"启动浏览器失败：{last_error}")

    def _context_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {"viewport": {"width": 1440, "height": 1000}}
        if isinstance(self.storage_state, dict):
            options["storage_state"] = self.storage_state
        return options

    def _prepare_storage_state(self) -> None:
        """Load an explicit bridge state or the account-scoped encrypted cache."""
        if self.storage_state is not None:
            return
        if self.storage_state_path:
            self.storage_state = self._load_storage_state(self.storage_state_path)
            if self.storage_state is not None:
                self._storage_state_source = "explicit_file"
            return
        if not self.session_cache_path:
            return
        self.storage_state = load_session_cache(self.session_cache_path, self.account)
        if self.storage_state is not None:
            self._storage_state_source = "encrypted_cache"
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] encrypted login session loaded"
            )

    @staticmethod
    def _load_storage_state(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        if isinstance(payload, dict) and isinstance(payload.get("storage_state"), dict):
            payload = payload["storage_state"]
        return payload if isinstance(payload, dict) else None

    async def _capture_storage_state(self, context: Any) -> None:
        try:
            state = await context.storage_state()
        except Exception:
            return
        if isinstance(state, dict):
            self.storage_state = state

    async def _write_storage_state(self, context: Any) -> None:
        if not self.storage_state_output_path:
            return
        await self._capture_storage_state(context)
        payload = {
            "storage_state": self.storage_state if isinstance(self.storage_state, dict) else {},
            "authenticated": bool(self.session_authenticated),
            "login_performed": bool(self.login_performed),
        }
        output = self.storage_state_output_path
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f"{output.name}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, output)

    async def _persist_session_state(
            self,
            context: Any,
            progress: ProgressCallback | None = None,
    ) -> None:
        """Persist a validated session before later navigation can fail."""
        await self._capture_storage_state(context)
        if self.session_authenticated and self.session_cache_path and isinstance(self.storage_state, dict):
            try:
                save_session_cache(self.session_cache_path, self.account, self.storage_state)
                if self.login_performed and not self._session_cache_saved:
                    self._emit(progress, "已加密保存 UserGrowth 登录会话，后续任务将自动复用")
                self._session_cache_saved = True
            except Exception as exc:
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] encrypted session cache save failed: "
                    f"{type(exc).__name__}: {exc}"
                )
        await self._write_storage_state(context)

    async def _track_browser_process(self, browser) -> None:
        """记录 Chromium 主进程，区分用户正常关闭与浏览器异常退出。"""
        self._close_browser_process_probe()
        self._tracked_browser = browser
        self._browser_disconnected = False
        self._browser_disconnected_at = 0.0
        self._user_closed_headed_page = False
        self._user_closed_headed_page_at = 0.0
        self._crashed_page_ids.clear()
        self._intentional_page_close_ids.clear()
        browser.on("disconnected", lambda *_: self._mark_browser_disconnected(browser))
        if os.name != "nt":
            return

        cdp_session = None
        try:
            cdp_session = await browser.new_browser_cdp_session()
            process_payload = await cdp_session.send("SystemInfo.getProcessInfo")
            browser_row = next(
                row for row in process_payload.get("processInfo", [])
                if str(row.get("type") or "").lower() == "browser"
            )
            browser_pid = int(browser_row.get("id") or 0)
            if browser_pid <= 0:
                return

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, browser_pid)
            if not handle:
                return
            self._browser_process_id = browser_pid
            self._browser_process_handle = int(handle)
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] browser process tracked: "
                f"pid={browser_pid}"
            )
        except Exception as exc:
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] browser process probe unavailable: "
                f"{type(exc).__name__}: {exc}"
            )
        finally:
            if cdp_session is not None:
                try:
                    await cdp_session.detach()
                except Exception:
                    pass

    def _mark_browser_disconnected(self, browser) -> None:
        """记录浏览器断开，让没有立即抛异常的轮询也能结束或恢复。"""
        if browser is not self._tracked_browser:
            return
        self._browser_disconnected = True
        self._browser_disconnected_at = time.monotonic()

    def _track_page_lifecycle(self, page) -> None:
        """区分用户关闭有头页面、代码主动关闭和页面崩溃。"""
        if self.headless or getattr(page, "_usergrowth_lifecycle_tracked", False):
            return
        page._usergrowth_lifecycle_tracked = True
        page.on("crash", lambda *_: self._mark_page_crashed(page))
        page.on("close", lambda *_: self._mark_page_closed(page))

    def _mark_page_crashed(self, page) -> None:
        self._crashed_page_ids.add(id(page))

    def _mark_page_closed(self, page) -> None:
        page_id = id(page)
        if (
                self.headless
                or self._cancel_requested()
                or page_id in self._crashed_page_ids
                or page_id in self._intentional_page_close_ids
        ):
            return
        self._user_closed_headed_page = True
        self._user_closed_headed_page_at = time.monotonic()
        self._write_run_log(
            f"[{datetime.now().isoformat(timespec='seconds')}] headed browser page closed by user"
        )
        self._write_event("browser_page_closed_by_user")

    async def _close_page_intentionally(self, page) -> None:
        """关闭流程自己的辅助页，不把它误判为用户取消。"""
        self._intentional_page_close_ids.add(id(page))
        await page.close()

    async def _close_browser_intentionally(self, browser) -> None:
        """关闭流程自己的浏览器，并预先标记其中页面为预期关闭。"""
        try:
            for context in list(browser.contexts):
                for page in list(context.pages):
                    self._intentional_page_close_ids.add(id(page))
        except Exception:
            pass
        await browser.close()

    def _read_browser_process_exit_code(self) -> int | None:
        """读取已跟踪浏览器的退出码；259 表示进程仍在运行。"""
        if os.name != "nt" or not self._browser_process_handle:
            return None
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            exit_code = wintypes.DWORD(0)
            if not kernel32.GetExitCodeProcess(
                    wintypes.HANDLE(self._browser_process_handle),
                    ctypes.byref(exit_code),
            ):
                return None
            return int(exit_code.value)
        except Exception:
            return None

    async def _wait_for_browser_process_exit_code(self, wait_seconds: float = 2.0) -> int | None:
        """短暂等待浏览器进程退出，避免断开事件早于退出码更新。"""
        deadline = time.monotonic() + max(wait_seconds, 0.0)
        while True:
            exit_code = self._read_browser_process_exit_code()
            if exit_code is None or exit_code != 259:
                return exit_code
            if time.monotonic() >= deadline:
                return exit_code
            await asyncio.sleep(0.1)

    async def _raise_if_user_closed_browser(
            self,
            exc: BaseException,
            progress: ProgressCallback | None = None,
    ) -> None:
        """用户关闭有头页面或正常退出时停止；异常退出仍交给断点恢复。"""
        if self._user_closed_headed_page:
            # 关闭事件可能略早于 browser disconnected。短暂等待后，浏览器主进程
            # 仍在线说明用户只是点了窗口 X；非零/未知异常退出继续走断点恢复。
            deadline = time.monotonic() + 0.75
            while time.monotonic() < deadline:
                exit_code = self._read_browser_process_exit_code()
                if self._browser_disconnected or exit_code not in {None, 259}:
                    break
                await asyncio.sleep(0.05)
            exit_code = self._read_browser_process_exit_code()
            if (
                    (not self._browser_disconnected and exit_code in {None, 259})
                    or exit_code == 0
            ):
                message = "检测到用户主动关闭浏览器，任务已停止，不自动重启"
                self._emit(progress, message)
                raise UserGrowthCancelled(message) from exc
        if not self._is_session_closed_exception(exc):
            return
        exit_code = await self._wait_for_browser_process_exit_code()
        self._write_run_log(
            f"[{datetime.now().isoformat(timespec='seconds')}] browser disconnected: "
            f"pid={self._browser_process_id or ''}, exit_code={exit_code}"
        )
        if exit_code != 0:
            return
        message = "检测到用户主动关闭浏览器，任务已停止，不自动重启"
        self._emit(progress, message)
        self._write_event(
            "browser_closed_by_user",
            browser_pid=self._browser_process_id,
            exit_code=exit_code,
        )
        raise UserGrowthCancelled(message) from exc

    def _close_browser_process_probe(self) -> None:
        """释放用于读取 Chromium 退出码的 Windows 进程句柄。"""
        handle = self._browser_process_handle
        self._browser_process_handle = None
        self._browser_process_id = None
        self._tracked_browser = None
        self._browser_disconnected = False
        self._browser_disconnected_at = 0.0
        if os.name != "nt" or not handle:
            return
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(wintypes.HANDLE(handle))
        except Exception:
            pass

    async def _login(self, page, progress: ProgressCallback | None) -> None:
        """打开登录页，填写账号密码并自动识别图片验证码。"""
        if self.storage_state:
            self._emit(progress, "验证已保存的 UserGrowth 登录会话")
            await self._safe_goto(page, HOME_URL)
            await page.wait_for_timeout(2500)
            if await self._looks_logged_in(page):
                self.session_authenticated = True
                self._emit(progress, "复用已保存的 UserGrowth 登录会话")
                return
            self._emit(progress, "已保存的 UserGrowth 登录会话失效，重新登录")
            self.storage_state = None
            self._storage_state_source = ""
            clear_session_cache(self.session_cache_path)
        self._emit(progress, "打开 UserGrowth 登录页")
        await self._safe_goto(page, LOGIN_URL)
        await self._snapshot(page, "01_open_login")
        if await self._looks_logged_in(page):
            self.session_authenticated = True
            self._emit(progress, "当前浏览器上下文已登录")
            return

        for attempt in range(1, 6):
            await self._wait_login_form_ready(page, progress)
            if await self._looks_logged_in(page):
                self.session_authenticated = True
                self._emit(progress, "当前浏览器上下文已登录")
                return
            self._emit(progress, f"填写账号密码并识别验证码，第 {attempt} 次")
            await self._fill_first(
                page,
                (
                    "input[placeholder='请输入注册邮箱']",
                    "input[type='text']",
                ),
                self.account,
            )
            await self._fill_first(page, ("input[type='password']", "input[placeholder*='密码']"), self.password)
            await self._fill_captcha_until_available(page, progress)
            await self._snapshot(page, f"login_attempt_{attempt}_filled")
            await self._click_first(page, ("button:has-text('登录')", "button:has-text('登 录')", "button"))
            outcome = await self._wait_login_outcome(page, progress)
            if outcome == "success":
                self.session_authenticated = True
                self.login_performed = True
                self._emit(progress, "登录成功")
                return
            await self._snapshot_error(page, f"login_failed_{attempt}")
            self._emit(progress, f"登录页明确返回失败，准备下一次验证码：{outcome}")
            await self._refresh_login_for_retry(page, progress, reason=outcome)
        raise RuntimeError("UserGrowth 登录失败：验证码或账号密码未通过")

    async def _refresh_login_for_retry(
            self,
            page,
            progress: ProgressCallback | None,
            *,
            reason: str = "",
    ) -> None:
        """复用红果登录恢复策略，刷新当前登录页并等待新验证码可用。

        登录接口返回验证码错误时，直接导航到相同 URL 在 SPA 中可能不会重新
        挂载验证码组件，最终表现为白屏或旧验证码卡住。先 reload 当前页面，
        只有 reload 失败时才退回安全导航；两种路径都等待表单重新可操作。
        """
        detail = f"：{reason}" if str(reason or "").strip() else ""
        self._emit(progress, f"刷新登录页获取新验证码并重试{detail}")
        try:
            await page.reload(wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception as exc:
            if self._is_session_closed_exception(exc):
                raise
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] login reload failed, "
                f"fallback goto: {type(exc).__name__}: {exc}"
            )
            await self._safe_goto(page, LOGIN_URL)
        await page.wait_for_timeout(2500)
        await self._wait_login_form_ready(page, progress)

    async def _wait_login_outcome(self, page, progress: ProgressCallback | None) -> str:
        """等待登录明确成功或明确失败；页面仍在处理中时不刷新、不跳首页。"""
        failure_markers = (
            "验证码错误",
            "验证码不正确",
            "验证码失效",
            "验证码过期",
            "图片验证码由4位字符组成",
            "验证码由4位字符组成",
            "图片验证码长度错误",
            "验证码长度错误",
            "请输入4位验证码",
            "请重新输入验证码",
            "图片验证码错误",
            "请填写正确的图片验证码",
            "校验码错误",
            "校验码不正确",
            "验证失败",
            "账号或密码错误",
            "用户名或密码错误",
            "邮箱或密码错误",
            "登录失败",
            "请检查账号密码",
        )
        wait_round = 0
        form_ready_rounds = 0
        while True:
            self._raise_if_cancelled()
            if await self._looks_logged_in(page):
                return "success"
            body = await self._body_text(page, timeout_ms=2500)
            # 登录失败提示通常由 Arco toast/notification 挂载到 body 外层，
            # 有时只短暂出现在 alert 节点里；单读 body 会漏掉验证码错误。
            transient_messages = await self._login_transient_messages(page)
            compact_body = _compact_text("\n".join((body, transient_messages)))
            for marker in failure_markers:
                if _compact_text(marker) in compact_body:
                    return marker
            if await self._login_form_is_actionable(page):
                form_ready_rounds += 1
            else:
                form_ready_rounds = 0
            if form_ready_rounds >= 90:
                return "登录表单恢复可操作但长时间未跳转，刷新验证码重试"
            wait_round += 1
            if wait_round == 1 or wait_round % 30 == 0:
                self._emit(progress, f"登录请求仍在处理，保持当前页面等待（第 {wait_round} 次）")
            await self._sleep(1.0)

    async def _login_transient_messages(self, page) -> str:
        """读取登录页 toast/alert 等短暂错误提示，不把普通表单文案当失败。"""
        selectors = (
            "[role='alert']",
            ".arco-message",
            ".arco-notification",
            ".arco-alert",
            "[class*='toast']",
            "[class*='notification']",
            "[class*='message']",
        )
        texts: list[str] = []
        for selector in selectors:
            try:
                values = await page.locator(selector).all_text_contents(timeout=1200)
            except Exception as exc:
                if self._is_session_closed_exception(exc):
                    raise
                continue
            texts.extend(str(value or "").strip() for value in values if str(value or "").strip())
        return "\n".join(texts)

    async def _login_form_is_actionable(self, page) -> bool:
        """登录提交后表单重新可操作，表示本次请求已结束但未登录。"""
        selectors = (
            "input[placeholder='请输入注册邮箱']",
            "input[type='password']",
            "input[placeholder='请输入图片验证码']",
            "button:has-text('登录')",
            "button:has-text('登 录')",
        )
        visible = 0
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.count() and await locator.is_visible() and await locator.is_enabled():
                    visible += 1
            except Exception as exc:
                if self._is_session_closed_exception(exc):
                    raise
        return visible >= 4

    async def _wait_login_form_ready(self, page, progress: ProgressCallback | None) -> None:
        """登录页白屏或延迟渲染时保持浏览器打开，直到账号和密码输入框都出现。"""
        attempt = 0
        delay_seconds = 2.0
        while True:
            self._raise_if_cancelled()
            if await self._looks_logged_in(page):
                return
            try:
                account_box = page.locator("input[placeholder='请输入注册邮箱'], input[type='text']").first
                password_box = page.locator("input[type='password'], input[placeholder*='密码']").first
                if (
                        await account_box.count()
                        and await password_box.count()
                        and await account_box.is_visible()
                        and await password_box.is_visible()
                ):
                    return
            except Exception as exc:
                if self._is_session_closed_exception(exc):
                    raise
            attempt += 1
            if attempt == 1 or attempt % 5 == 0:
                self._emit(progress, f"登录页仍在加载，保持浏览器打开继续等待（第 {attempt} 次）")
            await self._sleep(delay_seconds)
            delay_seconds = min(delay_seconds * 2, 30.0)

    async def _fill_captcha_until_available(self, page, progress: ProgressCallback | None) -> None:
        """验证码图片截图失败时刷新登录页，一直重试到成功或用户取消。"""
        refresh_count = 0
        while True:
            self._raise_if_cancelled()
            try:
                await self._fill_captcha(page)
                return
            except RuntimeError as exc:
                message = str(exc)
                if not (message.startswith("验证码图片未找到") or message.startswith("验证码截图失败")):
                    raise
                refresh_count += 1
                self._emit(progress, f"验证码截图失败，刷新登录页继续重试（第 {refresh_count} 次）")
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] captcha screenshot retry "
                    f"{refresh_count}: {message}"
                )
                await self._refresh_login_for_retry(page, progress, reason=message)

    async def _enable_post_login_resource_blocking(self, context, progress: ProgressCallback | None = None) -> None:
        """登录后拦截非必要静态资源，降低后续页面加载成本。"""

        async def handle_route(route) -> None:
            if self._should_block_static_resource(route.request):
                await route.abort()
                return
            await route.continue_()

        await context.route("**/*", handle_route)
        self._emit(progress, "已开启登录后非业务资源拦截：图片、字体、视频、favicon、第三方埋点和广告")

    @staticmethod
    def _should_block_static_resource(request) -> bool:
        """判断登录后的请求是否属于可拦截的非业务资源。"""
        resource_type = getattr(request, "resource_type", "")
        if resource_type in {"image", "font", "media"}:
            return True
        raw_url = str(getattr(request, "url", "") or "")
        url = raw_url.lower()
        if "favicon.ico" in url:
            return True

        parts = urlsplit(raw_url)
        hostname = (parts.hostname or "").lower()
        third_party_tracking_hosts = (
            "google-analytics.com",
            "googletagmanager.com",
            "googlesyndication.com",
            "doubleclick.net",
            "facebook.net",
            "clarity.ms",
            "hotjar.com",
            "mixpanel.com",
            "amplitude.com",
            "sentry.io",
            "bugsnag.com",
            "hm.baidu.com",
            "cnzz.com",
            "umeng.com",
        )
        if any(hostname == host or hostname.endswith(f".{host}") for host in third_party_tracking_hosts):
            return True

        # 只对非 UserGrowth 主站的明确采集/广告路径做保守拦截，避免误伤业务 API。
        if hostname and not hostname.endswith("usergrowth.com.cn"):
            tracking_path_markers = (
                "/collect",
                "/analytics",
                "/tracking",
                "/track.",
                "/pixel",
                "/beacon",
                "/adservice",
                "/ads/",
                "/advertising/",
            )
            if any(marker in parts.path.lower() for marker in tracking_path_markers):
                return True
        return False

    async def _looks_logged_in(self, page) -> bool:
        """根据 URL 和页面文本判断当前是否已登录。"""
        current_url = str(page.url or "")
        if "/home" in current_url or "/open/customer" in current_url:
            return True
        try:
            body = await page.locator("body").inner_text(timeout=5000)
        except Exception:
            return False
        return "墨攻AI" in body or "采购中心" in body or "客户列表" in body

    async def _fill_captcha(self, page) -> None:
        """查找验证码输入框和验证码图片，并把识别结果填入输入框。"""
        captcha_input = await self._first_existing(
            page,
            (
                "input[placeholder='请输入图片验证码']",
            ),
        )
        if not captcha_input:
            return

        captcha_image = await self._find_captcha_image(page, captcha_input)
        if not captcha_image:
            raise RuntimeError("验证码图片未找到")
        try:
            image_bytes = await asyncio.wait_for(
                captcha_image.screenshot(timeout=8000),
                timeout=10.0,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError("验证码截图失败：超过 10 秒未完成") from exc
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"验证码截图失败：{exc}") from exc
        self._captcha_solver = self._captcha_solver or UserGrowthCaptchaSolver()
        code = self._captcha_solver.solve(image_bytes)
        await captcha_input.fill(code)

    async def _find_captcha_image(self, page, captcha_input=None):
        """在登录页图片中选出最像验证码的图片元素。"""
        images = page.locator("img")
        count = await images.count()
        if not count:
            return None
        # 输入框中心坐标；拿不到就退化成"只比面积"
        cx, cy = None, None
        if captcha_input:
            try:
                box = await captcha_input.bounding_box()
                if box:
                    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            except Exception:
                pass
        # 一次遍历：尺寸合规 + 离输入框最近，分最高的就是验证码
        best, best_score = None, -1
        for i in range(count):
            try:
                box = await images.nth(i).bounding_box()
                if not box or not (50 <= box["width"] <= 260 and 20 <= box["height"] <= 100):
                    continue
                score = box["width"] * box["height"]
                if cx is not None:
                    score -= abs(box["x"] + box["width"] / 2 - cx) * 2
                    score -= abs(box["y"] + box["height"] / 2 - cy) * 6
                if score > best_score:
                    best, best_score = images.nth(i), score
            except Exception:
                continue
        if best:
            return best
        # 兜底：尺寸没一个合规的，从后往前拿第一个可见图片
        for i in range(count - 1, -1, -1):
            try:
                if await images.nth(i).is_visible():
                    return images.nth(i)
            except Exception:
                continue
        return None

    async def _resume_redfruit_order(
            self,
            page,
            plan: UserGrowthOrderPlan,
            items: list[UserGrowthVideoItem],
            progress: ProgressCallback | None,
    ) -> None:
        """按订单 checkpoint 恢复红果流程，不重新创建已完成的业务步骤。"""
        self._assert_redfruit_state_machine(plan)
        stage = str(plan.stage or "pending")
        if stage == "completed":
            plan.status = "success"
            plan.message = "红果短剧流程已完成，断点续跑跳过"
            return

        arlp_stages = self._redfruit_arlp_stages(items[0]) if items else []
        current_stage_config = (
            arlp_stages[plan.arlp_stage_index]
            if plan.arlp_stage_index < len(arlp_stages)
            else None
        )
        known_failed_second_stage = (
            stage == "arlp_submitting"
            and bool(plan.arlp_task_id)
            and plan.arlp_task_id not in plan.arlp_stage_task_ids
            and self._is_redfruit_second_arlp_stage(current_stage_config)
            and "失败" in str(plan.message or "")
            and self._redfruit_checkpoint_has_all_cids(items)
        )
        if known_failed_second_stage:
            await self._open_work_order_management(page, progress)
            confirmed = await self._verify_redfruit_second_arlp_detail_success(
                page,
                plan.arlp_task_id,
                progress,
                [item.cid for item in items],
            )
            if confirmed:
                plan.arlp_stage_index = min(plan.arlp_stage_index + 1, len(arlp_stages))
                if plan.arlp_task_id not in plan.arlp_stage_task_ids:
                    plan.arlp_stage_task_ids.append(plan.arlp_task_id)
                plan.status = "pending"
                plan.message = ""
                next_stage = "arlp_success" if plan.arlp_stage_index >= len(arlp_stages) else "arlp_submitting"
                self._checkpoint(
                    plan,
                    next_stage,
                    f"断点恢复经素材详情确认 ARLP 第二阶段成功（{plan.arlp_stage_index}/{len(arlp_stages)}）",
                )
                stage = plan.stage
            else:
                raise RuntimeError(
                    f"ARLP 第二阶段任务 {plan.arlp_task_id} 失败，且素材详情未确认目标审核项过审"
                )

        if (
                stage == "arlp_submitting"
                and plan.arlp_task_id
                and plan.arlp_task_id not in plan.arlp_stage_task_ids
        ):
            stage_index = max(plan.arlp_stage_index, 0)
            if current_stage_config:
                self._update_arlp_progress(
                    plan,
                    stage_index,
                    stage_config=current_stage_config,
                    status="waiting_result",
                    step="waiting_result",
                    task_id=plan.arlp_task_id,
                    attempt=max(
                        int(self._arlp_progress_entry(plan, stage_index, current_stage_config).get("attempt") or 1),
                        1,
                    ),
                    message=f"断点恢复等待 ARLP 任务 {plan.arlp_task_id}",
                )
            await self._open_work_order_management(page, progress)
            await self._search_task_by_id(page, plan.arlp_task_id)
            try:
                result = await self._wait_redfruit_arlp_task_result(
                    page,
                    progress,
                    "增加 ARLP",
                    stage_config=current_stage_config,
                    expected_count=len(items),
                    verification_cids=[item.cid for item in items],
                )
            except Exception as exc:  # noqa: BLE001
                if current_stage_config:
                    self._update_arlp_progress(
                        plan,
                        stage_index,
                        stage_config=current_stage_config,
                        status="failed",
                        step="waiting_result_failed",
                        task_id=plan.arlp_task_id,
                        last_error=str(exc),
                        message=f"断点恢复等待 ARLP 任务失败：{exc}",
                    )
                raise
            if current_stage_config:
                self._update_arlp_progress(
                    plan,
                    stage_index,
                    stage_config=current_stage_config,
                    status=("success" if self._redfruit_operation_all_expected_success(result, len(items)) else "partial_failure"),
                    step="result_received",
                    task_id=str(result.get("task_id") or plan.arlp_task_id),
                    total=int(result.get("total") or 0),
                    success=int(result.get("success") or 0),
                    failed=int(result.get("failed") or 0),
                    message=f"断点恢复收到 ARLP 任务 {plan.arlp_task_id} 结果",
                )
            if self._redfruit_operation_all_expected_success(result, len(items)):
                completed_index = min(plan.arlp_stage_index + 1, len(arlp_stages))
                plan.arlp_stage_index = completed_index
                if plan.arlp_task_id and plan.arlp_task_id not in plan.arlp_stage_task_ids:
                    plan.arlp_stage_task_ids.append(plan.arlp_task_id)
                next_stage = "arlp_success" if completed_index >= len(arlp_stages) else "arlp_submitting"
                self._checkpoint(
                    plan,
                    next_stage,
                    f"断点恢复确认 ARLP 阶段 {completed_index}/{len(arlp_stages)} 已成功",
                )
                stage = plan.stage
            else:
                self._emit(progress, "断点恢复发现当前 ARLP 阶段未覆盖全部素材，将回素材管理补做该阶段")

        # ARLP/分类标签阶段如果已经把本批 CID 写入 checkpoint，旧的上传/送审
        # 任务就不再是恢复入口。直接去素材管理按 CID 搜索，避免被历史任务状态
        # 卡住，也避免误回到上传流程。
        if stage in {
                "review_submitted",
                "arlp_submitting",
                "arlp_success",
                "classification_submitting",
        } and self._redfruit_checkpoint_has_all_cids(items):
            self._emit(
                progress,
                f"红果断点续跑：本批 {len(items)} 个 CID 已存在，跳过旧任务，直接进入素材管理搜索",
            )
            material_page = await self._open_redfruit_material_management_by_cids(
                page,
                items,
                progress,
            )
            await self._process_redfruit_material_stages(material_page, plan, items, progress)
            return

        task_id = str(plan.review_task_id or plan.upload_task_id or plan.task_id or "").strip()
        if not task_id:
            if stage == "upload_processing":
                existing_items = [
                    item for item in items
                    if item.status not in {"success", "skipped"}
                    and self._existing_creative_unit_id_for_item(item)
                ]
                already_done = [item for item in items if item.status in {"success", "skipped"}]
                if existing_items:
                    self._emit(
                        progress,
                        f"红果断点续跑：上传任务号缺失，已记录 {len(existing_items)}/{len(items)} 个原创意单元，"
                        "跳过重新上传，直接进入创意单元补录",
                    )
                    await self._open_work_order_management(page, progress)
                    await self._process_existing_creative_unit_items(page, plan, existing_items, progress)
                    remaining = [item for item in items if item.status not in {"success", "skipped"}]
                    if not remaining:
                        plan.status = "success"
                        plan.message = "红果原创意单元补录流程完成"
                        self._checkpoint(
                            plan,
                            "completed",
                            "红果断点补录完成；平台提示已录入为素材时直接结束",
                        )
                        return
                    plan.status = "failed"
                    plan.message = (
                        f"红果断点已补录 {len(items) - len(remaining)}/{len(items)} 个素材，"
                        f"仍有 {len(remaining)} 个素材未恢复；已阻止重新上传"
                    )
                    self._checkpoint(plan, "upload_processing", plan.message)
                    raise RuntimeError(plan.message)
                if already_done and len(already_done) == len(items):
                    plan.status = "success"
                    plan.message = "红果原创意单元补录流程已完成"
                    self._checkpoint(plan, "completed", "红果断点补录已完成，跳过重复补录")
                    return
                plan.status = "failed"
                plan.message = "红果断点缺少上传任务号和原创意单元 ID，已阻止重新上传"
                raise RuntimeError(plan.message)
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] redfruit resume: "
                f"order_id={plan.order_id}, stage={stage}, task_id missing; restart upload path"
            )
            plan.stage = "preflight_done"
            await self._process_order(page, plan, progress)
            return

        self._emit(progress, f"红果断点续跑：订单 {plan.order_id}，当前阶段 {stage}，任务 {task_id}")
        await self._open_work_order_management(page, progress)

        if stage == "classification_submitting":
            # 修改分类标签保存后不会提供稳定的任务详情入口。断点恢复时直接回到
            # 本批素材列表重新全选并保存，保持操作幂等且不再等待 ARLP 任务页。
            plan.classification_task_id = ""
            self._emit(progress, "断点恢复修改分类标签：直接回素材管理重新保存，不进入任务详情页")

        if stage in {"upload_processing", "upload_task_created", "upload_success"}:
            if stage == "upload_processing":
                self._emit(progress, f"红果短剧已记录上传任务 {task_id}，断点续跑跳过重复上传")
            detail_page = await self._open_task_detail_for_task_id(
                page,
                task_id,
                progress,
                expected_attempts=max(len(items), 1),
                retry_failed_task=True,
            )
            plan.upload_task_id = task_id
            plan.task_id = task_id
            self._checkpoint(plan, "upload_success", f"断点恢复确认上传任务 {task_id} 全部成功")
            await self._submit_review(detail_page)
            plan.review_task_id = task_id
            self._checkpoint(plan, "review_submitted", f"断点恢复后完成送审，任务 ID：{task_id}")
            await self._process_redfruit_after_review(detail_page, plan, items, task_id, progress)
            return

        if stage in {
                "review_submitted",
                "arlp_submitting",
                "arlp_success",
                "classification_submitting",
        }:
            detail_page = await self._open_task_detail_for_task_id(
                page,
                task_id,
                progress,
                expected_attempts=max(len(items), 1),
            )
            await self._process_redfruit_after_review(detail_page, plan, items, task_id, progress)
            return

        if stage == "classification_success":
            plan.status = "success"
            plan.message = "红果分类标签已完成，断点续跑跳过"
            self._checkpoint(plan, "completed", "断点确认红果分类标签已完成")
            return

        self._write_run_log(
            f"[{datetime.now().isoformat(timespec='seconds')}] redfruit resume: "
            f"unknown stage={stage}; restart from preflight-complete upload path"
        )
        plan.stage = "preflight_done"
        await self._process_order(page, plan, progress)

    @staticmethod
    def _redfruit_checkpoint_has_all_cids(items: list[UserGrowthVideoItem]) -> bool:
        """判断断点中的 CID 是否足够直接恢复素材管理后半段。"""
        active_items = [item for item in items if item.status != "skipped"]
        cids = [str(item.cid or "").strip().lower() for item in active_items]
        return bool(cids) and all(cids) and len(set(cids)) == len(cids)

    async def _resume_soda_order(
            self,
            page,
            plan: UserGrowthOrderPlan,
            items: list[UserGrowthVideoItem],
            progress: ProgressCallback | None,
    ) -> None:
        """按汽水音乐 checkpoint 恢复上传、送审和 CID 回填。"""
        stage = str(plan.stage or "pending")
        if stage == "completed":
            plan.status = "success"
            plan.message = "汽水音乐流程已完成，断点续跑跳过"
            return
        if stage == "cid_backfilled_unreviewed":
            plan.status = "success"
            plan.message = "CID 已备份，当前任务未送审；断点续跑不自动重复送审"
            return

        task_id = str(plan.review_task_id or plan.upload_task_id or plan.task_id or "").strip()
        if not task_id:
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] soda resume: "
                f"order_id={plan.order_id}, stage={stage}, task_id missing; restart upload path"
            )
            plan.stage = "pending"
            await self._process_order(page, plan, progress)
            return

        self._emit(progress, f"汽水音乐断点续跑：订单 {plan.order_id}，当前阶段 {stage}，任务 {task_id}")
        await self._open_work_order_management(page, progress)

        detail_page = None
        if stage in {"upload_processing", "upload_task_created"}:
            if stage == "upload_processing":
                self._emit(progress, f"汽水音乐已记录上传任务 {task_id}，断点续跑跳过重复上传")
            detail_page = await self._open_task_detail_for_task_id(
                page,
                task_id,
                progress,
                expected_attempts=max(len(items), 1),
            )
            plan.task_id = task_id
            plan.upload_task_id = task_id
            self._checkpoint(plan, "upload_success", f"断点恢复确认上传任务 {task_id} 全部成功")

        if stage in {"upload_success", "review_submitting"} or plan.stage == "upload_success":
            if detail_page is None:
                detail_page = await self._open_task_detail_for_task_id(
                    page,
                    task_id,
                    progress,
                    expected_attempts=max(len(items), 1),
                    retry_failed_task=stage == "review_submitting",
                    operation_name="汽水音乐送审",
                    plan=plan,
                )
            await self._submit_soda_review(
                detail_page,
                plan,
                task_id,
                progress,
                allow_already_submitted=stage == "review_submitting",
            )
            self._checkpoint(plan, "cid_backfilling", f"开始读取任务 {task_id} 的 CID 并回填")
            await self._fill_cids_for_task(
                detail_page,
                items,
                task_id,
                progress,
                retry_failed_task=True,
                operation_name="汽水音乐送审",
                plan=plan,
            )
            plan.status = "success"
            plan.message = "汽水音乐断点恢复、送审和 CID 回填完成"
            self._checkpoint(plan, "completed", "汽水音乐流程全部完成")
            return

        if stage in {"review_submitted", "cid_backfilling"}:
            plan.task_id = task_id
            plan.review_task_id = task_id
            self._checkpoint(plan, "cid_backfilling", f"断点恢复后继续读取任务 {task_id} 的 CID")
            await self._fill_cids_for_task(
                page,
                items,
                task_id,
                progress,
                retry_failed_task=True,
                operation_name="汽水音乐送审",
                plan=plan,
            )
            plan.status = "success"
            plan.message = "汽水音乐断点恢复 CID 回填完成"
            self._checkpoint(plan, "completed", "汽水音乐流程全部完成")
            return

        self._write_run_log(
            f"[{datetime.now().isoformat(timespec='seconds')}] soda resume: "
            f"unknown stage={stage}; restart upload path"
        )
        plan.stage = "pending"
        await self._process_order(page, plan, progress)

    async def _process_order(self, page, plan: UserGrowthOrderPlan, progress: ProgressCallback | None) -> None:
        """处理单个订单：进入工单、上传素材、录入变色龙、送审并读取 CID。"""
        active_items = [item for item in plan.items if item.status != "skipped"]
        if not active_items:
            plan.status = "skipped"
            plan.message = "没有可上传素材"
            return
        original_active_items = list(active_items)
        upload_items = [
            item for item in original_active_items
            if item.status not in {"success", "deferred_existing_creative_unit"}
            and not self._existing_creative_unit_id_for_item(item)
        ]
        redfruit_flow = self._is_redfruit_items(active_items)

        redfruit_recorded_task_id = str(
            plan.review_task_id or plan.upload_task_id or plan.task_id or ""
        ).strip()
        if redfruit_flow and plan.stage not in {"pending", "preflight_done"}:
            await self._resume_redfruit_order(page, plan, active_items, progress)
            return
        soda_recorded_task_id = str(
            plan.review_task_id or plan.upload_task_id or plan.task_id or ""
        ).strip()
        if not redfruit_flow and (
                plan.stage not in {"pending", "upload_processing"}
                or (plan.stage == "upload_processing" and soda_recorded_task_id)
        ):
            await self._resume_soda_order(page, plan, active_items, progress)
            return

        self._emit(progress, f"处理订单 {plan.order_id}，素材 {len(active_items)} 个")
        try:
            self._raise_if_cancelled()
            await self._open_work_order_management(page, progress)
            await self._search_order(page, plan.order_id)
            if redfruit_flow and plan.stage == "pending":
                await self._run_redfruit_preflight_checks(page, plan, active_items, progress)
                self._checkpoint(plan, "preflight_done", "红果工单、剧目类型和 BID 前置校验通过")
            if upload_items:
                workflow_label = "红果短剧" if redfruit_flow else "汽水音乐"
                self._checkpoint(plan, "upload_processing", f"{workflow_label}开始创建创意单元并上传素材")
            if not upload_items:
                self._emit(progress, "断点已恢复：本批没有需要重新上传的素材，直接处理已存在创意单元")
                await self._process_existing_creative_unit_items(
                    page,
                    plan,
                    [
                        item
                        for item in original_active_items
                        if item.status not in {"success", "skipped"}
                        and self._existing_creative_unit_id_for_item(item)
                    ],
                    progress,
                )
                plan.status = "success"
                plan.message = "处理完成"
                self._checkpoint(plan, "completed", "已存在创意单元补录流程全部完成")
                return
            page = await self._open_create_creative_unit(page, plan.order_id)
            await self._snapshot(page, f"order_{plan.order_id}_after_create")
            limit = await self._read_upload_limit(page)
            plan.upload_limit = limit
            if self._confirmed_upload_limit_exceeded(limit, len(upload_items)):
                plan.status = "skipped"
                plan.message = f"超过页面上传限制：最多 {limit} 个，实际 {len(upload_items)} 个"
                for item in upload_items:
                    item.status = "skipped"
                    item.message = plan.message
                return
            page = await self._upload_and_enter_chameleon_with_retry(page, plan, upload_items, progress)
            existing_unit_items = [
                item
                for item in original_active_items
                if item.status not in {"success", "skipped"}
                and self._existing_creative_unit_id_for_item(item)
            ]
            if upload_items:
                plan.task_id = await self._read_current_task_id(page, progress=progress)
                if not plan.task_id:
                    await self._snapshot_error(page, f"order_{plan.order_id}_task_id_not_found")
                    raise RuntimeError("未读取到当前任务ID")
                plan.upload_task_id = plan.task_id
                self._checkpoint(plan, "upload_task_created", f"已记录上传任务 ID：{plan.task_id}")

                wait_result = await self._wait_task_success(
                    page,
                    progress,
                    expected_attempts=max(len(upload_items), 1),
                    plan=None if redfruit_flow else plan,
                    items=[] if redfruit_flow else upload_items,
                    task_id=plan.task_id,
                    retry_failed_task=redfruit_flow,
                    operation_name="素材上传",
                )
                if wait_result != "cid_backed_up":
                    self._checkpoint(plan, "upload_success", f"上传任务 {plan.task_id} 全部成功")
                    if redfruit_flow:
                        await self._submit_review(page)
                        plan.review_task_id = plan.task_id
                        self._checkpoint(plan, "review_submitted", f"已完成送审，任务 ID：{plan.review_task_id}")
                        await self._process_redfruit_after_review(page, plan, upload_items, plan.task_id, progress)
                    else:
                        await self._submit_soda_review(page, plan, plan.task_id, progress)
                        self._checkpoint(plan, "cid_backfilling", f"开始读取任务 {plan.task_id} 的 CID 并回填")
                        await self._fill_cids_for_task(
                            page,
                            upload_items,
                            plan.task_id,
                            progress,
                            retry_failed_task=True,
                            operation_name="汽水音乐送审",
                            plan=plan,
                        )
                        self._checkpoint(plan, "completed", "汽水音乐上传、送审和 CID 回填全部完成")
                else:
                    self._checkpoint(plan, "cid_backfilled_unreviewed", "CID 已备份回填，当前任务尚未送审")
            elif existing_unit_items:
                self._emit(progress, "本轮新上传素材均命中已存在创意单元，跳过新建单元录入")

            if existing_unit_items:
                page = await self._process_existing_creative_unit_items(
                    page,
                    plan,
                    existing_unit_items,
                    progress,
                )
            plan.status = "success"
            plan.message = "处理完成"
            if plan.stage not in {"cid_backfilled_unreviewed", "completed"}:
                self._checkpoint(plan, "completed", "订单流程全部完成")
        except UserGrowthCancelled as exc:
            plan.status = "cancelled"
            plan.message = str(exc) or "任务已取消"
            for item in active_items:
                if item.status not in {"success", "skipped"}:
                    item.status = "cancelled"
                    item.message = plan.message
            raise
        except Exception as exc:  # noqa: BLE001
            if self._is_recoverable_session_exception(exc):
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] order suspended for recovery: "
                    f"order_id={plan.order_id}, error={type(exc).__name__}: {exc}"
                )
                raise
            plan.status = "failed"
            plan.message = str(exc)
            await self._snapshot_error(
                page,
                f"order_{plan.order_id}_failed",
                exc=exc,
                extra=f"order_id={plan.order_id}, items={len(active_items)}",
            )
            for item in active_items:
                if item.status not in {"success", "skipped"}:
                    item.status = "failed"
                    item.message = str(exc)

    async def _upload_and_enter_chameleon_with_retry(
            self,
            page,
            plan: UserGrowthOrderPlan,
            items: list[UserGrowthVideoItem],
            progress: ProgressCallback | None,
    ):
        """上传素材并进入变色龙；可恢复页面故障不按固定次数结束。"""
        upload_url = page.url
        attempt = 0
        retry_delay_seconds = 2.0
        while True:
            self._raise_if_cancelled()
            attempt += 1
            self._emit(progress, f"上传订单 {plan.order_id} 素材，第 {attempt} 次")
            if attempt > 1:
                await self._reset_upload_page(page, upload_url, plan.order_id, attempt)
            # 内部失败时会刷新页面并从「新建创意单元」入口重新走，
            # 拿到最终的 page（可能是新 tab）继续后续流程
            page = await self._upload_files(page, items, plan.order_id)
            try:
                return await self._enter_chameleon(page, items, progress, plan=plan)
            except UserGrowthUploadRowFailed:
                raise
            except UserGrowthUploadPageRetry as exc:
                await self._snapshot_error(
                    page,
                    f"upload_current_dialog_retry_{attempt}",
                    exc=exc,
                )
                self._emit(progress, "上传组件额度仍未稳定，保留当前上传弹窗继续恢复")
                attempt -= 1
                await self._sleep(retry_delay_seconds)
                retry_delay_seconds = min(retry_delay_seconds * 2, 30.0)
                continue
            except Exception as exc:
                if not self._is_upload_transient_failure(str(exc)):
                    raise
                await self._snapshot_error(
                    page,
                    f"upload_retry_{attempt}_failed",
                    exc=exc,
                )
                self._emit(progress, f"上传初始化失败，准备重试第 {attempt + 1} 次")
                await self._sleep(retry_delay_seconds)
                retry_delay_seconds = min(retry_delay_seconds * 2, 30.0)

    async def _reset_upload_page(self, page, upload_url: str, order_id: str, attempt: int) -> None:
        """重试上传前回到干净的创意单元上传页。"""
        try:
            await self._safe_goto(page, upload_url)
        except Exception as exc:
            if self._is_recoverable_session_exception(exc):
                raise
            await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        await self._snapshot(page, f"order_{order_id}_upload_retry_{attempt}_reset")

    @staticmethod
    def _is_upload_transient_failure(message: str) -> bool:
        """识别平台上传初始化、网络抖动这类适合重试的错误。"""
        keywords = (
            "上传处理失败",
            "上传失败",
            "当前选择文件数量超过订单创意单元上限",
            "订单创意单元上限: 0",
        )
        return any(keyword in message for keyword in keywords)

    @staticmethod
    def _confirmed_upload_limit_exceeded(limit: int | None, item_count: int) -> bool:
        """只让稳定的正数额度阻断上传；0 视为组件尚未初始化。"""
        return limit is not None and limit > 0 and item_count > limit

    async def _open_work_order_management(
            self,
            page,
            progress: ProgressCallback | None = None,
            *,
            navigate_home: bool = True,
    ) -> None:
        """进入墨攻AI，再点菜单栏的工单管理。

        客户列表的 ``进入`` 会返回带客户上下文的页面。番茄音乐复用本方法时
        传 ``navigate_home=False``，避免重新访问无 selectorId 的首页而丢失客户。
        页面普通加载慢只持续等待；仅在正文明确出现请求/网络/服务错误时刷新。
        """
        self._emit(progress, "进入工单管理")

        if navigate_home:
            try:
                await self._safe_goto(page, HOME_URL)
                await page.wait_for_timeout(2500)
            except Exception as exc:
                if self._is_recoverable_session_exception(exc):
                    raise

        # 客户首页可能先展示一次性提示。汽水、红果和番茄统一走这里：
        # 能点到「我已知悉」就确认；没有该按钮则直接进入后续墨攻导航。
        await self._acknowledge_customer_home(page, progress)

        # 1. 等"墨攻AI"出现；不设总超时，页面短暂白屏或接口慢时持续等待。
        entry_retry = 0
        while True:
            # 首页提示可能在客户上下文加载后异步出现；在等待入口期间持续复用
            # 同一个可选确认步骤，出现就点，不出现则不阻塞墨攻导航。
            await self._acknowledge_customer_home(page, progress)
            if await self._wait_for_page_text(
                    page,
                    ("墨攻AI",),
                    timeout_ms=15000,
                    raise_on_timeout=False,
            ):
                break
            entry_retry += 1
            body = await self._body_text(page, timeout_ms=3000)
            explicit_error = self._explicit_page_error_marker(body)
            self._emit(progress, f"等待墨攻AI入口加载，保持当前页面（第 {entry_retry} 次）")
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] waiting Mogong entry: "
                f"attempt={entry_retry}, explicit_error={explicit_error or 'none'}, url={page.url}"
            )
            if explicit_error:
                self._emit(progress, f"页面明确返回{explicit_error}，刷新当前客户页面后继续等待")
                try:
                    await page.reload(wait_until="domcontentloaded")
                except Exception as reload_exc:
                    if self._is_recoverable_session_exception(reload_exc):
                        raise
            await self._sleep(2)
        # 入口可能与首页提示同时异步出现；点击墨攻AI前再确认一次，封闭竞态。
        await self._acknowledge_customer_home(page, progress)
        await self._click_text(page, "墨攻AI")

        # 2. 等墨攻AI加载（出现工单管理/素材管理菜单就算成功）。若入口点击没有展开，
        # 则继续点入口或回首页重试，避免无日志地卡在菜单等待。
        menu_retry = 0
        while True:
            # 提示可能在点击墨攻AI后才异步出现并遮住入口；等待菜单的每一轮
            # 都重新检查，出现就确认，没有则立即继续。
            await self._acknowledge_customer_home(page, progress)
            if await self._wait_for_page_text(
                    page,
                    ("工单管理", "素材管理"),
                    timeout_ms=12000,
                    raise_on_timeout=False,
            ):
                break
            menu_retry += 1
            body = await self._body_text(page, timeout_ms=3000)
            explicit_error = self._explicit_page_error_marker(body)
            self._emit(progress, f"等待墨攻AI菜单加载，保持当前页面（第 {menu_retry} 次）")
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] waiting Mogong menu: "
                f"attempt={menu_retry}, explicit_error={explicit_error or 'none'}, url={page.url}"
            )
            if explicit_error:
                self._emit(progress, f"墨攻AI页面明确返回{explicit_error}，刷新当前页面后继续等待")
                try:
                    await page.reload(wait_until="domcontentloaded")
                except Exception as reload_exc:
                    if self._is_recoverable_session_exception(reload_exc):
                        raise
            else:
                try:
                    await self._click_text(page, "墨攻AI")
                except RuntimeError as click_exc:
                    if self._is_recoverable_session_exception(click_exc):
                        raise
            await self._sleep(1.8)

        # 3. 点工单管理
        try:
            await self._click_text(page, "工单管理")
        except RuntimeError:
            await self._snapshot_error(page, "work_order_not_reached")
            raise RuntimeError("未找到工单管理菜单")
        await page.wait_for_timeout(2000)
        if not await self._is_work_order_page(page):
            await self._snapshot_error(page, "work_order_not_reached")
            raise RuntimeError("点击工单管理后未进入列表")

    async def _acknowledge_customer_home(
            self,
            page,
            progress: ProgressCallback | None = None,
    ) -> bool:
        """确认客户首页的「我已知悉」提示；没有提示时立即跳过。

        这是所有 UserGrowth 工作流共用的可选前置步骤。它不负责刷新页面，
        页面等待、明确错误刷新和网络恢复仍由共享导航逻辑统一处理。
        """
        # 不存在时立即跳过。提示一旦出现，就留在当前页面持续重试，直到真正
        # 消失；不能仅凭某个包含文案的 div 接受 click() 就误报确认成功。
        if not await self._customer_home_acknowledgement_visible(page):
            return False

        attempt = 0
        while True:
            attempt += 1
            self._raise_if_cancelled()
            try:
                clicked = await self._click_customer_home_acknowledgement(page)
                if clicked:
                    for _ in range(10):
                        await self._sleep(0.2)
                        if not await self._customer_home_acknowledgement_visible(page):
                            self._emit(progress, "已确认首页提示：我已知悉（按钮已消失）")
                            self._write_run_log(
                                f"[{datetime.now().isoformat(timespec='seconds')}] "
                                "customer home acknowledged and dismissed"
                            )
                            return True
            except Exception as exc:
                if self._is_session_closed_exception(exc):
                    raise
            if not await self._customer_home_acknowledgement_visible(page):
                return False
            if attempt == 1 or attempt % 5 == 0:
                self._emit(progress, f"首页提示仍在，继续点击“我已知悉”（第 {attempt} 次）")
            if attempt == 5:
                await self._snapshot_error(
                    page,
                    "customer_home_acknowledgement_stuck",
                    exc=RuntimeError("点击“我已知悉”后提示仍可见"),
                )
            await self._sleep(1)

    async def _customer_home_acknowledgement_visible(self, page) -> bool:
        """判断首页确认按钮是否仍然可见，只匹配按钮本身的精确文案。"""
        try:
            candidates = page.get_by_text(re.compile(r"我\s*已\s*知悉"), exact=False)
            for candidate in await self._visible_locators(candidates, limit=100):
                text = _compact_text(await self._locator_text(candidate, timeout_ms=1000))
                if text == "我已知悉":
                    return True
        except Exception as exc:
            if self._is_session_closed_exception(exc):
                raise
        return False

    async def _click_customer_home_acknowledgement(self, page) -> bool:
        """点击真正的首页确认控件，避免把整块提示容器当成按钮。"""
        button_selector = (
            "button, [role='button'], a, .arco-btn, .ant-btn, "
            "[class*='button'], [class*='Button'], [onclick]"
        )
        try:
            controls = page.locator(button_selector)
            for control in await self._visible_locators(controls, limit=200):
                text = _compact_text(await self._locator_text(control, timeout_ms=1000))
                if text != "我已知悉":
                    continue
                try:
                    await control.scroll_into_view_if_needed(timeout=3000)
                    await control.click(timeout=3000)
                    return True
                except Exception as click_exc:
                    if self._is_session_closed_exception(click_exc):
                        raise
                    if await self._click_locator_center(page, control):
                        return True

            # 文案常在 span 内：只向上寻找最近的按钮/可点击父节点，绝不直接
            # 把包含整段提示内容的普通 div 点击成功当成确认完成。
            texts = page.get_by_text(re.compile(r"我\s*已\s*知悉"), exact=False)
            for candidate in await self._visible_locators(texts, limit=100):
                text = _compact_text(await self._locator_text(candidate, timeout_ms=1000))
                if text != "我已知悉":
                    continue
                parent = candidate.locator(
                    "xpath=ancestor-or-self::*[self::button or self::a or @role='button' or "
                    "@onclick or contains(@class,'btn') or contains(@class,'button') or "
                    "contains(@class,'Button')][1]"
                )
                if await self._click_locator(parent):
                    return True
                if await self._click_locator_center(page, parent):
                    return True
        except Exception as exc:
            if self._is_session_closed_exception(exc):
                raise
        return await self._click_text_or_locator(page, "我已知悉")

    async def _select_customer(
            self,
            page,
            customer_id: str,
            progress: ProgressCallback | None = None,
    ):
        """在客户列表搜索并进入指定客户，所有工作流共用。"""
        customer_id = str(customer_id or "").strip()
        if not customer_id:
            return page
        self._emit(progress, f"等待客户列表渲染，准备选择客户 {customer_id}")
        await self._click_if_present(page, "客户列表")
        search = None
        search_retry = 0
        blank_rounds = 0
        reload_count = 0

        async def recover_customer_list() -> None:
            nonlocal blank_rounds, reload_count
            if not await self._page_is_blank_or_loading(page):
                blank_rounds = 0
                return
            blank_rounds += 1
            # 共享退避：初次稳定白屏约 10 秒后刷新，恢复失败后放宽到约 30 秒。
            threshold = 5 if reload_count == 0 else 15
            if blank_rounds < threshold:
                return
            reload_count += 1
            blank_rounds = 0
            self._emit(progress, f"客户列表白屏，刷新当前页面恢复（第 {reload_count} 次）")
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(1800)

        while search is None:
            search_retry += 1
            search = await self._first_existing(
                page,
                (
                    "input[placeholder*='客户']",
                    "input[placeholder*='ID']",
                    "input[placeholder*='搜索']",
                ),
            )
            if search is not None:
                break
            await recover_customer_list()
            body = await self._body_text(page, timeout_ms=2500)
            explicit_error = self._explicit_page_error_marker(body)
            if explicit_error:
                self._emit(progress, f"客户列表页面明确返回{explicit_error}，刷新后继续等待")
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_timeout(1800)
            elif search_retry == 1 or search_retry % 5 == 0:
                self._emit(progress, f"客户搜索框仍在加载，保持当前页面等待（第 {search_retry} 次）")
            await self._sleep(2)

        await search.fill(customer_id)
        await search.press("Enter")
        await page.wait_for_timeout(2000)

        customer = None
        customer_retry = 0
        while customer is None:
            customer_retry += 1
            candidate = page.get_by_text(
                re.compile(rf"^\s*(?:ID\s*)?{re.escape(customer_id)}\s*$"),
                exact=False,
            ).first
            try:
                if await candidate.count() and await candidate.is_visible():
                    customer = candidate
                    break
            except Exception as exc:
                if self._is_session_closed_exception(exc):
                    raise
            await recover_customer_list()
            if customer_retry == 1 or customer_retry % 5 == 0:
                self._emit(progress, f"客户 {customer_id} 搜索结果仍在加载，保持当前页面等待（第 {customer_retry} 次）")
            await self._sleep(2)

        enter_pattern = re.compile(r"^\s*进\s*入\s*$")
        enter_retry = 0
        while True:
            enter_retry += 1
            for xpath in (
                "xpath=ancestor::*[.//*[contains(translate(normalize-space(.), ' ', ''), '进入')]][1]",
                "xpath=ancestor::tr[1]",
                "xpath=ancestor::*[contains(@class,'ant-card')][1]",
                "xpath=ancestor::*[contains(@class,'card')][1]",
                "xpath=ancestor::*[contains(@class,'item')][1]",
                "xpath=ancestor::*[contains(@class,'row')][1]",
            ):
                container = customer.locator(xpath)
                try:
                    if not await container.count() or not await container.is_visible():
                        continue
                    button = container.get_by_text(enter_pattern, exact=False).first
                    if await button.count() and await button.is_visible():
                        selected_page = await self._click_customer_enter(page, button, customer_id)
                        self._emit(progress, f"已选择客户 {customer_id}")
                        return selected_page
                except RuntimeError:
                    raise
                except Exception:
                    continue

            global_enter = page.get_by_text(enter_pattern, exact=False)
            try:
                if await global_enter.count() == 1 and await global_enter.first.is_visible():
                    selected_page = await self._click_customer_enter(page, global_enter.first, customer_id)
                    self._emit(progress, f"已选择客户 {customer_id}")
                    return selected_page
            except RuntimeError:
                raise
            except Exception:
                pass

            if enter_retry >= 15:
                self._emit(progress, f"客户 {customer_id} 进入按钮长时间未出现，刷新当前页面恢复")
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_timeout(1800)
                enter_retry = 0
            if enter_retry == 1 or enter_retry % 5 == 0:
                self._emit(progress, f"客户 {customer_id} 的进入按钮仍在加载，保持当前页面等待（第 {enter_retry} 次）")
            await self._sleep(2)

    async def _click_customer_enter(self, page, button, customer_id: str):
        pages_before = list(page.context.pages)
        await button.click()
        blank_rounds = 0
        reload_count = 0

        async def selected_page_ready():
            nonlocal blank_rounds, reload_count
            new_pages = [item for item in page.context.pages if item not in pages_before]
            selected_page = new_pages[-1] if new_pages else page
            if selected_page is not page and not getattr(selected_page, "_usergrowth_speed_wrapped", False):
                self._wrap_page_speed(selected_page)
                selected_page.set_default_timeout(self.timeout_ms)
                selected_page.set_default_navigation_timeout(self.timeout_ms)
            if await self._customer_context_selected(selected_page):
                return selected_page
            current_url = str(selected_page.url or "")
            if "/home" in current_url and await self._page_is_blank_or_loading(selected_page):
                blank_rounds += 1
                threshold = 15 if reload_count == 0 else 38
                if blank_rounds >= threshold:
                    reload_count += 1
                    blank_rounds = 0
                    self._emit(None, f"客户首页白屏，刷新当前页面恢复（第 {reload_count} 次）")
                    await selected_page.reload(wait_until="domcontentloaded")
                    await selected_page.wait_for_timeout(1800)
            else:
                blank_rounds = 0
            return None

        return await self._wait_for_result(selected_page_ready, timeout_ms=None, interval_ms=800)

    async def _customer_context_selected(self, page) -> bool:
        current_url = str(page.url or "")
        body = _compact_text(await self._body_text(page, timeout_ms=2500))
        if "墨攻AI" in body or "采购中心" in body:
            return True
        if "/open/customer" in current_url or "客户列表" in body:
            return False
        if not body or body in {"加载中", "正在加载", "loading"}:
            return False
        # 离开客户列表后，必须看到带 selectorId/customerId 的业务上下文，
        # 防止把无客户参数的空白 /home 当成已进入客户。
        if "/home" in current_url:
            query = urlsplit(current_url).query
            return any(
                key in {"selectorId", "customerId", "customer_id"} and value
                for key, value in parse_qsl(query, keep_blank_values=True)
            )
        return False

    async def _is_work_order_page(self, page) -> bool:
        """判断当前页面是否是工单管理列表页。"""
        body = await self._body_text(page)
        if "新建创意单元" in body:
            return True
        return bool(
            await self._first_existing(
                page,
                (
                    "input[placeholder*='订单']",
                ),
            )
        )

    async def _search_order(self, page, order_id: str) -> None:
        """在工单管理页按订单 ID 搜索并确认结果出现。"""
        if not await self._is_work_order_page(page):
            await self._snapshot_error(page, "search_order_not_on_work_order_page")
            raise RuntimeError("未进入工单管理页，不能搜索订单")
        if await self._order_visible(page, order_id):
            return
        search_input = await self._wait_first_existing(
            page,
            (
                "input[placeholder*='订单名称或ID']",
            ),
            timeout_ms=20000,
        )
        if not search_input:
            raise RuntimeError("未找到订单搜索框")
        await self._type_into_locator(search_input, page, order_id)
        await page.wait_for_timeout(1000)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(4000)
        if not await self._order_visible(page, order_id):
            await self._snapshot_error(page, f"order_{order_id}_not_found")
            raise RuntimeError(f"查询结果中未找到订单 {order_id}")

    async def _run_redfruit_preflight_checks(
            self,
            page,
            plan: UserGrowthOrderPlan,
            items: list[UserGrowthVideoItem],
            progress: ProgressCallback | None = None,
    ) -> None:
        """红果短剧正式上传前的工单/剧名标签/BID 前置校验。"""
        order_body = await self._body_text(page, timeout_ms=5000)
        order_title = redfruit_extract_order_title(order_body, plan.order_id)
        order_kind = redfruit_order_kind(order_title)
        item_drama_kinds = {
            redfruit_content_kind(item.workflow_metadata.get("drama_type") or item.file_name or item.song_name)
            for item in items
            if item.status != "skipped"
        }
        item_drama_kinds.discard("")
        item_order_kinds = {
            redfruit_expected_order_kind(
                item.file_name,
                drama_type=item.workflow_metadata.get("drama_type") or item.file_name or item.song_name,
                material_mode=item.workflow_metadata.get("material_mode") or item.material_type,
            )
            for item in items
            if item.status != "skipped"
        }
        item_order_kinds.discard("")
        unknown_kind_files = [
            item.file_name
            for item in items
            if item.status != "skipped"
            and not redfruit_content_kind(
                item.workflow_metadata.get("drama_type") or item.file_name or item.song_name
            )
        ]
        item_titles = sorted(
            {
                str(item.workflow_metadata.get("drama_title") or item.song_name or Path(item.file_name).stem).strip()
                for item in items
                if item.status != "skipped"
            }
        )
        if not order_title:
            await self._snapshot_error(page, f"redfruit_preflight_order_title_missing_{plan.order_id}")
            raise RuntimeError(
                redfruit_format_preflight_failure([
                    f"工单【{plan.order_id}】未能识别出订单名称，无法继续校验！！",
                ])
            )
        if not order_kind:
            await self._snapshot_error(page, f"redfruit_preflight_order_kind_missing_{plan.order_id}")
            raise RuntimeError(
                redfruit_format_preflight_failure([
                    f"工单【{plan.order_id}】名称【{order_title}】未识别到动态漫/仿真人/纯短剧分类，无法继续！！",
                ])
            )
        if unknown_kind_files:
            await self._snapshot_error(page, f"redfruit_preflight_item_kind_missing_{plan.order_id}")
            preview = "、".join(unknown_kind_files[:5])
            suffix = f"等 {len(unknown_kind_files)} 个文件" if len(unknown_kind_files) > 5 else ""
            raise RuntimeError(
                redfruit_format_preflight_failure([
                    f"文件名未识别到明确的动态漫/仿真人/纯短剧类型：{preview}{suffix}，无法继续！！",
                ])
            )
        if len(item_order_kinds) > 1:
            await self._snapshot_error(page, f"redfruit_preflight_mixed_item_kinds_{plan.order_id}")
            raise RuntimeError(
                redfruit_format_preflight_failure([
                    f"同一批素材里检测到多个应使用工单类型：{'、'.join(sorted(item_order_kinds))}，请先拆批！！",
                ])
            )
        if len(item_drama_kinds) > 1:
            await self._snapshot_error(page, f"redfruit_preflight_mixed_drama_kinds_{plan.order_id}")
            raise RuntimeError(
                redfruit_format_preflight_failure([
                    f"同一批素材里检测到多个剧目类型：{'、'.join(sorted(item_drama_kinds))}，请先拆批！！",
                ])
            )
        item_kind = next(iter(item_drama_kinds), "")
        expected_order_kind = next(iter(item_order_kinds), "")
        errors: list[str] = []
        warnings: list[str] = []
        if expected_order_kind and expected_order_kind != order_kind:
            errors.append(
                f"**用户指定订单与这批素材不符！！** 用户指定订单【{plan.order_id}】名称【{order_title}】应归类为【{order_kind}】，"
                f"但这批文件名识别应使用【{expected_order_kind}】工单，剧目分类为【{item_kind or '未识别'}】！！"
            )
        elif not expected_order_kind:
            errors.append("文件名未识别到明确的动态漫/仿真人/纯短剧工单类型，无法继续！！")
        if not item_titles:
            errors.append("未读取到任何剧名，无法执行红果前置校验！！")
        self._emit(
            progress,
            f"红果前置校验：工单【{plan.order_id}】=【{order_title}】；文件分类=【{item_kind or '未识别'}】；"
            f"应使用工单=【{expected_order_kind or '未识别'}】；工单分类=【{order_kind}】；剧目数={len(item_titles)}",
        )

        playlet_page = await page.context.new_page()
        self._wrap_page_speed(playlet_page)
        playlet_page.set_default_timeout(self.timeout_ms)
        playlet_page.set_default_navigation_timeout(self.timeout_ms)
        try:
            await self._safe_goto(playlet_page, REDFRUIT_PLAYLET_URL)
            search_input = await self._wait_first_existing(
                playlet_page,
                ("input[placeholder*='短剧名']", "input[placeholder*='搜索']"),
                timeout_ms=30000,
            )
            if not search_input:
                errors.append("短剧选剧页未找到剧名搜索框！！")
            for title in item_titles:
                if not search_input:
                    break
                title_error_count = len(errors)
                await self._type_into_locator(search_input, playlet_page, title)
                await playlet_page.keyboard.press("Enter")
                card = {}
                card_body = ""
                for _ in range(12):
                    await playlet_page.wait_for_timeout(1000)
                    card_body = await self._body_text(playlet_page, timeout_ms=5000)
                    card = redfruit_extract_playlet_card(card_body, title)
                    if card.get("found") == "true":
                        break
                if card.get("found") != "true":
                    errors.append(f"墨攻短剧选剧中未找到剧名【{title}】！！")
                    continue
                card_title = str(card.get("title") or "").strip()
                card_label = str(card.get("title_label") or "").strip()
                card_kind = str(card.get("title_kind") or "").strip()
                card_bid = str(card.get("bid") or "").strip()
                expected_item = next(
                    (
                        item for item in items
                        if str(item.workflow_metadata.get("drama_title") or item.song_name or Path(item.file_name).stem).strip() == title
                    ),
                    items[0],
                )
                expected_bid = str(expected_item.workflow_metadata.get("bid") or expected_item.song_id or "").strip()
                expected_kind = redfruit_content_kind(expected_item.workflow_metadata.get("drama_type") or expected_item.file_name)
                if card_title and card_title != title:
                    errors.append(f"剧名搜索命中结果不一致：文件名剧名【{title}】，墨攻结果【{card_title}】！！")
                if expected_bid:
                    if not card_bid:
                        if await self._click_redfruit_card_id_button(playlet_page, title):
                            for _ in range(6):
                                await playlet_page.wait_for_timeout(800)
                                card_body = await self._body_text(playlet_page, timeout_ms=5000)
                                card = redfruit_extract_playlet_card(card_body, title)
                                card_bid = str(card.get("bid") or "").strip()
                                if card_bid:
                                    break
                    if not card_bid:
                        errors.append(f"剧名【{title}】未读取到 BID，无法校验员工提供的 BID！！")
                    elif expected_bid != card_bid:
                        errors.append(
                            f"剧名【{title}】的 BID 不一致：文件/映射期望【{expected_bid}】，墨攻命中【{card_bid}】！！"
                        )
                if card_kind and expected_kind and card_kind != expected_kind:
                    errors.append(
                        f"剧名【{title}】的短剧标签不一致：文件名类型【{expected_kind}】，墨攻标签【{card_label or card_kind}】！！"
                    )
                if card_kind and order_kind in {"动态漫", "仿真人", "纯短剧"} and card_kind != order_kind:
                    errors.append(
                        f"**用户指定订单与墨攻短剧选剧不符！！** 用户指定订单【{plan.order_id}】分类为【{order_kind}】，"
                        f"剧名【{title}】在墨攻短剧选剧中的标签为【{card_label or card_kind}】！！"
                    )
                elif not card_kind:
                    warnings.append(f"剧名【{title}】的墨攻剧名标签未更新或未识别，已仅保留工单分类对照！！")
                status_text = "通过" if len(errors) == title_error_count else "发现异常"
                self._emit(
                    progress,
                    f"红果前置校验{status_text}：剧名【{title}】；墨攻标签【{card_label or '未识别'}】；BID【{card_bid or '未读取'}】",
                )
                await playlet_page.wait_for_timeout(800)
            if errors:
                await self._snapshot_error(playlet_page, f"redfruit_preflight_failed_{plan.order_id}")
                raise RuntimeError(redfruit_format_preflight_failure(errors, warnings))
            if warnings:
                self._emit(progress, "红果前置校验提醒：" + "；".join(warnings))
        finally:
            try:
                await self._close_page_intentionally(playlet_page)
            except Exception:
                pass

    async def _click_redfruit_card_id_button(self, page, drama_title: str) -> bool:
        """点击红果短剧卡片右侧的 ID 按钮，尽量让 BID 展开到正文里。"""
        title_locator = page.get_by_text(drama_title, exact=True).first
        try:
            title_box = await title_locator.bounding_box(timeout=5000)
        except Exception:
            title_box = None
        id_locators = page.get_by_text("ID", exact=True)
        try:
            count = await id_locators.count()
        except Exception:
            count = 0
        candidates = []
        for index in range(count):
            locator = id_locators.nth(index)
            try:
                if not await locator.is_visible():
                    continue
                box = await locator.bounding_box(timeout=2000)
                if not box:
                    continue
                score = 0.0
                if title_box:
                    score = abs((box["y"] + box["height"] / 2) - (title_box["y"] + title_box["height"] / 2))
                candidates.append((score, locator))
            except Exception:
                continue
        for _, locator in sorted(candidates, key=lambda item: item[0]):
            try:
                await locator.click(timeout=5000)
                return True
            except Exception:
                continue
        try:
            if count:
                await id_locators.first.click(timeout=5000)
                return True
        except Exception:
            pass
        return False

    async def _order_visible(self, page, order_id: str) -> bool:
        """判断当前页面文本中是否能看到订单 ID。"""
        body = await self._body_text(page)
        return order_id in body

    async def _open_create_creative_unit(self, page, order_id: str):
        """在订单搜索结果中点击"新建创意单元"，并返回实际进入的上传页面。"""
        await self._snapshot(page, f"order_{order_id}_before_create")

        async def try_open(attempt: int):
            for clicker in (self._click_create_button_for_order, self._click_create_button_by_coordinates):
                before_pages = list(page.context.pages)
                old_url = page.url
                if not await clicker(page, order_id):
                    continue
                target_page = await self._wait_create_page_after_click(
                    page,
                    before_pages,
                    old_url,
                    order_id=order_id,
                )
                if target_page:
                    await target_page.wait_for_timeout(4000)
                    return target_page
                blocked_message = await self._detect_create_creative_unit_blocked(page, order_id)
                if blocked_message:
                    await self._snapshot_error(
                        page,
                        f"order_{order_id}_create_blocked_{attempt}",
                        extra=blocked_message,
                    )
                    raise UserGrowthFatalPageError(blocked_message)
            await self._snapshot_error(page, f"order_{order_id}_create_click_no_effect_{attempt}")
            return False

        attempt = 0
        retry_delay_seconds = 2.0
        while True:
            self._raise_if_cancelled()
            attempt += 1
            try:
                if await self._looks_like_upload_page(page):
                    return page
                target_page = await try_open(attempt)
                if target_page:
                    return target_page
            except UserGrowthFatalPageError as exc:
                await self._snapshot_error(
                    page,
                    f"order_{order_id}_create_blocked",
                    exc=exc,
                    extra=str(exc),
                )
                raise RuntimeError(str(exc)) from exc
            except Exception as exc:
                if self._is_recoverable_session_exception(exc):
                    raise
                if attempt == 1 or attempt % 5 == 0:
                    self._write_run_log(
                        f"[{datetime.now().isoformat(timespec='seconds')}] create creative unit retry: "
                        f"order_id={order_id}, attempt={attempt}, error={type(exc).__name__}: {exc}"
                    )
            if attempt == 1 or attempt % 5 == 0:
                self._emit(
                    None,
                    f"订单 {order_id} 新建创意单元入口仍在加载，保持浏览器打开继续等待（第 {attempt} 次）",
                )
            await self._sleep(retry_delay_seconds)
            retry_delay_seconds = min(retry_delay_seconds * 2, 30.0)

    async def _detect_create_creative_unit_blocked(
            self,
            page,
            order_id: str,
            *,
            timeout_ms: int = 3000,
    ) -> str:
        """识别点击新建创意单元后平台弹出的不可继续原因。"""
        body = await self._body_text(page, timeout_ms=timeout_ms)
        block_messages = (
            "已超出当前订单的交付截止时间",
            "当前订单已超出交付截止时间",
            "订单已截止",
            "订单已结束",
            "当前订单不可新建创意单元",
        )
        for message in block_messages:
            if message in body:
                return f"订单 {order_id} {message}，无法新建创意单元"
        return ""

    async def _wait_create_page_after_click(
            self,
            page,
            before_pages: list,
            old_url: str,
            *,
            order_id: str | None = None,
    ):
        """点击创建入口后等待当前页或新标签页进入上传页。"""
        before_ids = {id(p) for p in before_pages}
        deadline = asyncio.get_event_loop().time() + 25

        while asyncio.get_event_loop().time() < deadline:
            for candidate in reversed(page.context.pages):
                if candidate.is_closed():
                    continue
                try:
                    await candidate.wait_for_load_state("domcontentloaded", timeout=1000)
                except Exception:
                    pass
                # 命中条件：是上传页，或者是一个新开的、非占位 URL 的标签页
                is_upload = await self._looks_like_upload_page(candidate)
                is_new = (
                        id(candidate) not in before_ids
                        and candidate.url not in {"about:blank", old_url}
                )
                if is_upload or is_new:
                    try:
                        await candidate.bring_to_front()
                    except Exception:
                        pass
                    self._wrap_page_speed(candidate)
                    return candidate
            # 当前页已经离开工单管理页（点中了跳转）
            if page.url != old_url and not await self._is_work_order_page(page):
                self._wrap_page_speed(page)
                return page
            if order_id:
                blocked_message = await self._detect_create_creative_unit_blocked(
                    page,
                    order_id,
                    timeout_ms=1000,
                )
                if blocked_message:
                    raise UserGrowthFatalPageError(blocked_message)
            await page.wait_for_timeout(800)
        return None

    async def _looks_like_upload_page(self, page) -> bool:
        """判断页面是否已经进入上传创意单元页面。"""
        if await self._first_attached(page, ("input[type='file']",)):
            return True
        body = await self._body_text(page, timeout_ms=2000)
        return any(text in body for text in ("点击或拖拽", "文件上传", "温馨提示"))

    async def _click_create_button_for_order(self, page, order_id: str) -> bool:
        """优先在包含订单 ID 的结果区域中点击“新建创意单元”。"""
        scope = await self._order_result_scope(page, order_id)
        if scope and await self._click_create_button_in_scope(scope):
            return True
        if await self._order_visible(page, order_id):
            exact_buttons = page.get_by_text("新建创意单元", exact=True)
            if await self._click_single_visible_locator(exact_buttons):
                return True
        return await self._click_create_button_near_order(page, order_id)

    async def _click_create_button_in_scope(self, scope) -> bool:
        """在指定 DOM 区域内寻找并点击"新建创意单元"。"""
        for locator in (
                scope.locator("button.ant-btn-link:has-text('新建创意单元')"),
                scope.get_by_text("新建创意单元", exact=True),
        ):
            try:
                button = locator.first
                if await button.count() and await button.is_visible():
                    await button.scroll_into_view_if_needed(timeout=3000)
                    await button.click(force=True)
                    return True
            except Exception:
                continue
        return False

    async def _find_closest_create_button(self, page, order_id: str):
        """找到距离订单 ID 垂直方向最近的"新建创意单元"按钮；找不到返回 None。"""
        # 仅在订单文本垂直方向 90 像素内搜索按钮，避免误点页面其它位置的同名按钮。
        max_distance = 90
        order_box = await self._first_text_box(page, order_id)
        if not order_box:
            return None
        order_y = order_box["y"] + order_box["height"] / 2

        candidates = page.locator(
            "button.ant-btn-link:has-text('新建创意单元'), "
            "a:has-text('新建创意单元'), "
            "[role='button']:has-text('新建创意单元')"
        )

        best, best_distance = None, max_distance
        for index in range(min(await candidates.count(), 30)):
            try:
                button = candidates.nth(index)
                if not await button.is_visible():
                    continue
                box = await button.bounding_box(timeout=3000)
                if not box:
                    continue
                distance = abs(box["y"] + box["height"] / 2 - order_y)
                if distance < best_distance:
                    best, best_distance = button, distance
            except Exception:
                continue
        return best

    async def _click_create_button_near_order(self, page, order_id: str) -> bool:
        """用 Playwright 点击订单附近的"新建创意单元"。"""
        button = await self._find_closest_create_button(page, order_id)
        return bool(button) and await self._click_locator(button)

    async def _click_create_button_by_coordinates(self, page, order_id: str) -> bool:
        """用真实鼠标点击，作为 Playwright 点击失败的兜底。"""
        button = await self._find_closest_create_button(page, order_id)
        return bool(button) and await self._click_locator_center(page, button)

    async def _order_result_scope(self, page, order_id: str):
        """寻找同时包含订单 ID 和操作入口的订单结果区域。"""
        order_literal = self._xpath_literal(order_id)
        for text_locator in (
                page.get_by_text(order_id, exact=True).first,
                page.get_by_text(order_id, exact=False).first,
        ):
            for xpath in (
                    "xpath=ancestor::*[contains(., '新建创意单元')][1]",
                    f"xpath=ancestor::*[contains(., {order_literal}) and contains(., '新建创意单元')][1]",
                    f"xpath=ancestor::*[contains(., {order_literal}) and contains(., '查看创意单元')][1]",
            ):
                try:
                    scope = text_locator.locator(xpath)
                    if await scope.count() and await scope.is_visible():
                        return scope
                except Exception:
                    continue
        return None

    async def _click_single_visible_locator(self, locators) -> bool:
        """当匹配结果只有一个可见元素时点击它。"""
        visible = await self._visible_locators(locators)
        if len(visible) != 1:
            return False
        return await self._click_locator(visible[0])

    async def _first_text_box(self, page, text: str):
        """返回页面上某段文本的第一个可见位置。"""
        for locator in (
                page.get_by_text(text, exact=True).first,
                page.get_by_text(text, exact=False).first,
        ):
            try:
                if await locator.count() and await locator.is_visible():
                    box = await locator.bounding_box(timeout=3000)
                    if box:
                        return box
            except Exception:
                continue
        return None

    @staticmethod
    def _xpath_literal(value: str) -> str:
        """把普通字符串转换成 XPath contains 可安全使用的字面量。"""
        if "'" not in value:
            return f"'{value}'"
        if '"' not in value:
            return f'"{value}"'
        parts = value.split("'")
        return "concat(" + ", \"'\", ".join(f"'{part}'" for part in parts) + ")"

    async def _read_upload_limit(self, page) -> int | None:
        """从上传页温馨提示中读取最多可上传创意单元数量。"""
        try:
            body = await page.locator("body").inner_text(timeout=5000)
        except Exception:
            return None
        # 三种文案兜底：带"上传"、带"个"、带"上限"，相互不重叠，按出现概率从高到低排序。
        patterns = (
            r"最多(?:可以|可)?上传[^0-9]{0,30}(\d+)",
            r"最多\s*(\d+)\s*个",
            r"上限\s*(\d+)",
        )
        for pattern in patterns:
            match = re.search(pattern, body)
            if match:
                return int(match.group(1))
        return None

    async def _upload_files(
            self,
            page,
            items: list[UserGrowthVideoItem],
            order_id: str | None = None,
    ):
        """上传视频文件，可恢复失败持续重试到成功或用户取消。

        文件上传 input 会持续等待，找不到时周期性点击上传入口。
        上传页白屏、控件未初始化、临时上限 0 或点击无响应时不按次数结束。

        失败时如果传入了 order_id，会刷新当前页并从"新建创意单元"入口
        重新走流程再进行下一次重试，避免"点击或拖拽"被点击后服务端没
        真正开始上传而留下的半成品状态。
        """

        current_page = page

        async def attempt(_attempt: int) -> bool:
            nonlocal current_page
            try:
                await self._snapshot(current_page, f"before_upload_{_attempt}")
                file_input = await self._wait_upload_page_ready(current_page)
                # 设置文件
                await file_input.set_input_files(
                    [str(item.path) for item in items]
                )
                # 触发真正上传
                await self._click_if_present(
                    current_page,
                    "点我开始上传"
                )
                await current_page.wait_for_timeout(3000)
                # 检查上传是否异常
                body = await self._body_text(current_page)
                if self._has_upload_limit_zero_error(body):
                    raise RuntimeError("当前选择文件数量超过订单创意单元上限: 0")
                if "上传失败" in body or "上传异常" in body:
                    raise RuntimeError(
                        "页面提示上传失败"
                    )
                return True

            except Exception as exc:
                if isinstance(exc, (UserGrowthCancelled, UserGrowthFatalPageError)):
                    raise
                if self._is_recoverable_session_exception(exc):
                    raise
                await self._snapshot_error(
                    current_page,
                    f"upload_failed_retry_{_attempt}",
                    exc=exc,
                )
                print(f"[upload] 第 {_attempt} 次上传失败: {exc}")
                if "当前选择文件数量超过订单创意单元上限: 0" in str(exc):
                    self._write_run_log(
                        f"[{datetime.now().isoformat(timespec='seconds')}] upload limit zero wait: "
                        f"attempt={_attempt}, keep_current_dialog=true"
                    )
                    await current_page.wait_for_timeout(3000)
                    return False
                # 刷新页面 + 从「新建创意单元」入口重新走流程，
                # 避免「点击或拖拽」被点击后服务端没真正开始上传的半成品状态
                if order_id:
                    try:
                        try:
                            await current_page.reload(
                                wait_until="domcontentloaded"
                            )
                            await current_page.wait_for_timeout(2000)
                        except Exception:
                            pass
                        new_page = await self._open_create_creative_unit(
                            current_page,
                            order_id,
                        )
                        if new_page:
                            current_page = new_page
                    except Exception as reset_exc:
                        print(
                            f"[upload] 重新走新建创意单元失败: {reset_exc}"
                        )
                return False

        retry_number = 0
        retry_delay_seconds = 2.0
        while True:
            self._raise_if_cancelled()
            retry_number += 1
            if await attempt(retry_number):
                break
            if retry_number == 1 or retry_number % 5 == 0:
                self._emit(
                    None,
                    f"文件上传尚未成功，保持浏览器打开继续恢复（第 {retry_number} 次）",
                )
            await self._sleep(retry_delay_seconds)
            retry_delay_seconds = min(retry_delay_seconds * 2, 30.0)
        return current_page

    @staticmethod
    def _has_upload_limit_zero_error(body: str) -> bool:
        """识别上传后页面提示的创意单元上限为 0 异常。"""
        compact_body = _compact_text(body)
        return (
                "当前选择文件数量超过订单创意单元上限" in body
                or "当前选择文件数量超过订单创意单元上限:0" in compact_body
                or "订单创意单元上限:0" in compact_body
        )

    async def _wait_file_input(self, page, timeout_ms: int = 20000):
        """等待隐藏或可见的文件上传 input 出现在页面上。"""

        async def find_file_input():
            return await self._first_attached(page, ("input[type='file']",))

        return await self._wait_for_result(find_file_input, timeout_ms=timeout_ms, interval_ms=800)

    async def _enter_chameleon(
            self,
            page,
            items: list[UserGrowthVideoItem],
            progress: ProgressCallback | None = None,
            plan: UserGrowthOrderPlan | None = None,
    ):
        """提交上传后的创意单元列表，并进入录入流程。"""

        def _log(msg: str) -> None:
            print(f"[enter-chameleon] {msg}", flush=True)

        self._emit(progress, f"等待 {len(items)} 个视频生成待提交卡片")
        await self._wait_upload_cards_ready(page, items, progress, plan=plan)
        if not items:
            self._emit(progress, "当前批次全部命中已存在创意单元，跳过新建创意单元录入")
            return page
        await self._click_if_present(page, "继续编辑")
        await page.wait_for_timeout(2000)
        # "确认提交"点击后可能打开新标签页，也可能当前页跳转；不设总超时等待目标页出现。
        new_page = await self._click_text_and_wait_page(page, "确认提交", timeout_ms=None)
        self._wrap_page_speed(new_page)
        _log(f"new tab opened, url={new_page.url}")
        # 切到新标签页
        page = new_page
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=0)
        except Exception as exc:
            await self._snapshot_error(page, "new_tab_load_failed", exc=exc)
            raise RuntimeError(f"新标签页加载失败: {exc}, url={page.url}")
        _log(f"new tab loaded, url={page.url}")
        await self._wait_creative_unit_table_ready(page, timeout_ms=None)
        await self._select_creative_units_for_items(page, items)
        # "录入素材"点击后可能打开新标签页，也可能当前页跳转；不设总超时等待目标页出现。
        entry_page = await self._click_text_and_wait_page(page, "录入素材", timeout_ms=None)
        self._wrap_page_speed(entry_page)
        page = entry_page
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=0)
        except Exception as exc:
            await self._snapshot_error(page, "enter_chameleon_load_failed", exc=exc)
            raise RuntimeError(f"chameleon 录入页加载失败: {exc}, url={page.url}")
        # 等 chameleon 内容渲染
        while True:
            if await self._looks_like_chameleon_entry_page(page):
                _log(f"chameleon entry page ready, url={page.url}")
                break
            await page.wait_for_timeout(500)
        await self._snapshot(page, "after_enter_chameleon")
        await self._ensure_chameleon_modal(page, items[0])
        await self._click_chameleon_modal_confirm(page)
        await self._wait_chameleon_card_forms_ready(page, items, progress)
        # todo 是否每次上传都是同一批的，如果是的话就不用循环了
        task_page = await self._fill_card_defaults(page, items[0])
        return task_page or page

    async def _wait_chameleon_entry_page_after_click(self, page, before_pages: list, old_url: str):
        """点击录入素材后，等待跳转后的新标签页出现。"""
        before_ids = {id(p) for p in before_pages}
        deadline = asyncio.get_event_loop().time() + 45
        while asyncio.get_event_loop().time() < deadline:
            for candidate in reversed(page.context.pages):
                if candidate.is_closed():
                    continue
                try:
                    await candidate.wait_for_load_state("domcontentloaded", timeout=1000)
                except Exception:
                    pass
                # 是新开的非占位页
                is_chameleon = await self._looks_like_chameleon_entry_page(candidate)
                is_new = (
                        id(candidate) not in before_ids
                        and candidate.url not in {"about:blank", old_url}
                )
                if is_chameleon or is_new:
                    try:
                        await candidate.bring_to_front()
                    except Exception:
                        pass
                    return candidate
            await self._sleep(1)
        await self._snapshot_error(page, "enter_chameleon_page_timeout")
        raise RuntimeError("点击录入后未进入录入页面")

    async def _looks_like_chameleon_entry_page(self, page) -> bool:
        """判断页面是否是录入变色龙后的投放信息确认页。"""
        try:
            if await self._first_existing(
                    page,
                    (
                        "input[placeholder*='任务ID']",
                        "input[placeholder*='任务']",
                    ),
            ):
                return True
        except Exception:
            pass
        body = await self._body_text(page, timeout_ms=2000)
        if not body.strip():
            return False
        return "投放平台" in body and any(text in body for text in ("投放产品", "汽水音乐", "红果免费短剧", "红果免费漫剧"))

    async def _wait_upload_cards_ready(
            self,
            page,
            items: list[UserGrowthVideoItem],
            progress: ProgressCallback | None = None,
            plan: UserGrowthOrderPlan | None = None,
    ) -> None:
        """等待上传卡片可继续提交；遇到可回收的失败项时先剔除再继续等待。"""
        while True:
            try:
                body = await self._body_text(page, timeout_ms=3000)
            except Exception:
                body = ""

            if self._has_upload_limit_zero_error(body):
                await self._snapshot_error(page, "upload_limit_zero_before_cards_ready")
                raise UserGrowthUploadPageRetry("当前选择文件数量超过订单创意单元上限: 0")

            try:
                # 查找页面上所有的 Arco 复选框/勾选图标
                success_icons = page.locator("span.arco-upload-list-success-icon")

                # 统计可见数量
                visible_icons = await self._visible_locators(success_icons, limit=100)
                s_count = len(visible_icons)

                # 数量相等即视为上传成功
                if s_count >= len(items):
                    await self._snapshot(page, "upload_cards_ready")
                    return

            except Exception:
                # 统计过程中发生任何异常（比如页面还没加载出 DOM），忽略，继续下一轮
                pass

            handled = await self._recover_failed_upload_cards(page, items, progress, plan=plan)
            if handled:
                if not items:
                    await self._snapshot(page, "upload_cards_ready_all_reused")
                    return
                await page.wait_for_timeout(800)
                continue

            # 轮询间隔，避免 CPU 飙高
            await page.wait_for_timeout(1000)

    async def _recover_failed_upload_cards(
            self,
            page,
            items: list[UserGrowthVideoItem],
            progress: ProgressCallback | None = None,
            plan: UserGrowthOrderPlan | None = None,
    ) -> int:
        """回收上传弹窗中可处理的失败素材行；返回已处理数量。"""
        handled = 0
        failed_rows = await self._visible_failed_upload_card_rows(page, items)
        for item, row in failed_rows:
            reason = await self._read_upload_card_failure_reason(page, row)
            metadata = item.workflow_metadata if isinstance(item.workflow_metadata, dict) else {}
            if not reason:
                wait_count = int(metadata.get("upload_failure_reason_wait_count") or 0) + 1
                metadata["upload_failure_reason_wait_count"] = wait_count
                item.workflow_metadata = metadata
                if wait_count == 1 or wait_count % 10 == 0:
                    await self._snapshot_error(
                        page,
                        "upload_failed_reason_missing",
                        extra=f"file={item.file_name}, wait_count={wait_count}",
                    )
                    self._emit(progress, f"素材【{item.file_name}】失败原因仍在加载，继续等待对应素材行")
                continue
            metadata.pop("upload_failure_reason_wait_count", None)
            unit_id, material_id = self._parse_existing_creative_unit_failure(reason)
            if not unit_id:
                if "曾经被上传" in reason:
                    wait_count = int(metadata.get("upload_duplicate_parse_wait_count") or 0) + 1
                    metadata["upload_duplicate_parse_wait_count"] = wait_count
                    item.workflow_metadata = metadata
                    if wait_count == 1 or wait_count % 10 == 0:
                        await self._snapshot_error(
                            page,
                            "upload_duplicate_reason_parse_failed",
                            extra=f"file={item.file_name}, wait_count={wait_count}\nreason={reason}",
                        )
                        self._emit(progress, f"素材【{item.file_name}】重复上传详情未完整显示，继续等待原创意单元 ID")
                    continue
                if await self._upload_card_has_retry_action(row):
                    try:
                        retry_count = int(metadata.get("upload_retry_count") or 0)
                    except (TypeError, ValueError):
                        retry_count = 0
                    if retry_count == 0 and metadata.get("upload_retry_clicked"):
                        retry_count = 1
                    if retry_count >= UPLOAD_ROW_RETRY_LIMIT:
                        await self._snapshot_error(
                            page,
                            "upload_retry_exhausted",
                            extra=(
                                f"file={item.file_name}, retry_count={retry_count}, "
                                f"retry_limit={UPLOAD_ROW_RETRY_LIMIT}\nreason={reason}"
                            ),
                        )
                        raise UserGrowthUploadRowFailed(
                            f"素材【{item.file_name}】行内重试 {UPLOAD_ROW_RETRY_LIMIT} 次后仍未恢复：{reason}"
                        )
                    if await self._click_upload_card_retry(row):
                        retry_count += 1
                        metadata["upload_retry_clicked"] = True
                        metadata["upload_retry_count"] = retry_count
                        metadata["upload_retry_clicked_at"] = datetime.now().isoformat(timespec="seconds")
                        item.workflow_metadata = metadata
                        self._emit(
                            progress,
                            f"素材【{item.file_name}】出现行内点击重试，已执行第 {retry_count}/{UPLOAD_ROW_RETRY_LIMIT} 次",
                        )
                        self._write_run_log(
                            f"[{datetime.now().isoformat(timespec='seconds')}] upload retry clicked "
                            f"file={item.file_name}, retry_count={retry_count}, reason={reason}"
                        )
                        self._write_event(
                            "upload_row_retry_clicked",
                            order_id=item.order_id,
                            file_name=item.file_name,
                            retry_count=retry_count,
                            retry_limit=UPLOAD_ROW_RETRY_LIMIT,
                            reason=reason,
                        )
                        if plan is not None:
                            self._checkpoint(
                                plan,
                                "upload_processing",
                                f"已对失败素材 {item.file_name} 执行第 {retry_count}/{UPLOAD_ROW_RETRY_LIMIT} 次行内点击重试",
                            )
                        handled += 1
                        continue
                    click_wait_count = int(metadata.get("upload_retry_click_wait_count") or 0) + 1
                    metadata["upload_retry_click_wait_count"] = click_wait_count
                    item.workflow_metadata = metadata
                    if click_wait_count == 1 or click_wait_count % 10 == 0:
                        await self._snapshot_error(
                            page,
                            "upload_retry_click_failed",
                            extra=f"file={item.file_name}, wait_count={click_wait_count}\nreason={reason}",
                        )
                        self._emit(progress, f"素材【{item.file_name}】重试按钮暂时不可点，继续等待该素材行")
                    continue
                if self._looks_like_upload_card_failure(reason):
                    action_wait_count = int(metadata.get("upload_failure_action_wait_count") or 0) + 1
                    metadata["upload_failure_action_wait_count"] = action_wait_count
                    item.workflow_metadata = metadata
                    if action_wait_count == 1 or action_wait_count % 10 == 0:
                        await self._snapshot_error(
                            page,
                            "upload_failed_waiting_for_action",
                            extra=f"file={item.file_name}, wait_count={action_wait_count}\nreason={reason}",
                        )
                        self._emit(progress, f"素材【{item.file_name}】已显示失败，等待该行出现可重试或可恢复信息")
                continue

            deleted = await self._delete_upload_card_row(page, row)
            if not deleted:
                delete_wait_count = int(metadata.get("upload_duplicate_delete_wait_count") or 0) + 1
                metadata["upload_duplicate_delete_wait_count"] = delete_wait_count
                item.workflow_metadata = metadata
                if delete_wait_count == 1 or delete_wait_count % 10 == 0:
                    await self._snapshot_error(
                        page,
                        "upload_failed_card_delete_failed",
                        extra=f"file={item.file_name}, wait_count={delete_wait_count}\nreason={reason}",
                    )
                    self._emit(progress, f"素材【{item.file_name}】重复上传行暂时无法删除，继续等待该行可操作")
                continue

            metadata["existing_creative_unit_id"] = unit_id
            if material_id:
                metadata["existing_material_id"] = material_id
            metadata["upload_failure_reason"] = reason
            item.workflow_metadata = metadata
            item.status = "deferred_existing_creative_unit"
            item.message = f"上传检测重复，后续按原创意单元 {unit_id} 补录"
            items.remove(item)
            handled += 1
            self._emit(
                progress,
                f"素材【{item.file_name}】曾经上传过，已删除当前失败行，稍后按原创意单元【{unit_id}】补录",
            )
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] upload duplicate recovered "
                f"file={item.file_name}, creative_unit={unit_id}, material={material_id or ''}, reason={reason}"
            )
            self._write_event(
                "upload_duplicate_deferred",
                order_id=item.order_id,
                file_name=item.file_name,
                creative_unit_id=unit_id,
                material_id=material_id or "",
                reason=reason,
            )
            if plan is not None:
                self._checkpoint(
                    plan,
                    "upload_processing",
                    f"已记录重复素材 {item.file_name} 对应原创意单元 {unit_id}，等待继续处理上传素材",
                )
        return handled

    async def _visible_failed_upload_card_rows(
            self,
            page,
            items: list[UserGrowthVideoItem],
    ) -> list[tuple[UserGrowthVideoItem, object]]:
        """只返回已经显式失败的可见上传行，避免轮询时逐行 hover 或误点。"""
        failed_rows: list[tuple[UserGrowthVideoItem, object]] = []
        for item in list(items):
            row = await self._upload_card_row_for_item(page, item)
            if row is None:
                continue
            if await self._upload_card_has_failure_signal(row):
                failed_rows.append((item, row))
        return failed_rows

    async def _upload_card_row_for_item(self, page, item: UserGrowthVideoItem):
        """按文件名找到上传弹窗中的单个文件行。"""
        file_name = str(item.file_name or "")
        if not file_name:
            return None
        selectors = (
            ".arco-upload-list-item",
            "[class*='upload-list-item']",
            "[class*='upload-item']",
            "[class*='upload-list'] li",
            "[role='listitem']",
        )
        for selector in selectors:
            try:
                rows = page.locator(selector).filter(has_text=file_name)
                visible = await self._visible_locators(rows, limit=20)
                if visible:
                    return visible[0]
            except Exception:
                continue
        text_locator = page.get_by_text(file_name, exact=True).first
        for xpath in (
                "xpath=ancestor::*[contains(@class, 'arco-upload-list-item')][1]",
                "xpath=ancestor::*[contains(@class, 'upload-list-item')][1]",
                "xpath=ancestor::*[contains(@class, 'upload-item')][1]",
                "xpath=ancestor::*[self::li or @role='listitem'][1]",
        ):
            try:
                row = text_locator.locator(xpath)
                if await row.count() and await row.is_visible():
                    return row
            except Exception:
                continue
        return None

    async def _wait_upload_page_ready(self, page):
        """等待上传控件和正数额度稳定，避免组件初始上限 0 时过早选文件。"""
        attempt = 0
        while True:
            self._raise_if_cancelled()
            attempt += 1
            file_input = await self._wait_file_input(page, timeout_ms=5000)
            body = await self._body_text(page, timeout_ms=3000)
            limit = await self._read_upload_limit(page)
            has_limit_hint = any(
                marker in body
                for marker in ("最多可上传", "最多可以上传", "最多上传", "创意单元上限")
            )
            quota_ready = limit is not None and limit > 0
            quota_not_exposed = limit is None and not has_limit_hint
            if file_input and (quota_ready or quota_not_exposed):
                await page.wait_for_timeout(2500)
                confirmed_input = await self._wait_file_input(page, timeout_ms=5000)
                confirmed_limit = await self._read_upload_limit(page)
                if confirmed_input and (
                        confirmed_limit is not None and confirmed_limit > 0
                        or confirmed_limit is None and quota_not_exposed
                ):
                    return confirmed_input
            if attempt == 1 or attempt % 10 == 0:
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] upload page initialization wait: "
                    f"attempt={attempt}, file_input={bool(file_input)}, limit={limit}"
                )
                self._emit(
                    None,
                    f"上传组件仍在初始化，等待正数额度和文件控件就绪（第 {attempt} 次）",
                )
            await page.wait_for_timeout(1000)

    async def _read_upload_card_failure_reason(self, page, row) -> str:
        """读取上传失败行的 tooltip/行文案，返回可读失败原因。"""
        texts: list[str] = []
        row_text = await self._locator_text(row, timeout_ms=1500)
        if row_text:
            texts.append(row_text)
        try:
            row_html = await row.evaluate("(node) => node.outerHTML")
            if row_html:
                texts.append(row_html)
        except Exception:
            pass
        try:
            attr_texts = await row.evaluate(
                """(node) => {
                    const attrs = ['title', 'aria-label', 'data-title', 'data-tooltip', 'data-tooltip-content', 'data-original-title'];
                    const values = [];
                    const walk = (el) => {
                        if (!el || el.nodeType !== 1) return;
                        for (const attr of attrs) {
                            try {
                                const val = el.getAttribute && el.getAttribute(attr);
                                if (val) values.push(val);
                            } catch (err) {}
                        }
                        for (const child of el.children || []) walk(child);
                    };
                    walk(node);
                    return values;
                }"""
            )
            if attr_texts:
                texts.extend(str(text) for text in attr_texts if text)
        except Exception:
            pass

        hover_targets = (
            ".arco-upload-list-error-icon",
            ".arco-icon-exclamation-circle-fill",
            ".arco-icon-exclamation-circle",
            ".arco-icon-close-circle-fill",
            "[class*='error-icon']",
            "[class*='fail-icon']",
            "[class*='error'] [role='img']",
            "[class*='fail'] [role='img']",
        )
        for selector in hover_targets:
            try:
                for target in await self._visible_locators(row.locator(selector), limit=8):
                    try:
                        await target.hover(timeout=2500)
                        await page.wait_for_timeout(250)
                        texts.extend(await self._visible_tooltip_texts(page))
                    except Exception:
                        continue
            except Exception:
                continue

        return self._extract_upload_failure_reason(texts)

    async def _upload_card_has_failure_signal(self, row) -> bool:
        """判断上传行是否真的进入失败/需重试状态；不滚动、不 hover。"""
        row_text = await self._locator_text(row, timeout_ms=800)
        if self._extract_upload_failure_reason((row_text,)):
            return True
        return await self._upload_card_has_retry_action(row) or await self._upload_card_has_error_marker(row)

    async def _upload_card_has_retry_action(self, row) -> bool:
        """判断单个上传行内是否有真正可见的点击重试动作。"""
        for selector in (
                "button:has-text('点击重试')",
                "text=点击重试",
        ):
            try:
                if await self._visible_locators(row.locator(selector), limit=3):
                    return True
            except Exception:
                continue
        return False

    async def _upload_card_has_error_marker(self, row) -> bool:
        """判断单个上传行内是否有明确错误图标。"""
        selectors = (
            ".arco-upload-list-error-icon",
            ".arco-icon-exclamation-circle-fill",
            ".arco-icon-exclamation-circle",
            ".arco-icon-close-circle-fill",
            "[class*='upload-list-error']",
            "[class*='status-error']",
            "[class*='error-icon']",
            "[class*='fail-icon']",
        )
        for selector in selectors:
            try:
                if await self._visible_locators(row.locator(selector), limit=5):
                    return True
            except Exception:
                continue
        return False

    async def _click_upload_card_retry(self, row) -> bool:
        """只点击当前失败上传行里的重试按钮。"""
        for selector in (
                "button:has-text('点击重试')",
                "text=点击重试",
        ):
            try:
                for candidate in await self._visible_locators(row.locator(selector), limit=3):
                    try:
                        await candidate.click(timeout=3000)
                        return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    async def _visible_tooltip_texts(self, page) -> list[str]:
        """读取当前可见 tooltip/浮层文本。"""
        texts: list[str] = []
        roots = page.locator(
            ".arco-tooltip, .arco-trigger-popup, [role='tooltip'], "
            ".ant-tooltip, .ant-popover, [class*='tooltip'], [class*='popover']"
        )
        for root in await self._visible_locators(roots, limit=20):
            text = await self._locator_text(root, timeout_ms=1000)
            if text:
                texts.append(text)
        return texts

    @staticmethod
    def _extract_upload_failure_reason(texts: Iterable[str]) -> str:
        """从若干页面文本里抽取最像上传失败原因的一段。"""
        cleaned_texts = []
        for text in texts:
            cleaned = re.sub(r"[\r\n\t]+", " ", str(text or "")).strip()
            if cleaned:
                cleaned_texts.append(cleaned)
        if not cleaned_texts:
            return ""

        hard_keywords = ("上传检测失败", "该文件曾经被上传")
        for keyword in hard_keywords:
            for cleaned in cleaned_texts:
                index = cleaned.find(keyword)
                if index >= 0:
                    return cleaned[index:index + 360].strip()

        soft_keywords = ("上传失败", "上传异常", "创建失败")
        for keyword in soft_keywords:
            for cleaned in cleaned_texts:
                index = cleaned.find(keyword)
                if index >= 0:
                    return cleaned[index:index + 360].strip()

        for cleaned in cleaned_texts:
            if "点击重试" in cleaned:
                return "点击重试"
        return ""

    @staticmethod
    def _parse_existing_creative_unit_failure(reason: str) -> tuple[str, str]:
        """解析“该文件曾经被上传”类失败中的原创意单元 ID 和素材 ID。"""
        text = str(reason or "")
        if "曾经被上传" not in text:
            return "", ""
        unit_match = re.search(r"创意单元\s*(?:id|ID)\s*[:：]?\s*([A-Za-z0-9_-]{4,})", text)
        material_match = re.search(r"素材\s*(?:id|ID)\s*[:：]?\s*(\d{4,})", text)
        return (
            unit_match.group(1).strip() if unit_match else "",
            material_match.group(1).strip() if material_match else "",
        )

    @staticmethod
    def _looks_like_upload_card_failure(reason: str) -> bool:
        compact = _compact_text(reason)
        return any(keyword in compact for keyword in ("上传检测失败", "上传失败", "上传异常", "创建失败"))

    async def _delete_upload_card_row(self, page, row) -> bool:
        """删除上传弹窗中的单个失败文件行。"""
        selectors = (
            "[aria-label*='删除']",
            "[title*='删除']",
            "button:has-text('删除')",
            ".arco-icon-delete",
            "[class*='delete']",
            "[class*='remove']",
        )
        for selector in selectors:
            try:
                for button in await self._visible_locators(row.locator(selector), limit=10):
                    if await self._click_locator_center(page, button) or await self._click_locator(button):
                        await page.wait_for_timeout(500)
                        await self._click_if_present(page, "确定")
                        await page.wait_for_timeout(500)
                        return True
            except Exception:
                continue

        return False

    async def _wait_before_submit_after_upload(self, page, items: list[UserGrowthVideoItem]) -> None:
        """提交前按视频数量等待平台完成文件预处理。"""
        timeout_ms = self._pre_submit_upload_wait_ms(len(items))
        deadline = None if timeout_ms is None else asyncio.get_event_loop().time() + timeout_ms / 1000
        while deadline is None or asyncio.get_event_loop().time() < deadline:
            body = await self._body_text(page, timeout_ms=3000)
            if "上传失败" in body or "上传异常" in body:
                await self._snapshot_error(page, "upload_failed_before_submit")
                raise RuntimeError("提交前视频上传处理失败")
            await page.wait_for_timeout(3000)
        await self._snapshot(page, "upload_before_submit_wait_done")

    async def _is_creative_unit_list_page(self, page, body: str | None = None) -> bool:
        """判断当前页面是否是创意单元列表页。"""
        body = body if body is not None else await self._body_text(page, timeout_ms=3000)
        if "提交创意单元" in body:
            return True
        return "创意单元" in body and any(text in body for text in ("操作", "单元名称"))

    async def _creative_unit_rows_ready(
            self,
            page,
            body: str | None,
            items: list[UserGrowthVideoItem],
    ) -> bool:
        """确认创意单元列表中已经出现本次上传的所有文件名。"""
        body = body if body is not None else await self._body_text(page, timeout_ms=3000)
        if not await self._is_creative_unit_list_page(page, body):
            return False
        if "暂无数据" in body:
            return False
        return all(item.file_name in body for item in items)

    async def _ensure_chameleon_modal(self, page, item: UserGrowthVideoItem | None = None) -> None:
        """检查录入弹窗里的投放产品和投放平台是否符合预期。"""
        await self._snapshot(page, "chameleon_delivery_before_check")
        expected_fields, platform_all = self._workflow_delivery_expectations(item)
        for field_text, value_text in expected_fields:
            if not await self._ensure_delivery_field_value(page, field_text, value_text):
                await self._snapshot_error(page, f"chameleon_delivery_{field_text}_not_selected")
        if platform_all:
            platform_ok = await self._ensure_delivery_platform_all(page)
        else:
            platform_ok = await self._ensure_delivery_platforms(page, item)
        if not platform_ok:
            await self._snapshot_error(page, "chameleon_delivery_platform_not_selected")

        missing = [
            f"{label}:{value}"
            for label, value in expected_fields
            if not await self._delivery_field_has_value(page, label, value)
        ]
        if not await self._delivery_platform_has_selection(page):
            missing.append("投放平台")
        if missing:
            await self._snapshot_error(page, "chameleon_delivery_check_failed")
            raise RuntimeError(f"录入弹窗内容不符合预期：缺少 {', '.join(missing)}")

    def _workflow_delivery_expectations(self, item: UserGrowthVideoItem | None) -> tuple[tuple[tuple[str, str], ...], bool]:
        """按工作流返回投放产品与平台全选策略。"""
        if self._is_redfruit_item(item):
            metadata = self._workflow_metadata(item)
            products = tuple((("投放产品", value) for value in (metadata.get("delivery_products") or ["红果免费短剧(8662)"])))
            return products, bool(metadata.get("delivery_platform_all"))
        return (("投放产品", "汽水音乐"),), True

    def _is_redfruit_item(self, item: UserGrowthVideoItem | None) -> bool:
        return bool(item and is_redfruit_workflow(getattr(item, "workflow", "")))

    def _is_redfruit_items(self, items: Iterable[UserGrowthVideoItem]) -> bool:
        return any(self._is_redfruit_item(item) for item in items)

    def _workflow_metadata(self, item: UserGrowthVideoItem | None) -> dict:
        if not item:
            return {}
        metadata = getattr(item, "workflow_metadata", None)
        return metadata if isinstance(metadata, dict) else {}

    async def _ensure_delivery_platforms(self, page, item: UserGrowthVideoItem | None = None) -> bool:
        """把投放平台按指定列表逐个选中。"""
        metadata = self._workflow_metadata(item)
        platform_values = list(metadata.get("delivery_platforms") or [])
        if not platform_values:
            return await self._ensure_delivery_platform_all(page)
        return await self._ensure_delivery_field_values(page, "投放平台", platform_values)

    async def _ensure_delivery_field_values(self, page, field_text: str, value_texts: Iterable[str]) -> bool:
        """快速选择一个多选投放字段里的多个值，并逐个校验是否保留。"""
        values = [str(value or "").strip() for value in value_texts if str(value or "").strip()]
        if not values:
            return True
        selected = True
        keep_dropdown_open = field_text == "投放平台"
        for value_text in values:
            if await self._delivery_field_has_value(page, field_text, value_text):
                continue

            success = await self._ensure_delivery_field_value(
                page,
                field_text,
                value_text,
                keep_dropdown_open=keep_dropdown_open,
            )
            selected = selected and success
        await self._close_open_delivery_dropdown_if_needed(page)
        return selected

    async def _type_open_dropdown_value(self, page, value_text: str) -> None:
        """在当前展开的下拉搜索框里快速写入一个值。"""
        root = await self._open_dropdown_root(page)
        input_box = root.locator("input").first if root else None
        try:
            if input_box and await input_box.count() and await input_box.is_visible():
                await input_box.fill(value_text, timeout=3000)
                return
        except Exception:
            pass
        await self._keyboard_type(page, value_text, delay_ms=20)

    async def _ensure_delivery_field_value(
            self,
            page,
            field_text: str,
            value_text: str,
            *,
            keep_dropdown_open: bool = False,
    ) -> bool:
        """确保录入投放信息中的指定字段选择到目标值。"""
        if await self._delivery_field_has_value(page, field_text, value_text):
            return True
        attempt = 0
        while True:
            self._raise_if_cancelled()
            attempt += 1
            if keep_dropdown_open:
                if not await self._delivery_dropdown_opened(page):
                    if not await self._open_delivery_dropdown_by_label(page, field_text):
                        await page.wait_for_timeout(120)
                        continue
                await page.wait_for_timeout(120)
            else:
                await self._close_open_delivery_dropdown_if_needed(page)
                if not await self._open_delivery_dropdown_by_label(page, field_text):
                    await page.wait_for_timeout(200)
                    continue
                await page.wait_for_timeout(220)
            if field_text == "投放平台":
                await self._snapshot(page, f"chameleon_delivery_platform_dropdown_{attempt}")
                await self._type_into_open_dropdown(page, value_text)
                await self._snapshot(page, f"chameleon_delivery_platform_after_type_{attempt}")
            clicked = await self._click_dropdown_option(page, value_text)
            if not clicked:
                await page.wait_for_timeout(120 if keep_dropdown_open else 200)
                continue

            async def value_selected() -> bool:
                return await self._delivery_field_has_value(page, field_text, value_text)

            if await self._wait_for_result(value_selected, timeout_ms=None, interval_ms=400):
                if not keep_dropdown_open:
                    await self._close_open_delivery_dropdown_if_needed(page)
                return True
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] delivery field waiting "
                f"field={field_text}, value={value_text}, attempt={attempt}"
            )

    async def _open_delivery_dropdown_by_label(self, page, field_text: str) -> bool:
        """在录入确认框中，按字段标签打开对应下拉控件。"""
        root = await self._delivery_modal_root(page)
        if not root:
            return False

        # 优先从表单项内部找控件；新页面布局变化时，再按字段标签同一行找右侧控件兜底。
        candidates = []
        form_item = await self._delivery_form_item(page, field_text)
        if form_item:
            candidates.extend(await self._select_controls(form_item))
        candidates.extend(await self._nearby_select_controls(root, field_text))

        # Arco 下拉有时不响应 dispatchEvent，所以这里用真实鼠标点击并确认浮层真的展开。
        for locator in candidates[:4]:
            if await self._click_locator_center(page, locator):
                await page.wait_for_timeout(120)
                if await self._delivery_dropdown_opened(page):
                    return True
        return False

    async def _delivery_modal_root(self, page):
        """返回录入投放弹窗根节点。"""
        # body 是最后兜底：有些页面把内容放在 drawer/portal 外层，仍然能靠文案定位。
        for selector in (DELIVERY_MODAL_SELECTOR, "body"):
            for locator in reversed(await self._visible_locators(page.locator(selector), limit=30)):
                text = _compact_text(await self._locator_text(locator, timeout_ms=2000))
                if "投放产品" in text and "投放平台" in text:
                    return locator
        return None

    @staticmethod
    def _pre_submit_upload_wait_ms(item_count: int) -> int:
        """根据视频数量计算点击提交前的固定等待时间。"""
        return UserGrowthBrowserClient._bounded_timeout_ms(item_count, minimum=90000, per_item=10000,
                                                           maximum=5 * 60 * 1000)

    @staticmethod
    def _bounded_timeout_ms(item_count: int, *, minimum: int, per_item: int, maximum: int) -> int:
        """按素材数量计算带上下限的等待时间。"""
        count = max(item_count, 1)
        return min(max(minimum, count * per_item), maximum)

    async def _delivery_form_item(self, page, field_text: str):
        """按字段名返回投放弹窗里的表单项。"""
        root = await self._delivery_modal_root(page)
        if not root:
            return None
        wanted = _compact_text(field_text)
        # 只在弹窗内部查找，避免页面背景里同名字段干扰。
        for item in await self._visible_locators(root.locator(FORM_ITEM_SELECTOR), limit=80):
            if wanted in _compact_text(await self._locator_text(item, timeout_ms=1000)):
                return item
        return None

    async def _select_controls(self, root) -> list:
        """返回某个区域内可见可用的选择控件。"""
        controls = []
        for locator in await self._visible_locators(root.locator(SELECT_CONTROL_SELECTOR), limit=80):
            class_name = str(await locator.get_attribute("class") or "")
            aria_disabled = await locator.get_attribute("aria-disabled")
            # 同时看 aria 和 class，兼容 Arco 不同组件的禁用状态写法。
            if aria_disabled != "true" and "disabled" not in class_name:
                controls.append(locator)
        return controls

    async def _nearby_select_controls(self, root, field_text: str) -> list:
        """按字段标签所在行寻找右侧选择控件。"""
        label = await self._field_label(root, field_text)
        if not label:
            return []
        label_box = await label.bounding_box(timeout=2000)
        if not label_box:
            return []
        label_y = label_box["y"] + label_box["height"] / 2
        controls = []
        for control in await self._select_controls(root):
            box = await control.bounding_box(timeout=1000)
            # 同一行右侧控件是最接近人工视觉判断的兜底，避免误点其它投放字段。
            if box and box["x"] >= label_box["x"] and abs((box["y"] + box["height"] / 2) - label_y) <= 70:
                controls.append((abs((box["y"] + box["height"] / 2) - label_y), control))
        return [control for _, control in sorted(controls, key=lambda item: item[0])]

    async def _field_label(self, root, field_text: str):
        """返回字段标签元素，优先精确文本，避免遍历整个弹窗。"""
        wanted = _compact_text(field_text)
        if not wanted:
            return None

        candidates = []
        for text_variant in _ui_text_variants(field_text):
            try:
                candidates.append(root.get_by_text(text_variant, exact=True))
            except Exception:
                continue
        for selector in ("label", ".arco-form-item-label", "[class*='form-item-label']"):
            try:
                # 先收集当前弹窗内可见候选，再用归一化文本比较；页面字段可能使用
                # 全角括号/逗号，而配置路径使用半角标点。
                candidates.append(root.locator(selector))
            except Exception:
                continue

        for locator_group in candidates:
            for label in await self._visible_locators(locator_group, limit=160):
                try:
                    text = _compact_text(await self._locator_text(label, timeout_ms=500))
                except Exception:
                    continue
                if text == wanted or (wanted in text and len(text) <= len(wanted) + 12):
                    return label
        return None

    async def _open_dropdown_root(self, page):
        """返回当前展开的下拉浮层。"""
        for locator in reversed(await self._visible_locators(page.locator(DROPDOWN_ROOT_SELECTOR), limit=30)):
            text = _compact_text(await self._locator_text(locator, timeout_ms=1000))
            if text:
                return locator
        return None

    async def _dropdown_option_clickable(self, option) -> bool:
        """判断下拉选项是否可点击且尚未选中。"""
        text = _compact_text(await self._locator_text(option, timeout_ms=1000))
        if not text:
            return False
        class_name = str(await option.get_attribute("class") or "")
        aria_disabled = await option.get_attribute("aria-disabled")
        if aria_disabled == "true" or "disabled" in class_name:
            return False
        if await self._dropdown_option_selected(option):
            return False
        return True

    async def _dropdown_option_selected(self, option) -> bool:
        """识别 Arco 下拉项的已选状态，避免再次点击把多选项反选掉。"""
        class_name = str(await option.get_attribute("class") or "")
        aria_selected = await option.get_attribute("aria-selected")
        if aria_selected == "true" or "selected" in class_name:
            return True
        try:
            # 多选项里有 check 图标时也视为已选中，避免重复点击导致反选。
            return bool(await option.locator(".arco-icon-check").count())
        except Exception:
            return False

    async def _dropdown_at_bottom(self, root) -> bool:
        """判断下拉浮层是否已经滚到底。"""
        try:
            return bool(
                await root.evaluate(
                    """node => {
                        const items = [node, ...Array.from(node.querySelectorAll('*'))]
                            .filter(item => item.scrollHeight > item.clientHeight + 4);
                        const target = items.find(item => item.scrollTop + item.clientHeight < item.scrollHeight - 2);
                        return !target;
                    }"""
                )
            )
        except Exception:
            # 判断失败时按“到底”处理，宁可少滚一轮，也不要卡在无限滚动里。
            return True

    async def _scroll_dropdown_root(self, page, root) -> None:
        """滚动当前下拉浮层。"""
        try:
            box = await root.bounding_box(timeout=2000)
            if box:
                # 优先使用鼠标滚轮，触发组件自己的虚拟列表加载逻辑。
                await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + max(box["height"] - 16, 8))
                await page.mouse.wheel(0, max(int(box["height"] * 0.9), 220))
                return
        except Exception:
            pass
        try:
            # 鼠标滚轮失败时，直接推进内部可滚动容器作为最后兜底。
            await root.evaluate(
                """node => {
                    const items = [node, ...Array.from(node.querySelectorAll('*'))]
                        .filter(item => item.scrollHeight > item.clientHeight + 4);
                    const target = items.find(item => item.scrollTop + item.clientHeight < item.scrollHeight - 2) || items[0];
                    if (target) target.scrollTop = Math.min(target.scrollTop + Math.max(target.clientHeight * 0.85, 120), target.scrollHeight);
                }"""
            )
        except Exception:
            return

    async def _delivery_dropdown_opened(self, page) -> bool:
        """确认当前页面确实展开了 Arco 下拉浮层。"""
        return await self._open_dropdown_root(page) is not None

    async def _ensure_delivery_platform_all(self, page) -> bool:
        """把投放平台下拉里的平台全部选中。

        一次全选 + 点"投放平台"标题收起，不再做 has_selection 校验，
        校验交给最后的 _click_chameleon_modal_confirm 流程。
        """
        for attempt in range(2):
            if not await self._open_delivery_dropdown_by_label(page, "投放平台"):
                await page.wait_for_timeout(250)
                continue
            await page.wait_for_timeout(100)
            await self._snapshot(page, f"chameleon_delivery_platform_dropdown_{attempt + 1}")
            await self._set_open_dropdown_page_size_max(page)
            clicked_table_all = await self._click_open_dropdown_table_select_all(page)
            if not clicked_table_all:
                await self._select_all_open_dropdown_options(page)
            await self._snapshot(page, f"chameleon_delivery_platform_all_clicked_{attempt + 1}")
            # 选完不等待，直接点"投放平台"标题收回下拉
            await self._click_text(page, "投放平台")
            await page.wait_for_timeout(100)
            return True
        return False

    async def _set_open_dropdown_page_size_max(self, page) -> None:
        """如果打开的下拉里有分页条数选择，先切到最大条数。"""
        root = await self._open_dropdown_root(page)
        if not root:
            return
        try:
            controls = await self._visible_locators(
                root.locator(".arco-pagination-options-size-changer, .arco-select-view, [class*='size-changer']"),
                limit=20,
            )
            target = None
            for control in controls:
                text = _compact_text(await self._locator_text(control, timeout_ms=1000))
                if "条/页" in text or "条每页" in text:
                    target = control
                    break
            if not target:
                return
            if not (await self._click_locator_center(page, target) or await self._click_locator(target)):
                return
            await page.wait_for_timeout(500)
            for value in ("100条/页", "100 条/页", "50条/页", "50 条/页", "30条/页", "30 条/页", "20条/页", "20 条/页"):
                if await self._click_visible_dropdown_option(page, value):
                    await page.wait_for_timeout(1200)
                    return
        except Exception:
            return

    async def _click_open_dropdown_table_select_all(self, page) -> bool:
        """点击当前下拉浮层内的表格全选框。"""
        root = await self._open_dropdown_root(page)
        if not root:
            return False
        candidates = (
            root.locator(".arco-table thead .arco-checkbox-mask").first,
            root.locator(".arco-table thead .arco-checkbox-mask-wrapper").first,
            root.locator(".arco-table thead label.arco-checkbox").first,
            root.locator(".arco-table thead .arco-checkbox").first,
            root.get_by_text("全选", exact=True).first,
        )
        for checkbox in candidates:
            try:
                if not await checkbox.count() or not await checkbox.is_visible():
                    continue
                if await self._checkbox_box_is_checked(checkbox):
                    return True
                if await self._click_locator(checkbox):
                    await page.wait_for_timeout(900)
                    return True
            except Exception:
                continue
        return False

    async def _close_open_delivery_dropdown_if_needed(self, page) -> None:
        """仅在投放信息弹窗下拉仍然展开时尝试收起，避免遮挡确认按钮。"""
        for _ in range(4):
            if not await self._delivery_dropdown_opened(page):
                return
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(250)
            except Exception:
                pass
            if not await self._delivery_dropdown_opened(page):
                return
            try:
                root = await self._delivery_modal_root(page)
                box = await root.bounding_box(timeout=2000) if root else None
                if box and box["width"] < 1200 and box["height"] < 900:
                    await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + 20)
                    await page.wait_for_timeout(120)
            except Exception:
                pass

    async def _select_all_open_dropdown_options(self, page) -> int:
        """逐屏逐个点击当前打开下拉框里的未选中选项，直到滚动到底。"""
        total_clicked = 0
        stagnant_rounds = 0
        last_visible_text = ""
        for _ in range(35):
            root = await self._open_dropdown_root(page)
            if not root:
                break
            clicked_this_round = 0
            visible_text = _compact_text(await self._locator_text(root, timeout_ms=2000))
            options = await self._visible_locators(root.locator(DROPDOWN_OPTION_SELECTOR), limit=80)
            for option in options:
                try:
                    if not await self._dropdown_option_clickable(option):
                        continue
                    await option.click(timeout=3000)
                    clicked_this_round += 1
                    total_clicked += 1
                    await page.wait_for_timeout(80)
                except Exception:
                    continue
            if await self._dropdown_at_bottom(root):
                break
            await self._scroll_dropdown_root(page, root)
            # 虚拟列表滚动异常时，连续几轮没有新文本也没有点击，就停止防止死循环。
            if visible_text == last_visible_text and clicked_this_round == 0:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0
            last_visible_text = visible_text
            if stagnant_rounds >= 3:
                break
            await page.wait_for_timeout(150)
        return total_clicked

    async def _delivery_field_has_value(self, page, field_text: str, value_text: str) -> bool:
        """只检查指定字段自己的可见值，避免被页面其它同名文案误导。"""
        text = await self._delivery_field_text(page, field_text)
        normalized = _compact_text(text)
        value_area = normalized.replace(_compact_text(field_text), "", 1)
        return "请选择" not in value_area and self._delivery_value_visible(value_area, value_text)

    async def _delivery_platform_has_selection(self, page) -> bool:
        """判断投放平台字段是否已经有选择值，而不是停留在“请选择”。"""
        text = await self._delivery_field_text(page, "投放平台")
        normalized = _compact_text(text)
        value_text = normalized.replace("投放平台", "")
        return bool(value_text) and "请选择" not in value_text

    async def _delivery_field_text(self, page, field_text: str) -> str:
        """读取录入变色龙投放弹窗中某个字段所在表单项的可见文本。"""
        item = await self._delivery_form_item(page, field_text)
        return await self._locator_text(item) if item else ""

    async def _type_into_open_dropdown(self, page, keyword: str) -> None:
        """在已经展开的下拉框里输入关键词，优先让长列表过滤出目标选项。"""
        try:
            root = await self._open_dropdown_root(page)
            input_box = root.locator("input").first if root else None
            if input_box and await input_box.count() and await input_box.is_visible():
                # 可搜索下拉优先填 input，让平台自己过滤长列表。
                await input_box.fill(keyword, timeout=3000)
            else:
                # 部分 Arco 组件 input 不在浮层内，退回键盘输入给当前焦点。
                await self._keyboard_type(page, keyword)
            await page.wait_for_timeout(200)
        except Exception:
            return

    async def _click_dropdown_option(self, page, option_text: str) -> bool:
        """点击 Arco 下拉浮层里的指定选项。"""
        deadline = asyncio.get_event_loop().time() + 8
        while asyncio.get_event_loop().time() < deadline:
            if await self._click_visible_dropdown_option(page, option_text):
                return True
            await self._scroll_open_dropdown(page)
            await page.wait_for_timeout(120)
        return False

    async def _click_visible_dropdown_option(self, page, option_text: str) -> bool:
        """点击当前已经渲染出来的下拉选项。"""
        root = await self._open_dropdown_root(page)
        if not root:
            return False
        wanted = _compact_text(option_text)
        for option in await self._visible_locators(root.locator(DROPDOWN_OPTION_SELECTOR), limit=80):
            text = _compact_text(await self._locator_text(option, timeout_ms=1000))
            if text == wanted or wanted in text:
                if await self._dropdown_option_selected(option):
                    return True
                if not await self._dropdown_option_clickable(option):
                    return False
                return await self._click_locator(option)
        return False

    async def _scroll_open_dropdown(self, page) -> None:
        """滚动当前展开的下拉浮层，让未渲染在首屏的选项逐步出现。"""
        root = await self._open_dropdown_root(page)
        if root:
            await self._scroll_dropdown_root(page, root)

    async def _click_chameleon_modal_confirm(self, page) -> None:
        """只点击录入变色龙投放信息弹窗里的确认按钮。"""
        await self._close_open_delivery_dropdown_if_needed(page)
        root = await self._delivery_modal_root(page)
        clicked = False
        if root:
            for button in await self._visible_locators(root.locator("button"), limit=20):
                text = _compact_text(await self._locator_text(button, timeout_ms=1000))
                if text in {"确认", "确定"} and await self._click_locator(button):
                    clicked = True
                    break
        if not clicked:
            await self._click_first(page, ("button:has-text('确认')", "button:has-text('确定')"))
        await self._wait_chameleon_modal_closed(page)

    async def _wait_chameleon_modal_closed(self, page) -> None:
        """等待投放信息弹窗真正关闭，再操作背景素材卡片。"""
        attempt = 0
        while True:
            self._raise_if_cancelled()
            modal_visible = False
            for root in await self._visible_locators(page.locator(DELIVERY_MODAL_SELECTOR), limit=30):
                text = _compact_text(await self._locator_text(root, timeout_ms=1000))
                if "投放产品" in text and "投放平台" in text:
                    modal_visible = True
                    break
            if not modal_visible:
                return
            attempt += 1
            if attempt == 1 or attempt % 20 == 0:
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] "
                    f"waiting chameleon delivery modal closed, attempt={attempt}"
                )
            await self._sleep(0.5)

    async def _wait_chameleon_card_forms_ready(
            self,
            page,
            items: list[UserGrowthVideoItem],
            progress: ProgressCallback | None = None,
    ) -> None:
        """投放弹窗关闭后，等待当前页每张素材卡片的表单控件完整渲染。"""
        if not items:
            return
        required_fields = ["UGC内容", "分类标签", "自定义标签"]
        if self._is_redfruit_item(items[0]):
            required_fields.insert(1, "创意源")

        attempt = 0
        while True:
            self._raise_if_cancelled()
            attempt += 1
            state = await self._chameleon_card_form_state(page, items, required_fields)
            if state.get("ready"):
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] chameleon card forms ready: "
                    f"expected={state.get('expected_count')}, field_counts={state.get('field_counts')}"
                )
                return
            if attempt == 1 or attempt % 10 == 0:
                message = (
                    "等待录入页素材卡片表单完整加载："
                    f"期望 {state.get('expected_count', len(items))} 张，"
                    f"当前字段数量 {state.get('field_counts', {})}，第 {attempt} 次"
                )
                self._emit(progress, message)
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] waiting chameleon card forms: "
                    f"attempt={attempt}, state={state}"
                )
            await self._sleep(1.0)

    async def _chameleon_card_form_state(
            self,
            page,
            items: list[UserGrowthVideoItem],
            required_fields: list[str],
    ) -> dict[str, Any]:
        """读取录入页当前分页内各素材卡片必需表单控件的就绪数量。"""
        expected_count = len(items)
        try:
            pagers = await self._visible_locators(page.locator(".arco-pagination"), limit=20)
            page_size_found = False
            for pager in pagers:
                pager_text = _compact_text(await self._locator_text(pager, timeout_ms=1000))
                match = re.search(r"(\d+)条(?:/页|每页)", pager_text)
                if match:
                    expected_count = min(expected_count, max(int(match.group(1)), 1))
                    page_size_found = True
                    break
            if pagers and not page_size_found:
                expected_count = min(expected_count, 20)
        except Exception as exc:
            if self._is_session_closed_exception(exc):
                raise

        field_counts: dict[str, int] = {}
        for field_name in required_fields:
            ready_items: set[str] = set()
            for text_variant in _ui_text_variants(field_name):
                try:
                    labels = await self._visible_locators(
                        page.get_by_text(text_variant, exact=True),
                        limit=max(expected_count * 3, 80),
                    )
                except Exception as exc:
                    if self._is_session_closed_exception(exc):
                        raise
                    continue
                for label in labels:
                    try:
                        form_item = label.locator(
                            "xpath=ancestor::*[contains(@class,'arco-form-item') or "
                            "contains(@class,'form-item')][1]"
                        ).first
                        if not await form_item.count() or not await form_item.is_visible():
                            continue
                        if field_name == "自定义标签":
                            control = form_item.locator(
                                ".arco-input-tag input, .arco-input-tag-view input, "
                                "[class*='input-tag'] input, input"
                            ).first
                        else:
                            control = form_item.locator(SELECT_CONTROL_SELECTOR).first
                        if not await control.count() or not await control.is_visible():
                            continue
                        disabled = str(await control.get_attribute("aria-disabled") or "").lower() == "true"
                        class_name = str(await control.get_attribute("class") or "").lower()
                        if disabled or "disabled" in class_name:
                            continue
                        try:
                            box = await form_item.bounding_box(timeout=1200)
                            key = (
                                f"{round(box['x'], 1)}:{round(box['y'], 1)}:"
                                f"{round(box['width'], 1)}:{round(box['height'], 1)}"
                                if box else str(await form_item.evaluate("node => node.outerHTML"))
                            )
                        except Exception:
                            key = str(id(form_item))
                        ready_items.add(key)
                    except Exception as exc:
                        if self._is_session_closed_exception(exc):
                            raise
                        continue
            field_counts[field_name] = len(ready_items)

        ready = expected_count > 0 and all(
            field_counts.get(field_name, 0) >= expected_count
            for field_name in required_fields
        )
        return {
            "ready": ready,
            "expected_count": expected_count,
            "field_counts": field_counts,
        }

    @staticmethod
    def _delivery_value_visible(body: str, value_text: str) -> bool:
        """判断投放信息目标值是否已显示。"""
        return _compact_text(value_text) in _compact_text(body)

    async def _fill_card_defaults(self, page, item: UserGrowthVideoItem):
        """为素材卡片填写制作团队、授权、分类标签和自定义标签。"""
        if self._is_redfruit_item(item):
            return await self._fill_redfruit_card_defaults(page, item)

        if not await self._ensure_dropdown_value(page, "UGC内容", "不包含"):
            await self._snapshot_error(page, "soda_music_ugc_select_failed")
            raise RuntimeError("汽水音乐 UGC 内容选择失败")
        soda_paths = [
            ["汽水音乐-素材类型", "LUNA_剪辑制作", "LUNA_自产"],
            ["LUNA素材来源", "LUNA素材来源", "LUNA_千沧代理"],
        ]
        material_path = classification_path_for_material(item.file_name)
        material_path.insert(0, "LUNA功能卖点")
        soda_paths.append(material_path)
        await self._open_classification_modal_ready(
            page,
            lambda: self._open_label_selector(page),
            required_fields=[str(path[0]) for path in soda_paths],
            context_label="汽水音乐录入分类标签",
        )
        # 进入到弹窗进行级联选择
        await self._select_cascader(
            page,
            "汽水音乐-素材类型",
            soda_paths[0],
            field_timeout_ms=None,
        )
        await self._select_cascader(
            page,
            "LUNA素材来源",
            soda_paths[1],
            field_timeout_ms=None,
        )
        await self._select_cascader(
            page,
            "LUNA功能卖点",
            soda_paths[2],
            field_timeout_ms=None,
        )

        await self._click_if_present(page, "确定")

        # 填入预检阶段按模板渲染好的自定义标签
        tags = list(item.custom_tags)

        for tag in tags:
            input_box = await self._inputtag_for_field(page, "自定义标签")
            await input_box.fill(tag, timeout=5000)
            await page.keyboard.press("Enter")
            # tag 之间不留停顿，让 chip 一次性录入

        # 单选选择
        await self._click_radio_near_text(page, "未成年人内容", "已授权")
        await self._click_radio_near_text(page, "影视内容", "已授权")

        # 一键复用-全选-一键复用
        before_pages = list(page.context.pages)
        before_url = page.url
        await self._click_if_present(page, "一键复用")
        await page.wait_for_timeout(1000)
        await self._click_if_present(page, "全选")
        await page.wait_for_timeout(800)
        await self._return_material_page_to_first_page(page)
        await self._click_if_present(page, "一键复用")
        await page.wait_for_timeout(1000)
        await self._click_if_present(page, "提交")
        try:
            await page.wait_for_timeout(1500)
        except Exception:
            pass
        return await self._wait_and_click_task_detail_after_submit(
            page,
            before_pages,
            before_url,
        )

    async def _fill_redfruit_card_defaults(self, page, item: UserGrowthVideoItem) -> None:
        """填写红果短剧素材卡片。"""
        if not await self._ensure_dropdown_value(page, "UGC内容", "不包含"):
            await self._snapshot_error(page, "redfruit_ugc_select_failed")
            raise RuntimeError("红果短剧 UGC 内容选择失败")
        await self._snapshot(page, "redfruit_ugc_selected")
        if not await self._ensure_dropdown_value(page, "创意源", "原创"):
            await self._snapshot_error(page, "redfruit_creative_source_select_failed")
            raise RuntimeError("红果短剧创意源选择失败")
        paths = [
            list(path)
            for path in item.classification_paths or item.workflow_metadata.get("classification_paths", [])
            if path
        ]
        await self._open_classification_modal_ready(
            page,
            lambda: self._open_label_selector(page),
            required_fields=[str(path[0]) for path in paths],
            context_label="红果短剧录入分类标签",
            refresh_before_reopen=True,
            on_page_refreshed=lambda: self._restore_redfruit_entry_card_after_classification_reload(
                page,
                item,
            ),
        )

        for path in paths:
            if not path:
                continue
            if not await self._select_cascader(
                    page,
                    str(path[0]),
                    list(path),
                    field_timeout_ms=None,
            ):
                raise RuntimeError(f"红果分类标签选择失败：{path[0]}")
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] redfruit classification selected: "
                f"{' > '.join(str(value) for value in path)}"
            )
        await self._click_if_present(page, "确定")

        for tag in item.custom_tags:
            input_box = await self._inputtag_for_field(page, "自定义标签")
            await input_box.fill(tag, timeout=5000)
            await page.keyboard.press("Enter")

        await self._click_radio_near_text(page, "未成年人内容", "已授权")
        await self._click_radio_near_text(page, "影视内容", "已授权")
        return await self._reuse_all_submit_and_open_task_detail(page, item)

    async def _restore_redfruit_entry_card_after_classification_reload(
            self,
            page,
            item: UserGrowthVideoItem,
    ) -> None:
        """刷新红果录入页后，恢复分类弹窗前必须已就绪的首卡状态。"""
        await self._wait_chameleon_card_forms_ready(page, [item])
        if not await self._ensure_dropdown_value(page, "UGC内容", "不包含"):
            raise RuntimeError("刷新后红果短剧 UGC 内容选择失败")
        if not await self._ensure_dropdown_value(page, "创意源", "原创"):
            raise RuntimeError("刷新后红果短剧创意源选择失败")

    async def _ensure_dropdown_value(self, page, field_text: str, value_text: str) -> bool:
        """普通下拉字段已是目标值时跳过，否则选择目标值。"""
        if await self._dropdown_field_has_value(page, field_text, value_text):
            return True
        return await self._select_dropdown_value(page, field_text, value_text)

    async def _select_redfruit_required_cascader(
            self,
            page,
            field_name: str,
            paths: Iterable[list[str]],
    ) -> None:
        """尝试多种红果级联路径，全部失败时抛出明确错误。"""
        last_error: Exception | None = None
        for path in paths:
            try:
                await self._select_cascader(page, field_name, list(path))
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
        await self._snapshot_error(page, f"redfruit_{field_name}_select_failed", exc=last_error)
        raise RuntimeError(f"红果分类标签选择失败：{field_name}") from last_error

    async def _cascader_trigger_for_field(
            self,
            page,
            field_name: str,
            *,
            prefer_modal_bottom: bool = False,
            field_timeout_ms: int | None = 12000,
    ):
        """按字段名定位真正的级联触发控件，优先同一表单项，避免点到邻近字段。"""
        root = await self._active_modal_root(page)
        if root and prefer_modal_bottom:
            await self._reset_root_scroll_to_top(page, root)

        if root:
            # 先在整个弹窗里精确定位字段文本，再回到所属表单项找控件。
            # 不再先遍历 120 个表单项并逐个读取内部 div/span 文本。
            label = await self._wait_cascader_field_label(
                page,
                field_name,
                timeout_ms=field_timeout_ms,
                prefer_modal_bottom=prefer_modal_bottom,
            )
            if label:
                controls = await self._nearby_select_controls(root, field_name)
                if controls:
                    return controls[0]
                form_item = label.locator(
                    "xpath=ancestor::*[contains(@class,'arco-form-item') or contains(@class,'form-item')][1]"
                ).first
                if await form_item.count():
                    controls = await self._select_controls(form_item)
                    if controls:
                        return controls[0]

            # 布局不规范时保留少量表单项兜底扫描。
            for item in await self._visible_locators(root.locator(FORM_ITEM_SELECTOR), limit=30):
                label = await self._field_label(item, field_name)
                if not label:
                    continue
                controls = await self._select_controls(item)
                if controls:
                    return controls[0]

        body = page.locator("body").first
        for control in await self._visible_locators(body.locator(SELECT_CONTROL_SELECTOR), limit=120):
            text = _compact_text(await self._locator_text(control, timeout_ms=1000))
            if _compact_text(field_name) and _compact_text(field_name) in text:
                return control
        return None

    async def _reuse_all_submit_and_open_task_detail(
            self,
            page,
            item: UserGrowthVideoItem | None = None,
    ):
        """复用首卡标签，提交并跨标签页进入任务详情。"""
        before_pages = list(page.context.pages)
        before_url = page.url
        await self._click_if_present(page, "一键复用")
        await page.wait_for_timeout(200)
        await self._select_all_visible_pages(page)
        await page.wait_for_timeout(800)
        # 跨页全选后页面停在最后一页；最后一次“一键复用”必须回到首卡，
        # 否则会把最后一页的空卡片当作复用源，覆盖掉首卡已填写的分类标签。
        await self._return_material_page_to_first_page(page)
        if item and self._is_redfruit_item(item):
            await self._assert_redfruit_classification_selected(page, item)
        await self._click_if_present(page, "一键复用")
        await page.wait_for_timeout(1000)
        await self._click_if_present(page, "提交")
        try:
            await page.wait_for_timeout(1500)
        except Exception:
            pass
        return await self._wait_and_click_task_detail_after_submit(
            page,
            before_pages,
            before_url,
        )

    async def _return_material_page_to_first_page(self, page) -> None:
        """跨页全选后回到录入页第 1 页，确保首卡仍是复用来源。"""
        attempt = 0
        while True:
            self._raise_if_cancelled()
            attempt += 1
            before_signature = await self._material_page_signature(page)
            pagers = await self._visible_locators(page.locator(".arco-pagination"), limit=20)
            moved = False
            found_pager = False
            for pager in pagers:
                controls = await self._visible_locators(
                    pager.locator("li, button, a, [role='button']"),
                    limit=80,
                )
                numeric: list[tuple[int, object, bool]] = []
                for control in controls:
                    text = _compact_text(await self._locator_text(control, timeout_ms=1000))
                    if not re.fullmatch(r"\d{1,4}", text):
                        continue
                    value = int(text)
                    class_name = str(await control.get_attribute("class") or "").lower()
                    aria_current = str(await control.get_attribute("aria-current") or "").lower()
                    active = "active" in class_name or "current" in class_name or aria_current == "page"
                    numeric.append((value, control, active))
                if not numeric:
                    continue
                found_pager = True
                active_values = [value for value, _, active in numeric if active]
                current = max(active_values) if active_values else min(value for value, _, _ in numeric)
                if current <= 1:
                    return
                first = next((control for value, control, _ in numeric if value == 1), None)
                if first and (await self._click_locator_center(page, first) or await self._click_locator(first)):
                    moved = True
                    self._write_run_log(
                        f"[{datetime.now().isoformat(timespec='seconds')}] material card pagination: "
                        f"{current} -> 1"
                    )
                    break
            if not found_pager:
                # 单页时没有分页条，首卡天然就是复用来源。
                return
            if moved:
                while True:
                    self._raise_if_cancelled()
                    current_signature = await self._material_page_signature(page)
                    if current_signature and current_signature != before_signature:
                        break
                    await self._sleep(0.5)
                continue
            if attempt == 1 or attempt % 10 == 0:
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] waiting material card page 1, "
                    f"attempt={attempt}"
                )
            await self._sleep(1)

    async def _wait_and_click_task_detail_after_submit(
            self,
            page,
            before_pages: list,
            before_url: str,
    ):
        """提交后从存活页面中寻找查看任务详情，兼容旧页关闭和新标签页。"""
        context = page.context
        before_ids = {id(candidate) for candidate in before_pages}
        attempt = 0
        while True:
            self._raise_if_cancelled()
            attempt += 1
            try:
                pages = list(context.pages)
            except Exception:
                pages = []

            for candidate in reversed(pages):
                try:
                    if candidate.is_closed():
                        continue
                    candidate_url = str(candidate.url or "")
                except Exception:
                    continue

                if await self._acknowledge_chameleon_bid_warning_and_resubmit(candidate):
                    continue

                if "/aigc/manage/task" in candidate_url:
                    return candidate

                if "/aigc/creatives/upload" in candidate_url:
                    try:
                        body = _compact_text(await self._body_text(candidate, timeout_ms=2000))
                    except Exception:
                        body = ""
                    validation_errors = [
                        text for text in (
                            "请选择分类标签",
                            "请选择UGC内容",
                            "请选择创意源",
                        ) if text in body
                    ]
                    if validation_errors:
                        message = "提交被页面校验拦截：" + "、".join(validation_errors)
                        await self._snapshot_error(candidate, "chameleon_submit_validation_failed", extra=message)
                        raise RuntimeError(message)

                try:
                    detail = candidate.get_by_text("查看任务详情", exact=True).first
                    if await detail.count() and await detail.is_visible():
                        click_before_pages = list(context.pages)
                        click_before_url = candidate_url
                        await detail.click()
                        target_page = await self._wait_page_change_or_new_page(
                            candidate,
                            click_before_pages,
                            click_before_url,
                            timeout_ms=None,
                        )
                        return target_page or candidate
                except Exception as exc:
                    if self._is_target_closed_exception(exc):
                        continue

            if attempt % 10 == 0:
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] waiting task detail after submit "
                    f"attempt={attempt}, before_url={before_url}, before_pages={len(before_ids)}"
                )
            await self._sleep(1)

    @staticmethod
    def _is_chameleon_bid_validation_warning(text: str) -> bool:
        """只识别录入提交后的自定义标签 BID 校验警告。"""
        return "自定义标签BID校验失败" in _compact_text(text).upper()

    async def _visible_chameleon_bid_validation_warning_root(self, page):
        """返回当前可见的录入 BID 校验警告弹窗。"""
        roots = await self._visible_locators(page.locator(DELIVERY_MODAL_SELECTOR), limit=40)
        for root in reversed(roots):
            text = await self._locator_text(root, timeout_ms=1200)
            if self._is_chameleon_bid_validation_warning(text):
                return root
        return None

    async def _click_chameleon_bid_warning_confirm(self, root) -> bool:
        """只点击 BID 校验警告弹窗内部的确认按钮。"""
        if await self._click_first_visible_locator(
            root.get_by_text("确认", exact=True).first,
            root.get_by_role("button", name="确认", exact=True).first,
            root.locator("button:has-text('确认')").first,
            root.get_by_text("确定", exact=True).first,
            root.get_by_role("button", name="确定", exact=True).first,
            root.locator("button:has-text('确定')").first,
        ):
            return True
        for button in await self._visible_locators(root.locator("button"), limit=20):
            if _compact_text(await self._locator_text(button, timeout_ms=800)) in {"确认", "确定"}:
                return await self._click_locator(button)
        return False

    async def _wait_chameleon_bid_warning_closed(self, root) -> None:
        """等待 BID 校验警告弹窗关闭后再点背景提交。"""
        while True:
            self._raise_if_cancelled()
            try:
                if not await root.count() or not await root.is_visible():
                    return
            except Exception as exc:
                if self._is_session_closed_exception(exc):
                    raise
                return
            await self._sleep(0.3)

    async def _acknowledge_chameleon_bid_warning_and_resubmit(self, page) -> bool:
        """确认录入 BID 警告并继续提交；未出现该警告时不操作。"""
        root = await self._visible_chameleon_bid_validation_warning_root(page)
        if root is None:
            return False
        if not await self._click_chameleon_bid_warning_confirm(root):
            await self._snapshot_error(
                page,
                "chameleon_bid_validation_warning_confirm_missing",
                extra="检测到自定义标签 BID 校验失败弹窗，但未找到确认按钮",
            )
            raise RuntimeError("自定义标签 BID 校验失败弹窗未找到确认按钮")

        await self._wait_chameleon_bid_warning_closed(root)
        self._write_run_log(
            f"[{datetime.now().isoformat(timespec='seconds')}] "
            "chameleon custom-tag BID warning acknowledged; resubmit"
        )
        self._write_event("chameleon_bid_warning_acknowledged")

        while True:
            self._raise_if_cancelled()
            try:
                if page.is_closed():
                    return True
            except Exception:
                return True
            current_url = str(page.url or "")
            if "/aigc/manage/task" in current_url:
                return True
            detail = page.get_by_text("查看任务详情", exact=True).first
            try:
                if await detail.count() and await detail.is_visible():
                    return True
            except Exception as exc:
                if self._is_session_closed_exception(exc):
                    raise
            if await self._click_text_or_locator(page, "提交"):
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] "
                    "chameleon resubmitted after custom-tag BID warning"
                )
                return True
            await self._sleep(0.5)

    async def _assert_redfruit_classification_selected(
            self,
            page,
            item: UserGrowthVideoItem,
    ) -> None:
        """提交前确认首卡分类标签不是空值，且包含预检得到的叶子标签。"""
        expected = [
            _compact_text(str(path[-1]))
            for path in (item.classification_paths or item.workflow_metadata.get("classification_paths", []))
            if path and str(path[-1]).strip()
        ]
        label = page.get_by_text("分类标签", exact=True).first
        if not await label.count():
            raise RuntimeError("红果短剧提交前未找到首卡分类标签字段")
        form_item = label.locator(
            "xpath=ancestor::*[contains(@class,'arco-form-item') or contains(@class,'form-item')][1]"
        ).first
        text = _compact_text(await self._locator_text(form_item, timeout_ms=3000))
        missing = [value for value in expected if value not in text]
        if "请选择分类标签" in text or missing:
            await self._snapshot_error(
                page,
                "redfruit_classification_not_selected_before_reuse",
                extra=f"missing={missing}; field_text={text}",
            )
            raise RuntimeError(
                "红果短剧首卡分类标签未填写完整，停止提交："
                + ("、".join(missing) if missing else "请选择分类标签")
            )

    async def _select_cascader(
            self,
            page,
            field_name: str,
            path: list[str],
            *,
            field_timeout_ms: int | None = 10000,
            prefer_modal_bottom: bool = False,
    ) -> bool:
        """
        通用级联选择

        path:
        [
            "汽水音乐-素材类型",
            "LUNA_剪辑制作",
            "LUNA_自产"
        ]
        """
        trigger = await self._cascader_trigger_for_field(
            page,
            field_name,
            prefer_modal_bottom=prefer_modal_bottom,
            field_timeout_ms=field_timeout_ms,
        )
        if not trigger:
            title = await self._wait_cascader_field_label(
                page,
                field_name,
                timeout_ms=field_timeout_ms,
                prefer_modal_bottom=prefer_modal_bottom,
            )
            if not title:
                raise RuntimeError(f"未找到级联字段：{field_name}")

            form_item = title.locator(
                "xpath=ancestor::*[contains(@class,'arco-form-item') or contains(@class,'form-item')][1]"
            ).first
            if await form_item.count():
                trigger = form_item.locator("div[class*='arco-cascader'], .arco-cascader-view").first
            else:
                trigger = title.locator(
                    "xpath=following::div[contains(@class,'arco-cascader')][1]"
                )

        await trigger.wait_for(
            state="visible",
            timeout=0 if field_timeout_ms is None else field_timeout_ms,
        )

        input_box = trigger.locator("input")

        if await input_box.count():
            await input_box.click()

        else:
            await trigger.click(force=True)

        await page.wait_for_timeout(1000)

        popup_count = await page.locator(".arco-cascader-popup").count()
        if popup_count == 0:
            raise RuntimeError(f"级联弹窗打开失败: {field_name}")

        # 逐级点击。部分字段打开后第一列直接是子级，不再展示与字段同名的根节点。
        for index, value in enumerate(path):
            success = await self._click_cascader_option(page, value)

            if not success:
                if (
                        index == 0
                        and len(path) > 1
                        and _compact_cascader_text(value) == _compact_cascader_text(field_name)
                ):
                    self._write_run_log(
                        f"[{datetime.now().isoformat(timespec='seconds')}] cascader root skipped: "
                        f"field={field_name}, root={value}"
                    )
                    continue
                # 上一级点击后，Arco 级联的下一列可能尚未完成渲染。
                # 等待目标节点真实出现后再重试一次，避免高速模式下误报找不到标签。
                try:
                    await self._wait_cascader_option(page, value)
                except Exception:
                    pass
                success = await self._click_cascader_option(page, value)
                if not success:
                    raise RuntimeError(f"级联选择失败: {value}")

            # 等待下一列展开
            if index < len(path) - 1:
                next_value = path[index + 1]
                await self._wait_cascader_child(
                    page,
                    current_value=value,
                    next_value=next_value,
                    field_name=field_name,
                )

        # 最后一项点击成功只代表事件发出，页面还可能在异步回显。
        # 等待当前字段真正显示叶子值，避免保存时把仍为“请选择分类标签”的字段提交出去。
        final_value = str(path[-1]) if path else ""
        if final_value and not await self._wait_for_result(
                lambda: self._cascader_field_has_value(page, trigger, field_name, final_value),
                timeout_ms=8000,
                interval_ms=300,
        ):
            raise RuntimeError(f"级联选择未回显：{field_name} -> {final_value}")

        return True

    async def _cascader_field_has_value(
            self,
            page,
            trigger,
            field_name: str,
            value_text: str,
    ) -> bool:
        """确认级联控件已回显目标叶子值，而不是仅确认点击事件完成。"""
        wanted = _compact_cascader_text(value_text)
        if not wanted:
            return True

        candidates = [trigger]
        try:
            form_item = trigger.locator(
                "xpath=ancestor::*[contains(@class,'arco-form-item') or contains(@class,'form-item')][1]"
            ).first
            if await form_item.count():
                candidates.append(form_item)
        except Exception as exc:
            if self._is_session_closed_exception(exc):
                raise

        for candidate in candidates:
            try:
                if not candidate or not await candidate.count() or not await candidate.is_visible():
                    continue
                text = _compact_text(await self._locator_text(candidate, timeout_ms=800))
                if "请选择" not in text and wanted in _compact_cascader_text(text):
                    return True
            except Exception as exc:
                if self._is_session_closed_exception(exc):
                    raise

        # 某些页面会在重新渲染时替换原触发节点，重新按字段找一次只用于读回显，
        # 不执行点击，不会改变当前操作对象。
        try:
            current = await self._cascader_trigger_for_field(page, field_name)
            if current:
                text = _compact_text(await self._locator_text(current, timeout_ms=800))
                return "请选择" not in text and wanted in _compact_cascader_text(text)
        except Exception as exc:
            if self._is_session_closed_exception(exc):
                raise
        return False

    async def _cascader_option_is_visible(self, page, value: str) -> bool:
        """只检查当前最后一列是否已经出现目标级联节点。"""
        wanted = _compact_cascader_text(value)
        popup = page.locator(".arco-cascader-popup").last
        options = popup.locator(
            ".arco-cascader-list-column:last-child .arco-cascader-list-item-label"
        )
        for option in await self._visible_locators(options, limit=80):
            try:
                if _compact_cascader_text(await option.inner_text()) == wanted:
                    return True
            except Exception:
                continue
        return False

    async def _wait_cascader_child(
            self,
            page,
            *,
            current_value: str,
            next_value: str,
            field_name: str,
    ) -> None:
        """等待当前级联节点展开下一列，只重试当前节点。"""
        attempt = 0
        while True:
            self._raise_if_cancelled()
            if await self._cascader_option_is_visible(page, next_value):
                return
            attempt += 1
            # 先用悬停触发 Arco 的展开事件，再用点击兜底；始终只操作
            # 当前路径节点，避免高速等待期间误点其它分类。
            if attempt == 1 or attempt % 4 == 0:
                current = await self._cascader_option_locator(page, current_value)
                if current:
                    try:
                        await current.hover(timeout=3000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(350)
                if not await self._cascader_option_is_visible(page, next_value):
                    await self._click_cascader_option(page, current_value)
            if attempt % 10 == 0:
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] cascader child wait: "
                    f"field={field_name}, current={current_value}, next={next_value}, attempt={attempt}"
                )
            await self._sleep(0.5)

    async def _cascader_option_locator(self, page, value: str):
        """返回当前最后一列中指定的可见级联节点。"""
        wanted = _compact_cascader_text(value)
        popup = page.locator(".arco-cascader-popup").last
        nodes = popup.locator(
            ".arco-cascader-list-column:last-child .arco-cascader-list-item-label"
        )
        for node in await self._visible_locators(nodes, limit=80):
            try:
                if _compact_cascader_text(await node.inner_text()) == wanted:
                    return node
            except Exception:
                continue
        return None

    async def _wait_cascader_field_label(
            self,
            page,
            field_name: str,
            *,
            timeout_ms: int | None = 10000,
            prefer_modal_bottom: bool = False,
    ):
        """在当前页面或弹窗中找到级联字段标签，并逐段扫描虚拟滚动列表。"""
        root = await self._active_modal_root(page)
        if prefer_modal_bottom and root:
            await self._reset_root_scroll_to_top(page, root)

        async def find_label():
            nonlocal root
            root = root or await self._active_modal_root(page)
            if root:
                label = await self._field_label(root, field_name)
                if label:
                    await self._scroll_locator_into_view(label)
                    return label

                # 修改分类标签弹窗使用虚拟滚动，目标字段可能相隔多个视口。
                # 每轮只推进当前弹窗，并在到底后再做一次最终检查，避免漏掉底部字段。
                moved = await self._scroll_root(page, root)
                label = await self._field_label(root, field_name)
                if label:
                    await self._scroll_locator_into_view(label)
                    return label
                if not moved:
                    try:
                        await self._scroll_root(page, root, to_bottom=True)
                    except Exception:
                        pass
                    label = await self._field_label(root, field_name)
                    if label:
                        await self._scroll_locator_into_view(label)
                        return label

            for text_variant in _ui_text_variants(field_name):
                for label in await self._visible_locators(
                        page.get_by_text(text_variant, exact=True),
                        limit=160,
                ):
                    await self._scroll_locator_into_view(label)
                    return label
            return None

        return await self._wait_for_result(find_label, timeout_ms=timeout_ms, interval_ms=350)

    async def _active_modal_root(self, page):
        """返回当前最上层可见弹窗/抽屉根节点。"""
        roots = await self._visible_locators(page.locator(DELIVERY_MODAL_SELECTOR), limit=40)
        for root in reversed(roots):
            text = _compact_text(await self._locator_text(root, timeout_ms=1000))
            if text:
                return root
        return roots[-1] if roots else None

    async def _open_classification_modal_ready(
            self,
            page,
            opener,
            *,
            required_fields: Iterable[str] = (),
            progress: ProgressCallback | None = None,
            context_label: str = "分类标签",
            refresh_before_reopen: bool = False,
            on_page_refreshed: Callable[[], Awaitable[None]] | None = None,
    ):
        """打开分类标签弹窗并持续等待目标字段；可按工作流要求刷新后重开。"""
        required = [str(field).strip() for field in required_fields if str(field).strip()]
        reopen_attempt = 0
        backoff_seconds = 2.0
        while True:
            self._raise_if_cancelled()
            reopen_attempt += 1
            await opener()
            wait_attempt = 0
            while True:
                self._raise_if_cancelled()
                wait_attempt += 1
                probe = await self._classification_modal_probe(
                    page,
                    required,
                    scan_all=bool(required),
                )
                root = probe.get("root")
                empty_message = str(probe.get("empty_message") or "")
                field_names = list(probe.get("field_names") or [])
                field_count = len(field_names)
                if root is not None and empty_message:
                    reason = empty_message
                    recovery_action = "取消后刷新页面再打开" if refresh_before_reopen else "取消后再打开"
                    self._emit(
                        progress,
                        f"{context_label}未完整加载：{reason}，{recovery_action}"
                        f"（{backoff_seconds:.1f}s 后重试）",
                    )
                    self._write_run_log(
                        f"[{datetime.now().isoformat(timespec='seconds')}] classification modal reopen: "
                        f"context={context_label}, reopen_attempt={reopen_attempt}, reason={reason}, "
                        f"field_count={field_count}, required={required}, "
                        f"refresh_before_reopen={refresh_before_reopen}, backoff={backoff_seconds:.1f}s"
                    )
                    await self._cancel_classification_modal(page, root)
                    await self._sleep(backoff_seconds)
                    if refresh_before_reopen:
                        await self._reload_classification_modal_host(page, context_label)
                        if on_page_refreshed is not None:
                            await on_page_refreshed()
                    backoff_seconds = min(backoff_seconds * 2, 30.0)
                    break

                normalized_names = {_compact_text(name) for name in field_names}
                missing = [field for field in required if _compact_text(field) not in normalized_names]
                if root is not None and not missing:
                    self._write_run_log(
                        f"[{datetime.now().isoformat(timespec='seconds')}] classification modal ready: "
                        f"context={context_label}, field_count={field_count}, required={required}"
                    )
                    return root

                if wait_attempt == 1 or wait_attempt % 10 == 0:
                    self._write_run_log(
                        f"[{datetime.now().isoformat(timespec='seconds')}] waiting classification modal: "
                        f"context={context_label}, wait_attempt={wait_attempt}, root={root is not None}, "
                        f"field_count={field_count}, missing={missing}"
                    )
                await self._sleep(1.0)

    async def _reload_classification_modal_host(self, page, context_label: str) -> None:
        """红果弹窗空态时刷新宿主页，避免在同一份空表单上反复打开。"""
        self._write_run_log(
            f"[{datetime.now().isoformat(timespec='seconds')}] classification modal host reload: "
            f"context={context_label}, url={page.url}"
        )
        try:
            await page.reload(wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            if self._is_session_closed_exception(exc) or self._is_recoverable_session_exception(exc):
                raise
            raise RuntimeError(f"{context_label}刷新页面失败：{exc}") from exc
        await self._sleep(2.0)

    async def _classification_modal_probe(
            self,
            page,
            required_fields: list[str],
            *,
            scan_all: bool = False,
    ) -> dict[str, Any]:
        """读取当前分类弹窗的空态文案和已加载字段。"""
        roots = await self._visible_locators(page.locator(DELIVERY_MODAL_SELECTOR), limit=40)
        root = None
        for candidate in reversed(roots):
            text = _compact_text(await self._locator_text(candidate, timeout_ms=1000))
            if "投放产品" in text and "投放平台" in text:
                continue
            if (
                    "分类标签" in text
                    or "修改分类标签" in text
                    or any(_compact_text(field) in text for field in required_fields)
                    or "暂无" in text
            ):
                root = candidate
                break
        if root is None:
            return {"root": None, "empty_message": "", "field_names": []}

        empty_message = await self._classification_modal_empty_message(root)
        field_names = await self._classification_modal_field_names(page, root, scan_all=scan_all)
        if field_names:
            empty_message = ""
        return {
            "root": root,
            "empty_message": empty_message,
            "field_names": field_names,
        }

    async def _classification_modal_empty_message(self, root) -> str:
        """只在当前分类弹窗内部识别“暂无XXX”空态。"""
        selectors = (
            ".arco-empty, .arco-empty-description, [class*='empty'], [class*='no-data']",
            "div, span, p",
        )
        for selector in selectors:
            try:
                locators = await self._visible_locators(root.locator(selector), limit=160)
            except Exception:
                continue
            for locator in locators:
                text = _compact_text(await self._locator_text(locator, timeout_ms=500))
                if text.startswith("暂无") and len(text) <= 30:
                    return text
        return ""

    async def _classification_modal_field_names(self, page, root, *, scan_all: bool = False) -> list[str]:
        """读取分类弹窗中的可选择字段；需要时逐段扫描虚拟滚动内容。"""
        names: dict[str, str] = {}

        async def collect() -> None:
            items = root.locator(".arco-form-item")
            try:
                count = await items.count()
            except Exception:
                count = 0
            if count == 0:
                items = root.locator("[class*='form-item']")
                try:
                    count = await items.count()
                except Exception:
                    count = 0
            for index in range(min(count, 160)):
                item = items.nth(index)
                try:
                    if not await item.is_visible():
                        continue
                    controls = item.locator(SELECT_CONTROL_SELECTOR)
                    if not await controls.count():
                        continue
                    label = item.locator(
                        ".arco-form-item-label, [class*='form-item-label'], label"
                    ).first
                    if not await label.count():
                        continue
                    raw_name = (await self._locator_text(label, timeout_ms=600)).strip()
                    raw_name = re.sub(r"^[*＊\s]+|[*＊\s]+$", "", raw_name)
                    compact_name = _compact_text(raw_name)
                    if compact_name:
                        names[compact_name] = raw_name
                except Exception as exc:
                    if self._is_session_closed_exception(exc):
                        raise

        if scan_all:
            await self._reset_root_scroll_to_top(page, root)
        await collect()
        if scan_all:
            for _ in range(80):
                moved = await self._scroll_root(page, root)
                await collect()
                if not moved:
                    break
            try:
                await self._scroll_root(page, root, to_bottom=True)
            except Exception:
                pass
            await collect()
            await self._reset_root_scroll_to_top(page, root)
        return list(names.values())

    async def _cancel_classification_modal(self, page, root) -> None:
        """只取消当前分类标签弹窗，避免误点背景页面按钮。"""
        clicked = await self._click_first_visible_locator(
            root.get_by_text("取消", exact=True).first,
            root.get_by_role("button", name="取消", exact=True).first,
            root.locator("button:has-text('取消')").first,
        )
        if not clicked:
            clicked = await self._click_first_visible_locator(
                root.locator(".arco-modal-close-icon").first,
                root.locator("button[aria-label='Close']").first,
                root.locator("[class*='close-icon']").first,
            )
        if not clicked:
            # 部分红果弹窗在空态加载期间不渲染按钮/关闭图标，但仍支持
            # Escape 关闭。只要当前弹窗消失，就按“取消成功”处理并由调用方重开。
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
                if not await root.is_visible():
                    return
            except Exception as exc:
                if self._is_session_closed_exception(exc):
                    raise
            # 空态弹窗可能由页面状态异步销毁，DOM 根节点会一直可见但已
            # 不再接收交互。此时不把它升级成浏览器故障；返回给重开循环，
            # 由下一次“修改分类标签”触发替换/重建弹窗。
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] classification modal close control absent; "
                "keep browser session and let reopen loop replace the empty modal"
            )
            return

        while True:
            self._raise_if_cancelled()
            try:
                if not await root.count() or not await root.is_visible():
                    return
            except Exception as exc:
                if self._is_session_closed_exception(exc):
                    raise
                return
            await self._sleep(0.3)

    async def _scroll_locator_into_view(self, locator) -> None:
        """滚动元素进入可点击视野。"""
        try:
            await locator.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            return

    async def _reset_root_scroll_to_top(self, page, root) -> None:
        """把弹窗内可滚动区域复位到顶部，随后可逐段扫描虚拟字段列表。"""
        try:
            await root.evaluate(
                """node => {
                    const nodes = [node, ...Array.from(node.querySelectorAll('*'))];
                    for (const el of nodes.filter(item => item.scrollHeight > item.clientHeight + 8)) {
                        if (el.scrollTop > 0) {
                            el.scrollTop = 0;
                            el.dispatchEvent(new Event('scroll', { bubbles: true }));
                        }
                    }
                }"""
            )
        except Exception:
            pass
        await page.wait_for_timeout(350)

    async def _scroll_root(self, page, root, *, to_bottom: bool = False) -> bool:
        """滚动弹窗/抽屉中的可滚动区域，兼容内部内容区。"""
        try:
            box = await root.bounding_box(timeout=1500)
            if box:
                await page.mouse.move(
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                )
                await page.mouse.wheel(0, max(int(box["height"] * 0.85), 260))
        except Exception:
            pass
        changed = False
        try:
            changed = bool(
                await root.evaluate(
                    """(node, toBottom) => {
                        const nodes = [node, ...Array.from(node.querySelectorAll('*'))];
                        const scrollables = nodes.filter(el => el.scrollHeight > el.clientHeight + 8);
                        let moved = false;
                        for (const el of scrollables) {
                            const before = el.scrollTop;
                            const delta = Math.max(el.clientHeight * 0.85, 220);
                            el.scrollTop = toBottom
                                ? el.scrollHeight
                                : Math.min(el.scrollTop + delta, el.scrollHeight);
                            if (Math.abs(el.scrollTop - before) > 1) {
                                moved = true;
                            }
                        }
                        return moved;
                    }""",
                    to_bottom,
                )
            )
        except Exception:
            changed = False
        await page.wait_for_timeout(350)
        return changed

    async def _wait_next_cascader_column(
            self,
            page,
            timeout_ms=5000
    ):
        """等待级联下一列展开"""

        for _ in range(10):

            columns = await page.locator(
                ".arco-cascader-list"
            ).count()

            if columns >= 2:
                return

            await page.wait_for_timeout(500)

        raise RuntimeError(
            "级联下一层未展开"
        )

    async def _inputtag_for_field(self, page, field_text: str):
        """按 label 定位同行右侧 InputTag 的 input。"""
        label = page.get_by_text(field_text, exact=True).first
        await label.wait_for(state="visible", timeout=10000)
        return label.locator(
            "xpath=following::div[contains(@class,'arco-input-tag')][1]//input"
        )

    async def _wait_cascader_option(
            self,
            page,
            value: str,
            timeout_ms: int = 10000
    ):
        """等待级联节点出现"""

        wanted = _compact_cascader_text(value)

        async def check():
            options = page.locator(
                ".arco-cascader-list-column:last-child .arco-cascader-option:visible"
            )

            for option in await self._visible_locators(
                    options,
                    limit=80
            ):
                text = _compact_cascader_text(
                    await self._locator_text(option)
                )

                if text == wanted:
                    return True

            return False

        await self._retry(
            lambda _: check(),
            description=f"等待级联节点 {value}",
            max_attempts=10,
            base_interval_ms=500,
        )

    async def _click_cascader_option(
            self,
            page,
            value: str
    ) -> bool:
        """
        点击当前级联节点

        Arco Cascader:
        - 文本在 .arco-cascader-list-item-label
        - 展开事件绑定在 li.arco-cascader-list-item
        """

        wanted = _compact_cascader_text(value)

        popup = page.locator(
            ".arco-cascader-popup"
        ).last

        if not await popup.count():
            return False

        # 只在当前最后一列找节点。遍历整个弹窗会命中前面已经展开的列，
        # 造成“点击调用成功但下一列没有展开”的假成功。
        nodes = popup.locator(
            ".arco-cascader-list-column:last-child .arco-cascader-list-item-label"
        )

        for node in await self._visible_locators(nodes, limit=80):
            try:
                text = _compact_cascader_text(await node.inner_text())
            except Exception:
                continue

            if text != wanted:
                continue

            try:

                # Arco 事件通常绑定在 li，但页面动画/遮罩偶尔会让
                # locator.click 一直等待稳定。节点已经可见时，给点击一个
                # 独立的短边界，并在失败时退回真实鼠标点击。
                item = node.locator(
                    "xpath=ancestor::li[contains(@class,'arco-cascader-list-item')]"
                ).first
                target = item if await item.count() else node
                clicked = False
                try:
                    await node.click(force=True, timeout=3000)
                    clicked = True
                except Exception:
                    try:
                        await target.click(force=True, timeout=3000)
                        clicked = True
                    except Exception:
                        pass
                    try:
                        box = await target.bounding_box(timeout=1500)
                        if box:
                            await page.mouse.click(
                                box["x"] + box["width"] / 2,
                                box["y"] + box["height"] / 2,
                            )
                            clicked = True
                    except Exception:
                        pass
                if not clicked:
                    return False

                await page.wait_for_timeout(180)
                return True


            except Exception as e:
                return False

        return False

    async def _select_dropdown_value(
            self,
            page,
            field_text: str,
            value_text: str | None = None
    ) -> bool:
        """
        点击下拉框并选择值。
        field_text 支持:
        1. 表单标题，例如 UGC内容
        2. placeholder，例如 请选择UGC内容
        """
        try:
            trigger = await self._dropdown_trigger_for_field(page, field_text)
            if trigger:
                if not await self._click_locator_center(page, trigger):
                    if not await self._click_locator(trigger):
                        raise RuntimeError(f"未点击到下拉控件: {field_text}")
            elif not value_text and field_text in {"分类标签", "选择分类标签"}:
                await self._open_label_selector(page)
                return True
            else:
                # 最后兜底才点击文案本身，避免误点到左侧 label
                await self._click_text(page, field_text)
            await page.wait_for_timeout(200)
            if not value_text:
                return True

            if await self._dropdown_field_has_value(page, field_text, value_text, trigger):
                return True
            for _ in range(3):
                if await self._click_visible_dropdown_option(page, value_text):
                    await page.wait_for_timeout(120)
                    if await self._dropdown_field_has_value(page, field_text, value_text, trigger):
                        return True
                if not await self._delivery_dropdown_opened(page):
                    trigger = await self._dropdown_trigger_for_field(page, field_text)
                    if trigger:
                        if not await self._click_locator_center(page, trigger):
                            await self._click_locator(trigger)
                    await page.wait_for_timeout(120)
                root = await self._open_dropdown_root(page)
                input_box = root.locator("input").first if root else None
                if input_box and await input_box.count() and await input_box.is_visible():
                    try:
                        await input_box.fill(value_text, timeout=3000)
                    except Exception:
                        await self._keyboard_type(page, value_text)
                else:
                    await self._keyboard_type(page, value_text)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(120)
                if await self._dropdown_field_has_value(page, field_text, value_text, trigger):
                    return True
                if await self._click_visible_dropdown_option(page, value_text):
                    await page.wait_for_timeout(120)
                    if await self._dropdown_field_has_value(page, field_text, value_text, trigger):
                        return True

            if await self._click_dropdown_option(page, value_text):
                await page.wait_for_timeout(120)
                if await self._dropdown_field_has_value(page, field_text, value_text, trigger):
                    return True
            return False

        except Exception as exc:
            if self._is_session_closed_exception(exc):
                raise
            return False

    async def _dropdown_field_has_value(self, page, field_text: str, value_text: str, trigger=None) -> bool:
        """判断普通下拉字段是否已经显示目标值。"""
        wanted = _compact_text(value_text)
        if not wanted:
            return True
        candidates = []
        if trigger:
            candidates.append(trigger)
        latest_trigger = await self._dropdown_trigger_for_field(page, field_text)
        if latest_trigger:
            candidates.append(latest_trigger)
        seen: set[int] = set()
        for locator in candidates:
            key = id(locator)
            if key in seen:
                continue
            seen.add(key)
            try:
                text = _compact_text(await self._locator_text(locator, timeout_ms=1000))
                if "请选择" not in text and wanted in text:
                    return True
            except Exception:
                continue
        return False

    async def _dropdown_trigger_for_field(self, page, field_text: str):
        """按字段名或 placeholder 找到真正的下拉触发控件，避免误点左侧标题。"""
        wanted = _compact_text(field_text)
        body = page.locator("body").first

        nearby_controls = await self._nearby_select_controls(body, field_text)
        if nearby_controls:
            return nearby_controls[0]

        for item in await self._visible_locators(body.locator(FORM_ITEM_SELECTOR), limit=120):
            item_text = _compact_text(await self._locator_text(item, timeout_ms=1000))
            if wanted and wanted in item_text:
                controls = await self._select_controls(item)
                if controls:
                    return controls[0]

        for control in await self._visible_locators(page.locator(SELECT_CONTROL_SELECTOR), limit=120):
            text = _compact_text(await self._locator_text(control, timeout_ms=1000))
            if wanted and wanted in text:
                return control

        placeholder_candidates = []
        for text in (field_text, f"选择{field_text}", f"请选择{field_text}"):
            compacted = _compact_text(text)
            if compacted and compacted not in placeholder_candidates:
                placeholder_candidates.append(compacted)

        for candidate in placeholder_candidates:
            for locator in (
                    page.get_by_text(candidate, exact=True).first,
                    page.get_by_text(candidate, exact=False).first,
            ):
                try:
                    if not await locator.count() or not await locator.is_visible():
                        continue
                    for xpath in (
                            "xpath=ancestor::*[contains(@class, 'arco-select-view')][1]",
                            "xpath=ancestor::*[contains(@class, 'arco-cascader-view')][1]",
                            "xpath=ancestor::*[contains(@class, 'arco-input-tag-view')][1]",
                            "xpath=ancestor::*[contains(@class, 'arco-input-tag')][1]",
                            "xpath=ancestor::*[contains(@class, 'select-view')][1]",
                            "xpath=ancestor::*[contains(@class, 'cascader-view')][1]",
                            "xpath=ancestor::*[contains(@class, 'input-tag')][1]",
                            "xpath=ancestor::*[@role='combobox'][1]",
                    ):
                        trigger = locator.locator(xpath)
                        if await trigger.count() and await trigger.is_visible():
                            return trigger
                    return locator
                except Exception:
                    continue

        return None

    async def _click_radio_near_text(self, page, field_text: str, value_text: str) -> None:
        """点击某个字段附近的单选值；找不到时保持页面现状。"""
        try:
            await self._click_text(page, field_text)
            await self._click_text(page, value_text)
        except RuntimeError:
            return

    async def _open_label_selector(self, page) -> None:
        """打开分类标签选择入口。"""
        for field_text in ("选择分类标签", "分类标签"):
            trigger = await self._dropdown_trigger_for_field(page, field_text)
            if trigger:
                if await self._click_locator_center(page, trigger) or await self._click_locator(trigger):
                    await page.wait_for_timeout(250)
                    return
        for text in ("选择分类标签", "分类标签"):
            try:
                await self._click_text(page, text)
                await page.wait_for_timeout(250)
                return
            except RuntimeError:
                continue
        raise RuntimeError("未找到分类标签入口")

    async def _ensure_custom_tags(self, page, tags: Iterable[str]) -> None:
        """确保素材卡片中包含规则要求的全部自定义标签。"""
        for tag in tags:
            body = await page.locator("body").inner_text(timeout=5000)
            if tag in body:
                continue
            input_box = await self._first_existing(
                page,
                (
                    "input[placeholder*='自定义标签']",
                    "input[placeholder*='标签']",
                    "textarea[placeholder*='标签']",
                ),
            )
            if not input_box:
                continue
            await input_box.fill(tag)
            await input_box.press("Enter")
            await page.wait_for_timeout(500)

    async def _submit_review(self, page, *, allow_already_submitted: bool = False) -> bool:
        """发起送审，并确认送审弹窗。"""
        if not await self._click_text_or_locator(page, "送审"):
            body = _compact_text(await self._body_text(page, timeout_ms=5000))
            submitted_markers = ("已送审", "送审中", "审核中", "审核通过", "审核完成")
            if allow_already_submitted and any(marker in body for marker in submitted_markers):
                return False
            raise RuntimeError("未找到送审按钮，且页面没有已送审状态")
        await page.wait_for_timeout(2000)
        # await self._ensure_chameleon_modal(page)
        await self._click_text(page, "确定")
        await page.wait_for_timeout(3500)
        await self._click_if_present(page, "查看任务详情")
        return True

    async def _submit_soda_review(
            self,
            page,
            plan: UserGrowthOrderPlan,
            task_id: str,
            progress: ProgressCallback | None,
            *,
            allow_already_submitted: bool = False,
    ) -> None:
        """汽水送审先落断点；恢复时允许识别已送审页面并继续。"""
        plan.review_task_id = task_id
        self._checkpoint(plan, "review_submitting", f"准备送审任务 {task_id}")
        submitted_now = await self._submit_review(
            page,
            allow_already_submitted=allow_already_submitted,
        )
        if not submitted_now:
            self._emit(progress, f"汽水音乐任务 {task_id} 已处于送审状态，断点恢复跳过重复点击送审")
        self._checkpoint(plan, "review_submitted", f"已完成送审，任务 ID：{task_id}")

    async def _wait_task_success(
            self,
            page,
            progress: ProgressCallback | None,
            *,
            expected_attempts: int | None = None,
            plan: UserGrowthOrderPlan | None = None,
            items: list[UserGrowthVideoItem] | None = None,
            task_id: str | None = None,
            retry_failed_task: bool = False,
            operation_name: str = "素材上传",
    ) -> str:
        """轮询任务状态；红果任务明确失败时可在目标任务行重试三次。"""
        backup_after_attempts = max(expected_attempts or self.max_status_retries, 1) + 2
        backup_attempted = False
        task_failure_retries = 0
        # 间隔减半，最小 3s，避免空转
        interval_ms = max(int(self.refresh_interval_seconds * 500), 3000)
        attempt = 0
        while True:
            self._raise_if_cancelled()
            attempt += 1
            body = await page.locator("body").inner_text(timeout=5000)
            status_text = _compact_text(body)
            if retry_failed_task and task_id:
                row = await self._find_task_row(page, task_id)
                status_text = (
                    _compact_text(await self._locator_text(row, timeout_ms=3000))
                    if row else ""
                )
            if "全部成功" in status_text:
                return "success"
            if "失败" in status_text:
                if retry_failed_task and task_id and task_failure_retries < 3:
                    task_failure_retries += 1
                    await self._retry_failed_operation_task(
                        page,
                        task_id,
                        progress,
                        operation_name=operation_name,
                        retry_number=task_failure_retries,
                    )
                    continue
                if retry_failed_task and task_id:
                    raise RuntimeError(f"{operation_name}任务 {task_id} 重试 3 次后仍失败")
                raise RuntimeError("任务执行失败")
            if (
                    not backup_attempted
                    and attempt >= backup_after_attempts
                    and plan is not None
                    and items
                    and task_id
            ):
                backup_attempted = True
                if await self._try_backup_cids_before_review(page, plan, items, task_id, progress):
                    return "cid_backed_up"
            self._emit(progress, f"刷新任务状态，第 {attempt} 次")
            await self._click_if_present(page, "刷新列表")
            await page.wait_for_timeout(interval_ms)

    async def _try_backup_cids_before_review(
            self,
            page,
            plan: UserGrowthOrderPlan,
            items: list[UserGrowthVideoItem],
            task_id: str,
            progress: ProgressCallback | None,
    ) -> bool:
        """任务状态长时间未成功时，先从查看详情读取 CID 并写回，标记未送审。"""
        original_state = [
            (item, item.cid, item.cid_material_type, item.status, item.message)
            for item in items
        ]
        detail_page = None
        before_url = page.url
        try:
            self._emit(progress, f"任务 {task_id} 状态暂未全部成功，尝试先从查看详情备份 CID")
            detail_page = await self._open_task_detail_without_wait(page, task_id)
            await self._read_cids_from_task_detail_page(detail_page, items, "未送审")
            plan.status = "success"
            plan.message = "CID 已备份，未送审"
            if self.order_complete:
                self.order_complete(plan)
                setattr(plan, "_pre_review_cid_backfilled", True)
            self._emit(progress, f"任务 {task_id} CID 已先写回，备注：未送审")
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] task {task_id} cid backed up before review"
            )
            return True
        except Exception as exc:  # noqa: BLE001
            for item, cid, cid_material_type, status, message in original_state:
                item.cid = cid
                item.cid_material_type = cid_material_type
                item.status = status
                item.message = message
            plan.status = "pending"
            plan.message = ""
            try:
                if detail_page is not None and detail_page is not page:
                    await self._close_page_intentionally(detail_page)
                elif page.url != before_url:
                    await page.go_back(timeout=10000)
                    await page.wait_for_timeout(1500)
            except Exception:
                pass
            self._emit(progress, f"任务 {task_id} CID 备份暂未成功，继续刷新状态：{exc}")
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] task {task_id} cid backup skipped: {exc}"
            )
            return False

    async def _fill_cids_for_task(
            self,
            page,
            items: list[UserGrowthVideoItem],
            task_id: str,
            progress: ProgressCallback | None,
            *,
            retry_failed_task: bool = False,
            operation_name: str = "素材上传",
            plan: UserGrowthOrderPlan | None = None,
    ) -> None:
        """按任务ID进入任务详情与素材页，读取 CID 后回填到素材条目。"""
        try:
            page = await self._open_task_detail_for_task_id(
                page,
                task_id,
                progress,
                expected_attempts=max(len(items), 1),
                retry_failed_task=retry_failed_task,
                operation_name=operation_name,
                plan=plan,
            )
            material_page = await self._open_material_list_page(page)
            cids = await self._read_cids_from_search_input(material_page)
        except UserGrowthOperationTaskFailed:
            raise
        except Exception as exc:
            await self._snapshot_error(
                page,
                f"task_{task_id}_fill_cids_fallback",
                exc=exc,
                extra=f"task_id={task_id}, items={len(items)}",
            )
            await self._fill_cids_from_detail(page, items)
            return

        if not cids:
            await self._snapshot_error(
                material_page,
                f"task_{task_id}_cid_not_found",
                extra=f"task_id={task_id}, items={len(items)}",
            )
            raise RuntimeError(f"任务 {task_id} 未读取到 CID")
        if len(cids) < len(items):
            await self._snapshot_error(
                material_page,
                f"task_{task_id}_cid_count_mismatch",
                extra=f"task_id={task_id}, expected={len(items)}, got={len(cids)}",
            )
            raise RuntimeError(f"任务 {task_id} 读取到的 CID 数量不足：期望 {len(items)}，实际 {len(cids)}")

        for item, cid in zip(items, cids):
            item.cid = cid
            item.cid_material_type = await self._read_material_type_by_cid(material_page, cid) or item.material_type
            item.status = "success"
            item.message = "上传并送审成功"

    async def _process_redfruit_after_review(
            self,
            page,
            plan: UserGrowthOrderPlan,
            items: list[UserGrowthVideoItem],
            task_id: str,
            progress: ProgressCallback | None,
    ) -> None:
        """红果短剧送审后追加 ARLP，并修改素材分类标签。"""
        self._assert_redfruit_state_machine(plan)
        self._emit(progress, f"红果短剧任务 {task_id}：进入素材/文案列表处理 ARLP")
        detail_page = await self._open_task_detail_for_task_id(
            page,
            task_id,
            progress,
            expected_attempts=max(len(items), 1),
        )
        cid_page = await self._open_material_list_page(detail_page)
        cids = await self._read_cids_from_search_input(cid_page)
        if cids:
            for item, cid in zip(items, cids):
                item.cid = cid
        else:
            self._emit(progress, f"红果短剧任务 {task_id}：未读取到 CID，继续执行 ARLP 和分类标签修改")

        # 送审任务详情打开的素材列表不一定带有可操作的当前结果集。
        # 红果首跑与断点续跑统一按已回填 CID 精确定位，避免在空的默认列表上无限等待。
        active_cids = [
            str(item.cid or "").strip().lower()
            for item in items
            if item.status != "skipped" and str(item.cid or "").strip()
        ]
        if len(active_cids) == len([item for item in items if item.status != "skipped"]):
            await self._search_redfruit_materials_by_cids(
                cid_page,
                active_cids,
                progress,
                wait_on_empty=True,
            )

        await self._process_redfruit_material_stages(cid_page, plan, items, progress)

    async def _process_redfruit_material_stages(
            self,
            material_page,
            plan: UserGrowthOrderPlan,
            items: list[UserGrowthVideoItem],
            progress: ProgressCallback | None,
    ) -> None:
        """在已定位到本批素材的素材管理页继续 ARLP 和后审分类标签。"""
        self._assert_redfruit_state_machine(plan)
        arlp_stages = self._redfruit_arlp_stages(items[0]) if items else []
        if self._upgrade_legacy_redfruit_arlp_checkpoint(plan, arlp_stages):
            self._checkpoint(
                plan,
                "arlp_submitting",
                f"兼容旧断点：历史 ARLP 第一阶段已完成，从第 2/{len(arlp_stages)} 阶段继续",
            )

        if plan.arlp_stage_index < len(arlp_stages):
            for stage_index in range(plan.arlp_stage_index, len(arlp_stages)):
                stage_config = arlp_stages[stage_index]
                stage_name = str(stage_config.get("name") or f"阶段 {stage_index + 1}")
                stage_progress = self._arlp_progress_entry(plan, stage_index, stage_config)
                saved_task_id = str(stage_progress.get("task_id") or "").strip()
                saved_status = str(stage_progress.get("status") or "").strip()
                if saved_status == "success":
                    if saved_task_id and saved_task_id not in plan.arlp_stage_task_ids:
                        plan.arlp_stage_task_ids.append(saved_task_id)
                    plan.arlp_stage_index = max(plan.arlp_stage_index, stage_index + 1)
                    self._checkpoint(
                        plan,
                        "arlp_success" if plan.arlp_stage_index >= len(arlp_stages) else "arlp_submitting",
                        f"断点确认 ARLP 阶段 {stage_index + 1}/{len(arlp_stages)} 已成功，跳过重复提交",
                    )
                    continue
                resumable_stage = bool(
                    saved_task_id
                    and saved_status in {"task_created", "waiting_result", "partial_failure"}
                )
                if resumable_stage:
                    plan.arlp_task_id = saved_task_id
                elif not plan.arlp_task_id or plan.arlp_task_id in plan.arlp_stage_task_ids:
                    plan.arlp_task_id = ""
                if resumable_stage:
                    self._checkpoint(
                        plan,
                        "arlp_submitting",
                        f"断点恢复 ARLP：{stage_name}（{stage_index + 1}/{len(arlp_stages)}），"
                        f"继续任务 {saved_task_id}",
                    )
                else:
                    self._update_arlp_progress(
                        plan,
                        stage_index,
                        stage_config=stage_config,
                        status="running",
                        step="stage_started",
                        attempt=int(stage_progress.get("attempt") or 0),
                        checkpoint_stage="arlp_submitting",
                        message=f"开始增加 ARLP：{stage_name}（{stage_index + 1}/{len(arlp_stages)}）",
                    )
                await self._ensure_redfruit_arlp_all_success(
                    material_page,
                    plan,
                    items,
                    progress,
                    stage_config=stage_config,
                    stage_number=stage_index + 1,
                    stage_total=len(arlp_stages),
                )
                plan.arlp_stage_index = stage_index + 1
                if plan.arlp_task_id and plan.arlp_task_id not in plan.arlp_stage_task_ids:
                    plan.arlp_stage_task_ids.append(plan.arlp_task_id)
                self._update_arlp_progress(
                    plan,
                    stage_index,
                    stage_config=stage_config,
                    status="success",
                    step="stage_completed",
                    task_id=plan.arlp_task_id,
                    checkpoint_stage=None,
                )
                checkpoint_stage = "arlp_success" if plan.arlp_stage_index >= len(arlp_stages) else "arlp_submitting"
                self._checkpoint(
                    plan,
                    checkpoint_stage,
                    f"ARLP 阶段 {stage_name} 已全部成功（{plan.arlp_stage_index}/{len(arlp_stages)}）",
                )
                if plan.arlp_stage_index < len(arlp_stages):
                    await self._refresh_material_list_page(material_page)

        if plan.stage not in {"classification_success", "completed"}:
            await self._refresh_material_list_page(material_page)
            self._update_classification_progress(
                plan,
                status="running",
                step="stage_started",
                save_status="pending",
                checkpoint_stage="classification_submitting",
                message="开始修改红果素材分类标签",
            )
            await self._ensure_redfruit_post_review_classifications_all_success(
                material_page,
                plan,
                items,
                progress,
            )
            self._update_classification_progress(
                plan,
                status="success",
                step="completed",
                save_status="success",
                message="红果素材分类标签已全部成功",
            )
            self._checkpoint(plan, "classification_success", "红果素材分类标签已全部成功")

        plan.status = "success"
        plan.message = "红果短剧送审、ARLP 和分类标签修改完成"
        for item in items:
            item.status = "success"
            item.cid_material_type = item.cid_material_type or item.material_type
            item.message = "红果短剧上传、送审、ARLP 和分类标签修改完成"

    @staticmethod
    def _upgrade_legacy_redfruit_arlp_checkpoint(
            plan: UserGrowthOrderPlan,
            arlp_stages: list[dict[str, list[str] | str]],
    ) -> bool:
        """把旧单阶段 ARLP 成功断点迁移为三阶段流程的第一阶段已完成。"""
        if len(arlp_stages) <= 1 or plan.arlp_stage_index != 0:
            return False
        if plan.stage not in {"arlp_success", "classification_submitting"}:
            return False
        plan.arlp_stage_index = 1
        if plan.arlp_task_id and plan.arlp_task_id not in plan.arlp_stage_task_ids:
            plan.arlp_stage_task_ids.append(plan.arlp_task_id)
        return True

    @staticmethod
    def _redfruit_arlp_stages(item: UserGrowthVideoItem) -> list[dict[str, list[str] | str]]:
        metadata = item.workflow_metadata or {}
        configured = metadata.get("arlp_stages")
        stages: list[dict[str, list[str] | str]] = []
        if isinstance(configured, list):
            for index, raw_stage in enumerate(configured):
                if not isinstance(raw_stage, dict):
                    continue
                products = [str(value).strip() for value in raw_stage.get("products", []) if str(value).strip()]
                platforms = [str(value).strip() for value in raw_stage.get("platforms", []) if str(value).strip()]
                if products and platforms:
                    stages.append({
                        "name": str(raw_stage.get("name") or f"阶段 {index + 1}").strip(),
                        "products": products,
                        "platforms": platforms,
                    })
        if stages:
            return stages
        return [{
            "name": "红果漫剧/番茄小说",
            "products": list(metadata.get("arlp_products") or ["红果免费漫剧(8704)", "番茄免费小说(1967)"]),
            "platforms": list(metadata.get("arlp_platforms") or metadata.get("delivery_platforms") or []),
        }]

    async def _ensure_redfruit_arlp_modal(
            self,
            page,
            item: UserGrowthVideoItem,
            stage_config: dict[str, list[str] | str],
    ) -> None:
        """填写红果增加 ARLP 弹窗中的投放产品和平台。"""
        products = list(stage_config.get("products") or [])
        if not await self._ensure_delivery_field_values(page, "投放产品", products):
            await self._snapshot_error(page, "redfruit_arlp_product_not_selected")
            raise RuntimeError(f"增加 ARLP 未选中投放产品：{'、'.join(products)}")
        platform_values = list(stage_config.get("platforms") or [])
        platform_ok = await self._ensure_delivery_field_values(page, "投放平台", platform_values)
        if not platform_ok:
            await self._snapshot_error(page, "redfruit_arlp_platform_not_selected")
            raise RuntimeError("增加 ARLP 未选中投放平台")

    async def _ensure_redfruit_arlp_all_success(
            self,
            material_page,
            plan: UserGrowthOrderPlan,
            items: list[UserGrowthVideoItem],
            progress: ProgressCallback | None,
            *,
            stage_config: dict[str, list[str] | str],
            stage_number: int,
            stage_total: int,
    ) -> None:
        """反复增加一个 ARLP 配置阶段，直到对应任务报告全部素材成功。"""
        self._assert_redfruit_state_machine(plan)
        if not items:
            return

        attempt = 0
        stage_name = str(stage_config.get("name") or f"阶段 {stage_number}")
        stage_index = max(stage_number - 1, 0)
        stage_progress = self._arlp_progress_entry(plan, stage_index, stage_config)
        saved_task_id = str(stage_progress.get("task_id") or "").strip()
        saved_status = str(stage_progress.get("status") or "").strip()
        if saved_task_id and saved_status in {"task_created", "waiting_result", "partial_failure"}:
            plan.arlp_task_id = saved_task_id
            attempt = int(stage_progress.get("attempt") or 1)
            self._update_arlp_progress(
                plan,
                stage_index,
                stage_config=stage_config,
                status="waiting_result",
                step="waiting_result",
                task_id=saved_task_id,
                attempt=attempt,
                message=f"断点恢复等待 ARLP【{stage_name}】任务 {saved_task_id}",
            )
            result = await self._resume_waiting_redfruit_arlp_task(
                material_page,
                plan,
                items,
                progress,
                stage_config=stage_config,
                stage_number=stage_number,
                stage_total=stage_total,
            )
            if self._redfruit_operation_all_expected_success(result, len(items)):
                return
            await self._refresh_material_list_page(material_page)
            attempt = max(attempt, int(stage_progress.get("attempt") or 1))
        else:
            attempt = int(stage_progress.get("attempt") or 0)
        while True:
            self._raise_if_cancelled()
            attempt += 1
            self._update_arlp_progress(
                plan,
                stage_index,
                stage_config=stage_config,
                status="selection_started",
                step="selecting_materials",
                attempt=attempt,
                task_id="",
                message=f"ARLP【{stage_name}】第 {attempt} 次开始选择素材",
            )
            if attempt > 1:
                await self._clear_redfruit_material_selection(material_page, progress)
                self._emit(
                    progress,
                    f"红果短剧 ARLP【{stage_name}】第 {attempt} 次：重新点第一条素材并全选所有",
                )

            await self._wait_material_items_ready(material_page, items)
            await self._select_all_materials(material_page, items)
            self._update_arlp_progress(
                plan,
                stage_index,
                stage_config=stage_config,
                status="selection_started",
                step="materials_selected",
                attempt=attempt,
                checkpoint_stage="arlp_submitting",
                message=f"ARLP【{stage_name}】已完成素材选择",
            )
            await self._run_material_edit_action(material_page, "增加ARLP")
            self._update_arlp_progress(
                plan,
                stage_index,
                stage_config=stage_config,
                status="modal_open",
                step="modal_open",
                attempt=attempt,
                message=f"ARLP【{stage_name}】弹窗已打开",
            )
            await self._ensure_redfruit_arlp_modal(material_page, items[0], stage_config)
            self._update_arlp_progress(
                plan,
                stage_index,
                stage_config=stage_config,
                status="submitting",
                step="fields_filled",
                attempt=attempt,
                message=f"ARLP【{stage_name}】投放产品和平台已填写",
            )

            before_pages = list(material_page.context.pages)
            before_url = material_page.url
            await self._click_redfruit_modal_action(material_page, ("保存并送审", "保存"))
            self._update_arlp_progress(
                plan,
                stage_index,
                stage_config=stage_config,
                status="submitting",
                step="submitted",
                attempt=attempt,
                message=f"ARLP【{stage_name}】已点击提交",
            )
            task_page = await self._wait_redfruit_arlp_task_page(
                material_page,
                before_pages,
                before_url,
                progress,
            )
            plan.arlp_task_id = await self._read_current_task_id(task_page, progress)
            self._update_arlp_progress(
                plan,
                stage_index,
                stage_config=stage_config,
                status="task_created",
                step="task_created",
                attempt=attempt,
                task_id=plan.arlp_task_id,
                message=f"已记录 ARLP【{stage_name}】任务 ID：{plan.arlp_task_id}",
            )
            self._checkpoint(
                plan,
                "arlp_submitting",
                f"已记录 ARLP【{stage_name}】任务 ID：{plan.arlp_task_id}（{stage_number}/{stage_total}）",
            )
            self._update_arlp_progress(
                plan,
                stage_index,
                stage_config=stage_config,
                status="waiting_result",
                step="waiting_result",
                attempt=attempt,
                task_id=plan.arlp_task_id,
                message=f"开始等待 ARLP【{stage_name}】任务 {plan.arlp_task_id}",
            )
            try:
                result = await self._wait_redfruit_arlp_task_result(
                    task_page,
                    progress,
                    "增加 ARLP",
                    stage_config=stage_config,
                    expected_count=len(items),
                    verification_cids=[item.cid for item in items],
                )
            except Exception as exc:  # noqa: BLE001
                self._update_arlp_progress(
                    plan,
                    stage_index,
                    stage_config=stage_config,
                    status="failed",
                    step="waiting_result_failed",
                    attempt=attempt,
                    task_id=plan.arlp_task_id,
                    last_error=str(exc),
                    message=f"ARLP【{stage_name}】等待任务结果失败：{exc}",
                )
                raise
            plan.arlp_task_id = str(result.get("task_id") or "")
            result_success = int(result.get("success") or 0)
            result_total = int(result.get("total") or 0)
            result_failed = int(result.get("failed") or 0)
            self._update_arlp_progress(
                plan,
                stage_index,
                stage_config=stage_config,
                status=("success" if self._redfruit_operation_all_expected_success(result, len(items)) else "partial_failure"),
                step="result_received",
                attempt=attempt,
                task_id=plan.arlp_task_id,
                total=result_total,
                success=result_success,
                failed=result_failed,
                message=f"ARLP【{stage_name}】收到任务结果：成功 {result_success}/{result_total}，失败 {result_failed}",
            )
            self._emit(
                progress,
                f"红果短剧 ARLP【{stage_name}】第 {attempt} 次结果：任务 {result['task_id']}，"
                f"成功 {result['success']}/{result['total']}，失败 {result['failed']}",
            )
            await self._close_redfruit_result_dialog(material_page)

            if self._redfruit_operation_all_expected_success(result, len(items)):
                self._emit(
                    progress,
                    f"红果短剧 ARLP【{stage_name}】全部成功：{result['success']}/{result['total']} "
                    f"（阶段 {stage_number}/{stage_total}）",
                )
                return

            self._emit(
                progress,
                f"红果短剧 ARLP【{stage_name}】未覆盖整批：{result['success']}/{result['total']}，目标 {len(items)} 条，"
                "清空当前选择后重新增加 ARLP",
            )
            await self._refresh_material_list_page(material_page)

    async def _resume_waiting_redfruit_arlp_task(
            self,
            material_page,
            plan: UserGrowthOrderPlan,
            items: list[UserGrowthVideoItem],
            progress: ProgressCallback | None,
            *,
            stage_config: dict[str, list[str] | str],
            stage_number: int,
            stage_total: int,
    ) -> dict[str, int | str]:
        """从已保存的 ARLP 任务号继续等待，不重新创建同一阶段任务。"""
        task_id = str(plan.arlp_task_id or "").strip()
        if not task_id:
            raise RuntimeError("ARLP 断点缺少任务 ID，无法续等")
        stage_index = max(stage_number - 1, 0)
        task_list_page = await material_page.context.new_page()
        self._wrap_page_speed(task_list_page)
        task_page = task_list_page
        try:
            await self._open_work_order_management(task_list_page, progress)
            task_page = await self._open_task_detail_without_wait(task_list_page, task_id)
            result = await self._wait_redfruit_arlp_task_result(
                task_page,
                progress,
                "增加 ARLP",
                stage_config=stage_config,
                expected_count=len(items),
                verification_cids=[item.cid for item in items],
            )
        except Exception as exc:  # noqa: BLE001
            self._update_arlp_progress(
                plan,
                stage_index,
                stage_config=stage_config,
                status="failed",
                step="waiting_result_failed",
                task_id=task_id,
                last_error=str(exc),
                message=f"ARLP【{stage_config.get('name') or stage_number}】续等任务失败：{exc}",
            )
            raise
        finally:
            for candidate in {task_page, task_list_page}:
                try:
                    if candidate and not candidate.is_closed():
                        await candidate.close()
                except Exception:
                    pass
        result_total = int(result.get("total") or 0)
        result_success = int(result.get("success") or 0)
        result_failed = int(result.get("failed") or 0)
        self._update_arlp_progress(
            plan,
            stage_index,
            stage_config=stage_config,
            status=("success" if self._redfruit_operation_all_expected_success(result, len(items)) else "partial_failure"),
            step="result_received",
            task_id=str(result.get("task_id") or task_id),
            total=result_total,
            success=result_success,
            failed=result_failed,
            message=(
                f"断点恢复收到 ARLP【{stage_config.get('name') or stage_number}】任务结果："
                f"成功 {result_success}/{result_total}，失败 {result_failed}"
            ),
        )
        return result

    async def _wait_redfruit_arlp_task_page(
            self,
            material_page,
            before_pages: list,
            before_url: str,
            progress: ProgressCallback | None,
    ):
        """等待 ARLP 成功提示中的「查看任务详情」打开操作任务页。"""
        before_ids = {id(candidate) for candidate in before_pages}
        details_clicked = False
        attempt = 0
        while True:
            self._raise_if_cancelled()
            attempt += 1

            for candidate in reversed(material_page.context.pages):
                if candidate.is_closed():
                    continue
                candidate_url = str(candidate.url or "")
                is_new_page = id(candidate) not in before_ids
                is_current_task_page = candidate is material_page and candidate_url != before_url
                if "/aigc/manage/task" not in candidate_url or not (is_new_page or is_current_task_page):
                    continue
                try:
                    await candidate.wait_for_load_state("domcontentloaded", timeout=1000)
                except Exception:
                    pass
                self._wrap_page_speed(candidate)
                return candidate

            if not details_clicked:
                locators = (
                    material_page.get_by_role("button", name="查看任务详情", exact=True).first,
                    material_page.get_by_text("查看任务详情", exact=True).first,
                    material_page.locator("button:has-text('查看任务详情')").first,
                )
                if await self._click_first_visible_locator(*locators):
                    details_clicked = True
                    self._emit(progress, "增加 ARLP 任务已创建，打开任务详情等待处理结果")

            if attempt % 10 == 0:
                self._emit(progress, f"等待增加 ARLP 任务详情页，第 {attempt} 次")
            await self._sleep(1)

    async def _wait_redfruit_arlp_task_result(
            self,
            task_page,
            progress: ProgressCallback | None,
            operation_name: str = "增加 ARLP",
            *,
            stage_config: dict[str, list[str] | str] | None = None,
            expected_count: int | None = None,
            verification_cids: Iterable[str] | None = None,
    ) -> dict[str, int | str]:
        """持续刷新红果操作任务，直到明确得到成功或部分失败结果。"""
        task_id = await self._read_current_task_id(task_page, progress)
        interval_ms = max(int(self.refresh_interval_seconds * 500), 3000)
        attempt = 0
        task_failure_retries = 0
        while True:
            self._raise_if_cancelled()
            attempt += 1
            row = await self._find_task_row(task_page, task_id)
            progress_data = await self._read_redfruit_arlp_task_progress(row)
            if progress_data:
                progress_data["task_id"] = task_id
                total = int(progress_data["total"])
                success = int(progress_data["success"])
                failed = int(progress_data["failed"])
                status = _compact_text(str(progress_data["status"]))
                if total > 0 and success >= total:
                    return progress_data
                task_failed = (
                    "已失败" in status
                    or (total > 0 and success == 0 and failed >= total and "失败" in status)
                )
                if task_failed:
                    if (
                            self._is_redfruit_second_arlp_stage(stage_config)
                            and await self._verify_redfruit_second_arlp_detail_success(
                                task_page,
                                task_id,
                                progress,
                                verification_cids,
                            )
                    ):
                        confirmed_total = max(int(expected_count or 0), 1)
                        self._emit(
                            progress,
                            f"红果短剧 ARLP 第二阶段任务 {task_id} 总状态失败，但素材详情中"
                            "短剧端原生IAA(duanju1)-头条内广已成功，按第二阶段成功继续",
                        )
                        return {
                            "status": "详情确认第二阶段成功",
                            "total": confirmed_total,
                            "success": confirmed_total,
                            "failed": 0,
                            "task_id": task_id,
                        }
                    if task_failure_retries >= 3:
                        raise RuntimeError(
                            f"{operation_name}任务 {task_id} 重试 3 次后仍失败："
                            f"成功 {success}/{total}，失败 {failed}"
                        )
                    task_failure_retries += 1
                    await self._retry_failed_operation_task(
                        task_page,
                        task_id,
                        progress,
                        operation_name=operation_name,
                        retry_number=task_failure_retries,
                    )
                    continue
                if total > 0 and success + failed >= total:
                    return progress_data
                if failed > 0 and any(token in status for token in ("失败", "部分")):
                    return progress_data

            if attempt % 10 == 0:
                self._emit(progress, f"等待{operation_name}任务 {task_id} 完成，第 {attempt} 次")
            await self._click_if_present(task_page, "刷新列表")
            await task_page.wait_for_timeout(interval_ms)
            await self._search_task_by_id(task_page, task_id)

    @staticmethod
    def _is_redfruit_second_arlp_stage(
            stage_config: dict[str, list[str] | str] | None,
    ) -> bool:
        """只识别短剧端原生 IAA + 头条内广这一段 ARLP。"""
        if not isinstance(stage_config, dict):
            return False
        products = {
            _compact_text(str(value))
            for value in (stage_config.get("products") or [])
            if str(value).strip()
        }
        platforms = {
            _compact_text(str(value))
            for value in (stage_config.get("platforms") or [])
            if str(value).strip()
        }
        return products == {"短剧端原生IAA(796433)"} and platforms == {"头条内广"}

    @staticmethod
    def _redfruit_second_arlp_detail_text_is_success(text: str) -> bool:
        """判断目标 IAA 审核项自身是否明确成功，避免借用其他产品状态。"""
        compact = _compact_cascader_text(text).lower()
        if not all(token in compact for token in ("短剧端原生iaa", "duanju1", "头条内广")):
            return False
        failed_markers = (
            "被拒",
            "失败",
            "未通过",
            "不通过",
            "不能新建广告",
            "不可新建广告",
            "审核拒绝",
        )
        if any(marker in compact for marker in failed_markers):
            return False
        return any(
            marker in compact
            for marker in ("过审", "成功", "审核通过", "可以新建广告")
        )

    async def _verify_redfruit_second_arlp_detail_success(
            self,
            task_page,
            task_id: str,
            progress: ProgressCallback | None,
            verification_cids: Iterable[str] | None = None,
    ) -> bool:
        """任务总状态失败时，从任意单个素材详情核验第二阶段目标审核项。"""
        before_url = str(task_page.url or "")
        material_page = None
        verification_cid = next(
            (
                str(cid or "").strip().lower()
                for cid in (verification_cids or [])
                if str(cid or "").strip()
            ),
            "",
        )
        if not verification_cid:
            self._emit(progress, f"ARLP 第二阶段任务 {task_id} 缺少素材 CID，无法进入素材详情核验")
            return False
        try:
            self._emit(
                progress,
                f"ARLP 第二阶段任务 {task_id} 显示失败，进入素材管理抽查 CID {verification_cid}",
            )
            material_page = await self._open_material_management_page(task_page)
            await self._search_redfruit_materials_by_cids(
                material_page,
                [verification_cid],
                progress,
                wait_on_empty=True,
            )
            await self._open_redfruit_material_detail(material_page, verification_cid)
            target_pattern = re.compile(r"短剧端原生IAA.*头条内广", flags=re.IGNORECASE)
            audit_expanded = False

            async def find_target_success() -> bool:
                nonlocal audit_expanded
                drawer = material_page.locator(".arco-drawer").first
                targets = drawer.get_by_text(target_pattern)
                try:
                    target_count = min(await targets.count(), 20)
                except Exception:
                    target_count = 0
                for index in range(target_count):
                    target = targets.nth(index)
                    try:
                        await target.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    try:
                        if not await target.is_visible():
                            continue
                    except Exception:
                        continue
                    scope_text = await self._redfruit_arlp_status_scope_text(target)
                    if self._redfruit_second_arlp_detail_text_is_success(scope_text):
                        return True

                if not audit_expanded:
                    audit_header = drawer.get_by_text("审核信息", exact=True).first
                    try:
                        if await audit_header.count() and await audit_header.is_visible():
                            await self._click_locator(audit_header)
                            await material_page.wait_for_timeout(400)
                    except Exception:
                        pass
                    audit_expanded = True

                await self._scroll_redfruit_material_detail_down(drawer)
                return False

            confirmed = bool(
                await self._wait_for_result(find_target_success, timeout_ms=20000, interval_ms=600)
            )
            if confirmed:
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] redfruit ARLP stage 2 detail fallback: "
                    f"task_id={task_id}, cid={verification_cid}, "
                    "target=短剧端原生IAA(duanju1)-头条内广, result=success"
                )
                self._write_event(
                    "redfruit_arlp_stage_2_detail_confirmed",
                    task_id=task_id,
                    cid=verification_cid,
                    target="短剧端原生IAA(duanju1)-头条内广",
                )
                return True

            await self._snapshot_error(
                material_page,
                "redfruit_arlp_stage_2_detail_not_success",
                extra=(
                    f"task_id={task_id}, cid={verification_cid}, "
                    "target=短剧端原生IAA(duanju1)-头条内广"
                ),
            )
            self._emit(
                progress,
                f"ARLP 第二阶段任务 {task_id} 的素材详情未确认目标审核项过审，继续原重试逻辑",
            )
            return False
        except Exception as exc:  # noqa: BLE001
            if self._is_recoverable_session_exception(exc):
                raise
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] redfruit ARLP stage 2 detail fallback failed: "
                f"task_id={task_id}, error={exc}"
            )
            self._emit(progress, f"ARLP 第二阶段任务 {task_id} 素材详情核验失败，继续原重试逻辑：{exc}")
            return False
        finally:
            await self._restore_task_page_after_arlp_detail(task_page, material_page, before_url)

    async def _open_redfruit_material_detail(self, page, cid: str) -> None:
        """点击单个红果素材卡片正文，并等待包含审核信息的详情抽屉。"""
        await self._wait_material_items_ready(page, [])
        card_selectors = (
            ".waterfall-item",
            ".common-card-item-eVVh45",
            "[class*='waterfall-item']",
            "[class*='common-card-item']",
            "[class*='material-card']",
            "[class*='material-item']",
        )
        while True:
            self._raise_if_cancelled()
            for selector in card_selectors:
                for card in await self._visible_locators(page.locator(selector), limit=20):
                    try:
                        await card.scroll_into_view_if_needed(timeout=3000)
                        box = await card.bounding_box()
                        if not box:
                            continue
                        await page.mouse.click(
                            box["x"] + box["width"] * 0.45,
                            box["y"] + box["height"] * 0.45,
                        )
                    except Exception:
                        continue

                    async def detail_ready() -> bool:
                        drawer = page.locator(".arco-drawer").first
                        try:
                            if not await drawer.count() or not await drawer.is_visible():
                                return False
                        except Exception:
                            return False
                        body = await self._locator_text(drawer, timeout_ms=2000)
                        return cid.lower() in body.lower() and "审核信息" in body

                    if await self._wait_for_result(detail_ready, timeout_ms=5000, interval_ms=300):
                        return
            await page.wait_for_timeout(800)

    async def _scroll_redfruit_material_detail_down(self, drawer) -> bool:
        """向下推进素材详情内所有可滚动区域，直至审核项进入 DOM/可视区。"""
        try:
            return bool(
                await drawer.evaluate(
                    """node => {
                        const candidates = [node, ...node.querySelectorAll('*')]
                            .filter(el => el.scrollHeight > el.clientHeight + 8)
                            .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                        let moved = false;
                        for (const el of candidates) {
                            const before = el.scrollTop;
                            const step = Math.max(Math.floor(el.clientHeight * 0.7), 240);
                            el.scrollTop = Math.min(el.scrollTop + step, el.scrollHeight - el.clientHeight);
                            if (el.scrollTop > before) {
                                el.dispatchEvent(new Event('scroll', { bubbles: true }));
                                moved = true;
                            }
                        }
                        return moved;
                    }"""
                )
            )
        except Exception as exc:
            if self._is_recoverable_session_exception(exc):
                raise
            return False

    async def _redfruit_arlp_status_scope_text(self, target) -> str:
        """读取包含目标 ARLP 名称和其局部状态的最小父节点文本。"""
        try:
            return await target.evaluate(
                """node => {
                    const statusTokens = [
                        '过审', '成功', '审核通过', '可以新建广告',
                        '被拒', '失败', '未通过', '不通过', '不能新建广告', '不可新建广告'
                    ];
                    let current = node;
                    while (current && current !== document.body) {
                        const text = (current.innerText || current.textContent || '').trim();
                        if (text.includes('短剧端原生IAA') && text.includes('头条内广') &&
                                statusTokens.some(token => text.includes(token)) && text.length <= 1200) {
                            return text;
                        }
                        current = current.parentElement;
                    }
                    return (node.innerText || node.textContent || '').trim();
                }"""
            )
        except Exception as exc:
            if self._is_recoverable_session_exception(exc):
                raise
            return await self._locator_text(target, timeout_ms=2000)

    async def _restore_task_page_after_arlp_detail(self, task_page, material_page, before_url: str) -> None:
        """关闭素材详情并回任务页，保证失败时仍可继续原重试逻辑。"""
        if material_page is None:
            return
        try:
            if material_page is not task_page:
                if not material_page.is_closed():
                    await self._close_page_intentionally(material_page)
                return
            if await self._close_redfruit_material_drawer(task_page):
                await task_page.wait_for_timeout(300)
            if str(task_page.url or "") != before_url:
                await self._safe_goto(task_page, before_url)
        except Exception as exc:
            if self._is_recoverable_session_exception(exc):
                raise

    async def _read_redfruit_arlp_task_progress(self, row) -> dict[str, int | str] | None:
        """读取 ARLP 任务行的状态、总数、成功数和失败数。"""
        if not row:
            return None
        cells = row.locator("td")
        try:
            count = await cells.count()
        except Exception:
            return None
        if count < 8:
            return None

        values: list[str] = []
        for index in range(min(count, 10)):
            values.append(_compact_text(await self._locator_text(cells.nth(index), timeout_ms=2000)))

        def parse_count(value: str) -> int:
            return int(value) if re.fullmatch(r"\d+", value or "") else -1

        total = parse_count(values[5])
        success = parse_count(values[6])
        failed = parse_count(values[7])
        if min(total, success, failed) < 0:
            return None
        return {
            "status": values[3] if len(values) > 3 else "",
            "total": total,
            "success": success,
            "failed": failed,
        }

    async def _close_redfruit_result_dialog(self, page) -> None:
        """关闭 ARLP 任务创建成功提示，避免遮挡下一轮素材选择。"""
        buttons = page.get_by_role("button", name="Close", exact=True)
        try:
            count = await buttons.count()
        except Exception:
            return
        for index in range(count - 1, -1, -1):
            locator = buttons.nth(index)
            try:
                if await locator.is_visible() and await self._click_locator(locator):
                    await page.wait_for_timeout(300)
                    return
            except Exception:
                continue

    async def _clear_redfruit_material_selection(
            self,
            page,
            progress: ProgressCallback | None,
    ) -> None:
        """重试 ARLP 前清空上一轮选择，保证下一轮重新点卡片再全选。"""
        attempt = 0
        while True:
            self._raise_if_cancelled()
            selected = await self._selected_count(page)
            if selected <= 0:
                return
            attempt += 1
            if await self._click_text_or_locator(page, "取消全选"):
                await self._wait_selected_count(page, 0, timeout_ms=None)
                return
            if attempt % 10 == 0:
                self._emit(progress, f"等待清空上一轮素材选择，当前已选择 {selected} 项，第 {attempt} 次")
            await self._sleep(1)

    async def _ensure_redfruit_post_review_classifications_all_success(
            self,
            material_page,
            plan: UserGrowthOrderPlan,
            items: list[UserGrowthVideoItem],
            progress: ProgressCallback | None,
    ) -> None:
        """修改红果后审分类标签，并持续补改直到每条素材都成功。"""
        self._assert_redfruit_state_machine(plan)
        if not items:
            return

        item = items[0]
        paths = [
            list(path)
            for path in (
                item.post_review_classification_paths
                or item.workflow_metadata.get("post_review_classification_paths", [])
            )
            if path
        ]
        if not paths:
            self._emit(progress, "红果短剧未配置后审分类标签路径，跳过修改分类标签任务")
            return

        attempt = 0
        while True:
            self._raise_if_cancelled()
            attempt += 1
            if attempt > 1:
                await self._clear_redfruit_material_selection(material_page, progress)
                await self._refresh_material_list_page(material_page)
                self._emit(
                    progress,
                    f"红果短剧修改分类标签第 {attempt} 轮：重新点第一条素材并全选所有",
                )

            await self._wait_material_items_ready(material_page, items)
            await self._select_all_materials(material_page, items)
            await self._open_redfruit_post_review_classification_modal(
                material_page,
                progress,
                required_fields=[str(path[0]) for path in paths if path],
                items=items,
            )
            await self._fill_redfruit_post_review_classifications(
                material_page,
                plan,
                items,
                progress,
            )

            self._update_classification_progress(
                plan,
                status="saving",
                step="save_pending",
                save_status="pending",
                checkpoint_stage="classification_submitting",
                message="红果素材分类标签已填写，准备保存",
            )
            await self._click_redfruit_modal_action(material_page, ("保存", "确定", "确认"))
            plan.classification_task_id = ""
            await self._close_redfruit_result_dialog(material_page)
            self._update_classification_progress(
                plan,
                status="success",
                step="save_completed",
                save_status="success",
                checkpoint_stage="classification_success",
                message="红果素材分类标签保存成功",
            )
            self._emit(
                progress,
                f"红果短剧修改分类标签已保存：本批 {len(items)} 条，不进入任务详情页",
            )
            return

    @staticmethod
    def _redfruit_operation_all_expected_success(
            result: dict[str, int | str],
            expected_count: int,
    ) -> bool:
        """只有任务总数和成功数都覆盖本批预期素材，才允许进入下一阶段。"""
        expected = max(int(expected_count or 0), 0)
        total = int(result.get("total") or 0)
        success = int(result.get("success") or 0)
        failed = int(result.get("failed") or 0)
        return expected > 0 and total == expected and success == expected and failed == 0

    async def _fill_redfruit_post_review_classifications(
            self,
            page,
            plan: UserGrowthOrderPlan,
            items: list[UserGrowthVideoItem],
            progress: ProgressCallback | None = None,
    ) -> None:
        """填写红果 ARLP 后需要补充的素材分类标签，失败时刷新并指数退避重试。"""
        self._assert_redfruit_state_machine(plan)
        if not items:
            return

        item = items[0]
        paths = [
            list(path)
            for path in (item.post_review_classification_paths or item.workflow_metadata.get("post_review_classification_paths", []))
            if path
        ]
        if not paths:
            return

        attempt = 0
        saved_attempt = int(plan.classification_progress.get("attempt") or 0)
        attempt = saved_attempt
        pending_refresh_delay = 0.0
        next_backoff_seconds = 1.0
        while True:
            self._raise_if_cancelled()
            attempt += 1
            self._update_classification_progress(
                plan,
                status="filling",
                step="fields_started",
                field_index=int(plan.classification_progress.get("field_index") or 0),
                attempt=attempt,
                save_status="pending",
                checkpoint_stage="classification_submitting",
                message=f"红果短剧分类标签第 {attempt} 轮开始填写",
            )
            try:
                if pending_refresh_delay > 0:
                    self._emit(
                        progress,
                        f"红果短剧修改分类标签未命中，{pending_refresh_delay:.1f}s 后刷新重试，第 {attempt} 次",
                    )
                    await self._refresh_material_list_page(
                        page,
                        settle_delay_seconds=pending_refresh_delay,
                        force_reload=True,
                    )
                    await self._select_all_materials(page, items)
                    await self._open_redfruit_post_review_classification_modal(
                        page,
                        progress,
                        items=items,
                    )
                    pending_refresh_delay = 0.0

                await self._fill_redfruit_post_review_classifications_once(page, plan, paths)
                return
            except Exception as exc:  # noqa: BLE001
                if self._is_target_closed_exception(exc):
                    raise
                self._emit(
                    progress,
                    f"红果短剧修改分类标签失败，第 {attempt} 次：{exc}",
                )
                self._update_classification_progress(
                    plan,
                    status="failed",
                    step="field_failed",
                    attempt=attempt,
                    save_status="pending",
                    last_error=str(exc),
                    message=f"红果短剧分类标签第 {attempt} 轮失败：{exc}",
                )
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] redfruit post-review classification retry "
                    f"attempt={attempt}, next_backoff={next_backoff_seconds:.1f}s, error={exc}"
                )
                if attempt <= 2 or attempt % 3 == 0:
                    await self._snapshot_error(
                        page,
                        f"redfruit_post_review_classification_retry_{attempt}",
                        exc=exc,
                    )
                pending_refresh_delay = next_backoff_seconds
                next_backoff_seconds = min(next_backoff_seconds * 2, 30.0)

    async def _fill_redfruit_post_review_classifications_once(
            self,
            page,
            plan: UserGrowthOrderPlan,
            paths: list[list[str]],
    ) -> None:
        """在已打开的修改分类标签弹窗中填写一次红果后审分类标签。"""
        saved_field_index = max(int(plan.classification_progress.get("field_index") or 0), 0)
        for index, path in enumerate(paths):
            if not path:
                continue
            field_name = str(path[0])
            self._update_classification_progress(
                plan,
                status="filling",
                step="field_started",
                field_index=index,
                field_name=field_name,
                field_path=path,
                save_status="pending",
                message=f"开始填写红果分类字段 {index + 1}/{len(paths)}：{field_name}",
            )
            if index < saved_field_index and await self._redfruit_classification_field_has_value(
                    page,
                    field_name,
                    str(path[-1]),
            ):
                self._update_classification_progress(
                    plan,
                    status="filling",
                    step="field_verified",
                    field_index=index + 1,
                    field_name=field_name,
                    field_path=path,
                    save_status="pending",
                    message=f"断点恢复确认分类字段已存在：{field_name}",
                )
                continue
            await self._select_cascader(
                page,
                field_name,
                list(path),
                field_timeout_ms=None,
                prefer_modal_bottom=True,
            )
            self._update_classification_progress(
                plan,
                status="filling",
                step="field_completed",
                field_index=index + 1,
                field_name=field_name,
                field_path=path,
                save_status="pending",
                last_error="",
                message=f"已完成红果分类字段 {index + 1}/{len(paths)}：{field_name}",
            )

    async def _redfruit_classification_field_has_value(
            self,
            page,
            field_name: str,
            final_value: str,
    ) -> bool:
        """判断分类字段当前是否已经回显目标叶子值，用于恢复时安全跳过。"""
        trigger = await self._cascader_trigger_for_field(
            page,
            field_name,
            prefer_modal_bottom=True,
            field_timeout_ms=None,
        )
        if not trigger:
            return False
        return await self._cascader_field_has_value(page, trigger, field_name, final_value)

    async def _select_all_materials(self, page, items: list[UserGrowthVideoItem]) -> None:
        """红果素材管理页先点一条素材进入选择模式，再全选全部素材。"""
        await self._wait_material_items_ready(page, items)
        if await self._selected_count(page) > 0:
            await self._clear_redfruit_material_selection(page, None)
        if not await self._click_first_material_card(page, items):
            await self._snapshot_error(page, "redfruit_material_first_card_not_selected")
            raise RuntimeError("未选中素材列表中的第一条素材")
        await self._wait_redfruit_material_selection_bar_visible(page)
        if not await self._select_redfruit_all_materials(page, len(items)):
            await self._snapshot_error(page, "redfruit_material_select_all_failed")
            raise RuntimeError("红果素材列表全选失败")

        async def selected_enough() -> bool:
            return await self._selected_count(page) == len(items)

        await self._wait_for_result(
            selected_enough,
            timeout_ms=None,
            interval_ms=500,
        )
        await page.wait_for_timeout(1200)

    async def _wait_material_items_ready(self, page, items: list[UserGrowthVideoItem]) -> None:
        """等待素材/文案列表出现可操作的素材项，并周期性纠正页面状态。"""
        tokens = self._material_card_tokens(items)
        attempt = 0

        async def material_ready() -> bool:
            body = await self._body_text(page, timeout_ms=2000)
            compact_body = _compact_text(body)
            material_markers = (
                "素材/文案列表",
                "素材管理",
                "全局搜索",
                "全选所有",
                "增加ARLP",
            )
            if not any(marker in compact_body for marker in material_markers):
                return False
            if any(token and token in compact_body for token in tokens):
                return True
            item_count = await page.locator(
                ".arco-table-tbody tr:visible, .arco-table-tr:visible, [role='row']:visible, "
                ".waterfall-item:visible, [class*='waterfall-item']:visible, "
                ".common-card-item-eVVh45:visible, [class*='common-card-item']:visible, "
                ".arco-card:visible, [class*='material-card']:visible, [class*='material-item']:visible, "
                "[class*='creative-card']:visible"
            ).count()
            return item_count > 0 and "暂无数据" not in compact_body

        while not await material_ready():
            self._raise_if_cancelled()
            attempt += 1
            if attempt == 1 or attempt % 10 == 0:
                body = _compact_text(await self._body_text(page, timeout_ms=2000))
                item_count = await page.locator(
                    ".arco-table-tbody tr:visible, .arco-table-tr:visible, [role='row']:visible, "
                    ".waterfall-item:visible, [class*='waterfall-item']:visible, "
                    ".common-card-item-eVVh45:visible, [class*='common-card-item']:visible, "
                    ".arco-card:visible, [class*='material-card']:visible, [class*='material-item']:visible"
                ).count()
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] waiting material list items, "
                    f"attempt={attempt}, url={page.url}, item_count={item_count}, "
                    f"body_tail={body[-240:]}"
                )
            if attempt % 20 == 0:
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] "
                    "material list still not actionable; reload current material page"
                )
                await self._refresh_material_list_page(page, force_reload=True)
            await page.wait_for_timeout(1500)

    @staticmethod
    def _material_card_tokens(items: list[UserGrowthVideoItem]) -> list[str]:
        """生成素材卡片可见文件名 token。"""
        tokens: list[str] = []
        for item in items:
            file_name = str(item.file_name or "").strip()
            stem = Path(file_name).stem.strip()
            for token in (file_name, stem, stem[:32], stem[:22], stem[:14]):
                compact = _compact_text(token)
                if len(compact) >= 4 and compact not in tokens:
                    tokens.append(compact)
        return tokens

    async def _select_all_visible_pages(self, page) -> None:
        """把当前页及后续分页里的所有可选项都选上。"""
        await self._set_page_size_max(page)
        selected_any = False
        seen_pages: set[str] = set()
        while True:
            wait_attempt = 0
            while True:
                self._raise_if_cancelled()
                if await self._click_table_select_all_now(page):
                    selected_any = True
                    break
                if await self._click_text_or_locator(page, "全选"):
                    selected_any = True
                    break
                wait_attempt += 1
                if wait_attempt == 1 or wait_attempt % 10 == 0:
                    self._write_run_log(
                        f"[{datetime.now().isoformat(timespec='seconds')}] "
                        f"waiting selectable list items, attempt={wait_attempt}"
                    )
                await self._sleep(1)
            await page.wait_for_timeout(600)
            page_signature = await self._material_page_signature(page)
            if page_signature and page_signature in seen_pages:
                break
            if page_signature:
                seen_pages.add(page_signature)
            if not await self._click_pagination_next(page):
                break
            await page.wait_for_timeout(1000)
        if not selected_any:
            raise RuntimeError("未完成列表全选")

    async def _wait_redfruit_material_drawer_visible(self, page) -> bool:
        """等待红果素材列表右侧抽屉出现。"""
        async def drawer_visible() -> bool:
            drawer = page.locator(".arco-drawer").first
            try:
                return bool(await drawer.count() and await drawer.is_visible())
            except Exception:
                return False

        return bool(await self._wait_for_result(drawer_visible, timeout_ms=None, interval_ms=400))

    async def _material_page_signature(self, page) -> str:
        """读取素材管理页可见内容签名，用于避免分页重复循环。"""
        rows = page.locator(
            ".arco-table-tbody tr, .arco-table-tr, [role='row'], "
            ".arco-card, [class*='material-card'], [class*='material-item']"
        )
        texts: list[str] = []
        for row in await self._visible_locators(rows, limit=80):
            text = _compact_text(await self._locator_text(row, timeout_ms=1000))
            if text:
                texts.append(text[:80])
        return "|".join(texts)

    async def _open_material_management_page(
            self,
            page,
            progress: ProgressCallback | None = None,
    ):
        """从当前墨攻AI页面侧栏打开素材管理页。"""
        self._emit(progress, "进入素材管理")
        await self._click_text(page, "素材管理")

        async def material_page_ready():
            for candidate in reversed(page.context.pages):
                if candidate.is_closed():
                    continue
                try:
                    await candidate.wait_for_load_state("domcontentloaded", timeout=1000)
                except Exception:
                    pass
                body = await self._body_text(candidate, timeout_ms=2000)
                if "素材管理" in body and "全部素材" in body and ("全局搜索" in body or "上传素材" in body):
                    self._wrap_page_speed(candidate)
                    return candidate
            return None

        wait_attempt = 0
        while True:
            self._raise_if_cancelled()
            material_page = await material_page_ready()
            if material_page:
                self._emit(progress, "已进入素材管理")
                return material_page
            wait_attempt += 1
            if wait_attempt == 1 or wait_attempt % 10 == 0:
                body = await self._body_text(page, timeout_ms=3000)
                explicit_error = self._explicit_page_error_marker(body)
                self._emit(progress, f"等待素材管理页面加载，保持当前页面（第 {wait_attempt} 次）")
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] waiting material page: "
                    f"attempt={wait_attempt}, explicit_error={explicit_error or 'none'}, url={page.url}"
                )
                if explicit_error:
                    self._emit(progress, f"素材管理页面明确返回{explicit_error}，刷新当前页面后继续等待")
                    try:
                        await page.reload(wait_until="domcontentloaded")
                    except Exception as reload_exc:
                        if self._is_recoverable_session_exception(reload_exc):
                            raise
            await self._sleep(0.8)

    async def _open_redfruit_material_management_by_cids(
            self,
            page,
            items: list[UserGrowthVideoItem],
            progress: ProgressCallback | None,
    ):
        """断点恢复时直接进入素材管理，并按 checkpoint CID 定位本批素材。"""
        cids = [str(item.cid or "").strip().lower() for item in items if item.status != "skipped"]
        if not cids or not all(cids) or len(set(cids)) != len(cids):
            raise RuntimeError("红果断点恢复缺少完整且唯一的 CID，不能直接进入素材管理")
        if len(cids) > 50:
            raise RuntimeError(f"红果断点恢复单批 CID 超过素材管理搜索上限：{len(cids)} > 50")

        await self._open_work_order_management(page, progress)
        self._emit(progress, f"进入素材管理，按 {len(cids)} 个 CID 搜索本批素材")
        material_page = await self._open_material_management_page(page)
        await self._search_redfruit_materials_by_cids(
            material_page,
            cids,
            progress,
            wait_on_empty=True,
        )
        return material_page

    async def _search_redfruit_materials_by_cids(
            self,
            page,
            cids: list[str],
            progress: ProgressCallback | None,
            *,
            wait_on_empty: bool = False,
            clear_query_scope: bool = False,
    ) -> int:
        """用空格分隔的 CID 精确筛选素材管理页，并等待命中数覆盖整批。"""
        expected = {str(cid or "").strip().lower() for cid in cids if str(cid or "").strip()}
        if not expected:
            raise RuntimeError("素材管理 CID 搜索条件为空")

        # 先打开不带旧关键字的素材管理页。番茄音乐要求清除默认的“同公司”
        # 先进入无 q 参数的素材页；番茄必须在填入 CID 后再取消“同公司”，
        # 因为该筛选值可能随 CID 输入异步渲染。红果调用保持既有筛选上下文。
        parts = urlsplit(str(page.url or ""))
        params = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key != "q"
        ]
        material_url = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment)
        )
        await self._safe_goto(page, material_url)
        await page.wait_for_timeout(1000)
        query = " ".join(cids)
        search_input = await self._wait_first_existing(
            page,
            (
                "input[placeholder*='关键字搜索']",
                "input[placeholder*='全局搜索']",
                "input[placeholder*='关键字']",
            ),
            timeout_ms=None,
        )
        if not search_input:
            await self._snapshot_error(page, "material_cid_search_input_missing")
            raise RuntimeError("素材管理未找到关键字搜索框")
        await self._type_into_locator(search_input, page, query)
        self._emit(progress, f"已输入 {len(expected)} 个空格分隔 CID")
        if clear_query_scope:
            await self._clear_material_query_scope(page, progress)
        try:
            await search_input.press("Enter")
        except Exception:
            await page.keyboard.press("Enter")
        await page.wait_for_timeout(1200)

        async def reapply_query() -> None:
            """刷新素材页后重新提交本批 CID，保持番茄/红果搜索上下文。"""
            await self._safe_goto(page, material_url)
            await page.wait_for_timeout(1000)
            input_box = await self._wait_first_existing(
                page,
                (
                    "input[placeholder*='关键字搜索']",
                    "input[placeholder*='全局搜索']",
                    "input[placeholder*='关键字']",
                ),
                timeout_ms=None,
            )
            if not input_box:
                raise RuntimeError("素材管理恢复后未找到关键字搜索框")
            await self._type_into_locator(input_box, page, query)
            if clear_query_scope:
                await self._clear_material_query_scope(page, progress)
            try:
                await input_box.press("Enter")
            except Exception:
                await page.keyboard.press("Enter")

        attempt = 0
        recovery_delay_seconds = 2.0
        while True:
            self._raise_if_cancelled()
            attempt += 1
            body = await self._body_text(page, timeout_ms=3000)
            body_cids = set(self._extract_cids(body))
            result_counts = [
                int(value.replace(",", ""))
                for value in re.findall(r"共\s*([0-9,]+)\s*条", body)
            ]
            matched_count = result_counts[-1] if result_counts else 0
            exact_count_ready = matched_count == len(expected)
            visible_cids_ready = expected.issubset(body_cids)
            if exact_count_ready or visible_cids_ready:
                # 计数/CID 文本可能先于实际列表渲染；必须等待真实素材项出现，
                # 再进入选中和批量编辑，避免把“暂无数据”误判成可操作结果。
                item_count = await page.locator(
                    ".arco-table-tbody tr:visible, .arco-table-tr:visible, [role='row']:visible, "
                    ".waterfall-item:visible, [class*='waterfall-item']:visible, "
                    ".common-card-item-eVVh45:visible, [class*='common-card-item']:visible, "
                    ".arco-card:visible, [class*='material-card']:visible, [class*='material-item']:visible, "
                    "[class*='creative-card']:visible"
                ).count()
                if item_count and "暂无数据" not in _compact_text(body):
                    self._emit(progress, f"素材管理 CID 搜索已命中本批 {len(expected)} 条素材")
                    return matched_count or len(expected)

            if matched_count > len(expected):
                await self._snapshot_error(
                    page,
                    "redfruit_material_cid_search_too_broad",
                    extra=f"expected={len(expected)}, matched={matched_count}",
                )
                raise RuntimeError(
                    f"素材管理 CID 搜索结果过多：期望 {len(expected)} 条，实际 {matched_count} 条，已停止全选"
                )
            if "暂无数据" in body and attempt >= 5 and not wait_on_empty:
                await self._snapshot_error(
                    page,
                    "redfruit_material_cid_search_empty",
                    extra=f"expected={len(expected)}",
                )
                raise RuntimeError(f"素材管理未检索到本批 {len(expected)} 个 CID")

            explicit_error = self._explicit_page_error_marker(body)
            if explicit_error:
                self._emit(
                    progress,
                    f"素材管理明确返回{explicit_error}，{recovery_delay_seconds:.0f}s 后恢复同一 CID 搜索",
                )
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] material CID search recovery: "
                    f"reason={explicit_error}, delay={recovery_delay_seconds}, url={page.url}"
                )
                await self._sleep(recovery_delay_seconds)
                await reapply_query()
                recovery_delay_seconds = min(recovery_delay_seconds * 2, 30.0)
                continue

            # 平台偶发只更新 URL/筛选条件而未触发素材列表请求；低频刷新并重提，
            # 避免白屏长期卡住，同时不引入高频刷新。
            if attempt >= 60 and attempt % 60 == 0:
                self._emit(progress, f"素材管理连续 {attempt} 次无结果，刷新当前页并重新提交 CID")
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] material CID search periodic recovery: "
                    f"attempt={attempt}, expected={len(expected)}, url={page.url}"
                )
                await self._sleep(2.0)
                await reapply_query()
                continue

            if attempt == 1 or attempt % 10 == 0:
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] redfruit material CID search wait: "
                    f"attempt={attempt}, expected={len(expected)}, matched={matched_count}, "
                    f"visible_cids={len(expected.intersection(body_cids))}, counts={result_counts}, "
                    f"url={page.url}"
                )
                self._emit(
                    progress,
                    f"等待素材管理返回本批 {len(expected)} 条结果，第 {attempt} 次",
                )
            await self._sleep(1)

    async def _clear_material_query_scope(
            self,
            page,
            progress: ProgressCallback | None = None,
    ) -> bool:
        """点击两次“同公司”（中间留短间隔）并验证查询范围已取消。"""
        value_selectors = (
            ".arco-select-view-value",
            ".arco-select-view-tag",
            ".arco-input-tag-tag",
            ".arco-tag",
            "[class*='selection-item']",
            "[class*='selected-value']",
        )

        async def scope_has_selected_value(container) -> bool:
            for selector in value_selectors:
                for value in await self._visible_locators(container.locator(selector), limit=30):
                    text = _compact_text(await self._locator_text(value, timeout_ms=1000))
                    if text and text not in {"查询范围", "请选择", "全部"}:
                        return True
            inputs = container.locator("input")
            for input_box in await self._visible_locators(inputs, limit=10):
                try:
                    if str(await input_box.input_value(timeout=1000) or "").strip():
                        return True
                except Exception:
                    continue
            return False

        async def find_selected_same_company():
            candidates = page.get_by_text(re.compile(r"^\s*同公司\s*$"), exact=False)
            for candidate in await self._visible_locators(candidates, limit=50):
                containers = (
                    candidate.locator("xpath=ancestor::*[contains(@class,'arco-select-view')][1]"),
                    candidate.locator("xpath=ancestor::*[contains(@class,'arco-input-tag')][1]"),
                )
                for container in containers:
                    try:
                        if await container.count() and await container.is_visible():
                            return candidate, container
                    except Exception as exc:
                        if self._is_session_closed_exception(exc):
                            raise
            return None, None

        selected_same_company, active_container = await find_selected_same_company()
        if selected_same_company is None or active_container is None:
            self._emit(progress, "素材管理查询范围已无“同公司”选中值")
            return True

        for attempt in range(1, 6):
            self._raise_if_cancelled()
            # 这里按素材管理页面的实际交互操作：第一次点击当前值打开下拉，
            # 页面响应后再点击下拉中的同名选项取消勾选。
            first_clicked = await self._click_locator(selected_same_company)
            if not first_clicked:
                try:
                    await active_container.click(force=True, timeout=3000)
                    first_clicked = True
                except Exception as exc:
                    if self._is_session_closed_exception(exc):
                        raise
            if not first_clicked:
                await self._sleep(0.5)
                continue

            await page.wait_for_timeout(700)
            option_candidates = page.locator(
                ".arco-trigger-popup .arco-select-option, "
                ".arco-select-dropdown .arco-select-option, "
                "[role='option']"
            )
            second_clicked = False
            for option in await self._visible_locators(option_candidates, limit=50):
                try:
                    if _compact_text(await self._locator_text(option, timeout_ms=1000)) != "同公司":
                        continue
                    if await self._click_locator(option):
                        second_clicked = True
                        break
                except Exception as exc:
                    if self._is_session_closed_exception(exc):
                        raise
            if not second_clicked:
                self._emit(progress, f"未找到展开后的“同公司”选项，继续重试（第 {attempt} 次）")
                try:
                    await page.keyboard.press("Escape")
                except Exception as exc:
                    if self._is_session_closed_exception(exc):
                        raise
                await self._sleep(0.5)
                selected_same_company, active_container = await find_selected_same_company()
                if selected_same_company is None or active_container is None:
                    return True
                continue

            await page.wait_for_timeout(700)
            selected_same_company, current_container = await find_selected_same_company()
            if selected_same_company is None:
                if await scope_has_selected_value(active_container):
                    await self._snapshot_error(page, "material_query_scope_unexpected_value")
                    raise RuntimeError("清除“同公司”后仍存在其他查询范围筛选值")
                self._emit(progress, "已间隔点击两次“同公司”并验证取消查询范围")
                try:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(300)
                except Exception as exc:
                    if self._is_session_closed_exception(exc):
                        raise
                return True
            active_container = current_container or active_container
            self._emit(progress, f"两次点击后“同公司”仍选中，继续重试（第 {attempt} 次）")
            await self._sleep(0.5)
        await self._snapshot_error(page, "material_query_scope_not_cleared")
        raise RuntimeError("素材管理筛选框中的“同公司”未能清除，已停止 CID 搜索")

    async def _close_redfruit_material_drawer(self, page) -> bool:
        """关闭红果素材列表右侧抽屉，避免遮挡全选与分页控件。"""
        drawer = page.locator(".arco-drawer").first
        try:
            if not await drawer.count() or not await drawer.is_visible():
                return False
        except Exception as exc:
            if self._is_session_closed_exception(exc):
                raise
            return False

        candidates = (
            page.locator(".arco-drawer .arco-drawer-close-btn").first,
            page.locator(".arco-drawer .arco-drawer-header button").first,
            page.locator(".arco-drawer .arco-icon-close").first,
            page.locator(".arco-drawer [aria-label*='close']").first,
            page.locator(".arco-drawer button[aria-label*='close']").first,
            page.locator(".arco-drawer button:has-text('关闭')").first,
            page.locator(".arco-drawer [role='button']:has-text('关闭')").first,
        )
        for locator in candidates:
            try:
                if not await locator.count() or not await locator.is_visible():
                    continue
                if await self._click_locator(locator) or await self._click_locator_center(page, locator):
                    await page.wait_for_timeout(600)
                    return True
            except Exception:
                continue
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(600)
        except Exception:
            pass
        try:
            return not await drawer.is_visible()
        except Exception:
            return False

    def _existing_creative_unit_id_for_item(self, item: UserGrowthVideoItem) -> str:
        """读取上传失败时记录下来的原创意单元 ID。"""
        metadata = item.workflow_metadata if isinstance(item.workflow_metadata, dict) else {}
        return str(metadata.get("existing_creative_unit_id") or "").strip()

    @staticmethod
    def _parse_already_recorded_material_cids(text: str) -> dict[str, str]:
        """解析“已录入为素材”弹窗中的素材 ID 与 CID 映射。"""
        compact = _compact_text(text)
        pairs = re.findall(
            r"创意id[:=：]?(\d+).*?cid[:=：]?([0-9a-fA-F]{32})",
            compact,
            flags=re.IGNORECASE,
        )
        return {material_id: cid.lower() for material_id, cid in pairs}

    @staticmethod
    def _parse_already_recorded_material_ids(text: str) -> set[str]:
        """解析弹窗中所有已录入创意 ID；CID 缺失时仍可识别对应素材。"""
        compact = _compact_text(text)
        return set(re.findall(r"创意id[:=：]?(\d+)", compact, flags=re.IGNORECASE))

    async def _consume_already_recorded_materials(
            self,
            page,
            items: list[UserGrowthVideoItem],
            progress: ProgressCallback | None,
    ) -> list[UserGrowthVideoItem] | None:
        """消费已录入弹窗并返回明确命中的素材；未出现弹窗返回 None。"""
        body = await self._body_text(page, timeout_ms=3000)
        compact = _compact_text(body)
        if "已录入为素材" not in compact and "已录入,cid" not in compact.lower():
            return None

        cid_by_material_id = self._parse_already_recorded_material_cids(body)
        recorded_material_ids = self._parse_already_recorded_material_ids(body)
        if not recorded_material_ids:
            await self._snapshot_error(
                page,
                "existing_already_recorded_ids_unparsed",
                extra="已录入弹窗未解析到创意 ID，已阻止整批误判完成",
            )
            raise RuntimeError("已录入为素材弹窗未解析到具体创意 ID，无法安全继续补录")

        matched_items: list[UserGrowthVideoItem] = []
        matched_material_ids: set[str] = set()
        for item in items:
            metadata = item.workflow_metadata if isinstance(item.workflow_metadata, dict) else {}
            material_id = str(metadata.get("existing_material_id") or "").strip()
            if not material_id or material_id not in recorded_material_ids:
                continue
            cid = cid_by_material_id.get(material_id, "")
            if cid:
                item.cid = cid
                item.cid_material_type = item.material_type
                item.message = "原创意单元已录入，CID 已读取"
            else:
                item.message = "原创意单元已录入，无需重复录入"
            item.status = "success"
            matched_items.append(item)
            matched_material_ids.add(material_id)

        unmatched_material_ids = recorded_material_ids - matched_material_ids
        if unmatched_material_ids:
            await self._snapshot_error(
                page,
                "existing_already_recorded_ids_unmatched",
                extra=f"unmatched_material_ids={','.join(sorted(unmatched_material_ids))}",
            )
            raise RuntimeError(
                "已录入弹窗中的创意 ID 无法与本批素材对应："
                + ",".join(sorted(unmatched_material_ids))
            )

        remaining_count = len(items) - len(matched_items)
        self._emit(
            progress,
            f"原创意单元已录入为素材：命中 {len(matched_items)}/{len(items)}，"
            f"剩余待补录 {remaining_count}",
        )
        self._write_run_log(
            f"[{datetime.now().isoformat(timespec='seconds')}] existing recovery already recorded: "
            f"recorded={len(matched_items)}/{len(items)}, remaining={remaining_count}, "
            f"matched_cids={sum(1 for item in matched_items if item.cid)}"
        )
        self._write_event(
            "existing_creative_units_already_recorded",
            item_count=len(items),
            recorded_count=len(matched_items),
            remaining_count=remaining_count,
            matched_cid_count=sum(1 for item in matched_items if item.cid),
            material_cids=cid_by_material_id,
        )
        await self._click_if_present(page, "确定")
        return matched_items

    async def _process_existing_creative_unit_items(
            self,
            page,
            plan: UserGrowthOrderPlan,
            items: list[UserGrowthVideoItem],
            progress: ProgressCallback | None,
    ):
        """上传检测重复时，回到创意单元列表按原创意单元 ID 补录素材。"""
        items = [item for item in items if item.status not in {"success", "skipped"}]
        if not items:
            return page
        unit_ids = []
        for item in items:
            unit_id = self._existing_creative_unit_id_for_item(item)
            if unit_id and unit_id not in unit_ids:
                unit_ids.append(unit_id)
        if not unit_ids:
            return page

        self._emit(progress, f"回到工单管理补录已存在创意单元：{','.join(unit_ids)}")
        await self._open_work_order_management(page, progress)
        self._emit(progress, "已返回工单管理，准备进入创意单元页")
        await self._open_creative_unit_tab(page)
        self._emit(progress, f"已打开创意单元页，按 ID 搜索：{','.join(unit_ids)}")
        await self._search_creative_units_by_ids(page, unit_ids)
        self._emit(progress, "创意单元搜索完成，准备跨页勾选")
        pseudo_items = [
            UserGrowthVideoItem(
                path=Path(unit_id),
                file_name=unit_id,
                material_type="",
                song_name="",
            )
            for unit_id in unit_ids
        ]
        await self._select_creative_units_for_items(page, pseudo_items)
        self._emit(progress, "创意单元已选中，准备进入录入素材")
        entry_page, pending_items = await self._enter_chameleon_from_selected_creative_units(
            page,
            plan,
            items,
            progress,
        )
        if entry_page is None:
            unresolved = [item for item in items if item.status != "success"]
            if unresolved:
                raise RuntimeError(f"补录结束后仍有 {len(unresolved)} 个素材未处理")
            return page

        self._emit(progress, "已进入录入素材页，准备读取任务ID")
        task_id = await self._read_current_task_id(entry_page, progress=progress)
        if not task_id:
            await self._snapshot_error(entry_page, f"existing_creative_units_task_id_not_found_{plan.order_id}")
            raise RuntimeError("补录原创意单元后未读取到当前任务ID")
        plan.task_id = task_id
        plan.upload_task_id = task_id
        self._checkpoint(plan, "upload_task_created", f"补录任务已创建，任务 ID：{task_id}")

        redfruit_flow = self._is_redfruit_items(items)
        wait_result = await self._wait_task_success(
            entry_page,
            progress,
            expected_attempts=max(len(pending_items), 1),
            plan=None if redfruit_flow else plan,
            items=[] if redfruit_flow else pending_items,
            task_id=task_id,
            retry_failed_task=redfruit_flow,
            operation_name="素材上传",
        )
        if wait_result == "cid_backed_up":
            self._checkpoint(plan, "cid_backfilled_unreviewed", "补录任务 CID 已备份回填，当前任务尚未送审")
            return entry_page
        self._checkpoint(plan, "upload_success", f"补录任务 {task_id} 全部成功")
        if redfruit_flow:
            await self._submit_review(entry_page)
            plan.review_task_id = task_id
            self._checkpoint(plan, "review_submitted", f"补录任务已送审，任务 ID：{task_id}")
            await self._process_redfruit_after_review(entry_page, plan, pending_items, task_id, progress)
        else:
            await self._submit_soda_review(entry_page, plan, task_id, progress)
            self._checkpoint(plan, "cid_backfilling", f"开始读取补录任务 {task_id} 的 CID 并回填")
            await self._fill_cids_for_task(
                entry_page,
                pending_items,
                task_id,
                progress,
                retry_failed_task=True,
                operation_name="汽水音乐送审",
                plan=plan,
            )
            self._checkpoint(plan, "completed", "汽水音乐补录、送审和 CID 回填全部完成")
        return entry_page

    async def _open_creative_unit_tab(self, page) -> None:
        """进入工单管理页的创意单元 tab。"""
        if await self._click_text_or_locator(page, "创意单元"):
            await page.wait_for_timeout(1200)
            return
        raise RuntimeError("未找到创意单元 tab")

    async def _search_creative_units_by_ids(self, page, unit_ids: list[str]) -> None:
        """在创意单元 tab 中按多个创意单元 ID 逗号搜索。"""
        query = ",".join(unit_ids)
        search_input = await self._wait_first_existing(
            page,
            (
                "input[placeholder*='创意单元名称或ID']",
                "input[placeholder*='创意单元名称']",
                "input[placeholder*='创意单元']",
            ),
            timeout_ms=30000,
        )
        if not search_input:
            raise RuntimeError("未找到创意单元名称或ID搜索框")
        await self._type_into_locator(search_input, page, query)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(2500)

        async def any_unit_visible() -> bool:
            body = await self._body_text(page, timeout_ms=3000)
            return any(unit_id in body for unit_id in unit_ids)

        if not await self._wait_for_result(any_unit_visible, timeout_ms=30000, interval_ms=1000):
            await self._snapshot_error(page, "existing_creative_units_not_found", extra=f"query={query}")
            raise RuntimeError(f"未在创意单元列表中找到：{query}")

    async def _enter_chameleon_from_selected_creative_units(
            self,
            page,
            plan: UserGrowthOrderPlan,
            items: list[UserGrowthVideoItem],
            progress: ProgressCallback | None = None,
    ):
        """从已选中的创意单元列表进入录入素材，兼容已录入提示。"""
        before_body = await self._body_text(page, timeout_ms=3000)
        try:
            before_pages = list(page.context.pages)
        except Exception:
            before_pages = [page]
        before_page_ids = {id(candidate) for candidate in before_pages}
        before_url = page.url
        self._write_run_log(
            f"[{datetime.now().isoformat(timespec='seconds')}] existing recovery: "
            "开始定位录入素材按钮"
        )
        try:
            self._attach_entry_network_diagnostics(page.context)
            # 录入素材页会加载独立的前端模块；登录后的静态资源拦截可能让
            # 新标签页停在空白 loading 状态，进入该页前恢复完整资源加载。
            try:
                await page.context.unroute("**/*")
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] existing recovery: "
                    "已解除登录后静态资源拦截，准备打开录入页"
                )
            except Exception:
                pass
            await self._wait_and_click_entry_materials_button(page)
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] existing recovery: "
                "录入素材按钮已点击，等待录入页"
            )
            await self._log_after_entry_materials_click(page)
            recorded_items = await self._consume_already_recorded_materials(page, items, progress)
            if recorded_items is not None:
                return await self._continue_partial_existing_recovery(
                    page,
                    plan,
                    items,
                    recorded_items,
                    progress,
                )
        except Exception as exc:
            recorded_items = await self._consume_already_recorded_materials(page, items, progress)
            if recorded_items is not None:
                return await self._continue_partial_existing_recovery(
                    page,
                    plan,
                    items,
                    recorded_items,
                    progress,
                )
            body = await self._body_text(page, timeout_ms=3000)
            combined = f"{before_body}\n{body}"
            if any(text in combined for text in ("已录入", "已经录入", "无需再进行", "无需重复")):
                await self._snapshot_error(
                    page,
                    "existing_already_recorded_unparsed",
                    exc=exc,
                    extra="页面出现已录入提示，但未解析到具体创意 ID",
                )
                raise RuntimeError("页面提示素材已录入，但无法识别具体素材，已停止整批误判") from exc
            await self._snapshot_error(page, "existing_creative_units_enter_chameleon_failed", exc=exc)
            raise

        entry_page = await self._wait_for_existing_creative_unit_entry_page(
            page,
            progress,
            before_page_ids=before_page_ids,
            before_url=before_url,
        )
        if entry_page is None:
            raise RuntimeError("补录页面返回已录入状态，但未获得可解析的弹窗页面")
        self._wrap_page_speed(entry_page)
        # 新标签页可能持续保持 loading 状态，但页面内容已经可以交互；
        # 补录流程只等待真实业务条件，避免被浏览器导航事件永久卡住。
        already_script = """
        () => {
          const body = document.body ? (document.body.innerText || '') : '';
          return ['已录入', '已经录入', '无需再进行', '无需重复']
            .some((text) => body.includes(text));
        }
        """
        entry_script = """
        () => {
          const body = document.body ? (document.body.innerText || '') : '';
          const inputs = Array.from(document.querySelectorAll('input'));
          const hasTaskInput = inputs.some((input) => {
            const placeholder = input.getAttribute('placeholder') || '';
            if (!(placeholder.includes('任务ID') || placeholder.includes('任务'))) {
              return false;
            }
            const style = window.getComputedStyle(input);
            const rect = input.getBoundingClientRect();
            return style.display !== 'none' &&
              style.visibility !== 'hidden' &&
              Number(style.opacity || '1') > 0 &&
              rect.width > 0 && rect.height > 0;
          });
          const hasVisibleText = (wanted) => Array.from(document.querySelectorAll('body *'))
            .some((element) => {
              const text = (element.innerText || element.textContent || '').trim();
              if (!text.includes(wanted)) return false;
              const style = window.getComputedStyle(element);
              const rect = element.getBoundingClientRect();
              return style.display !== 'none' &&
                style.visibility !== 'hidden' &&
                Number(style.opacity || '1') > 0 &&
                rect.width > 0 && rect.height > 0;
            });
          const hasDeliveryText = hasVisibleText('投放平台') &&
            (hasVisibleText('投放产品') || hasVisibleText('红果免费短剧') || hasVisibleText('红果免费漫剧'));
          return hasDeliveryText;
        }
        """
        already_task = asyncio.create_task(
            entry_page.wait_for_function(already_script, polling=500, timeout=0)
        )
        entry_task = asyncio.create_task(
            entry_page.wait_for_function(entry_script, polling=500, timeout=0)
        )
        wait_round = 0
        entry_state = "entry"
        try:
            entry_page_url = entry_page.url
        except Exception:
            entry_page_url = "https://usergrowth.com.cn/aigc/creatives/upload"
        try:
            while not already_task.done() and not entry_task.done():
                self._raise_if_cancelled()
                await self._sleep(10)
                wait_round += 1
                self._emit(progress, f"录入素材页仍在加载，第 {wait_round} 次检查")
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] existing recovery entry wait: "
                    f"round={wait_round}, url={entry_page_url}"
                )
            if already_task.done():
                entry_state = "already"
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] "
                    "existing recovery entry condition: already"
                )
            else:
                await entry_task
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] "
                    "existing recovery entry condition: entry"
                )
        finally:
            for pending_task in (already_task, entry_task):
                if not pending_task.done():
                    pending_task.cancel()
        if entry_state == "already":
            recorded_items = await self._consume_already_recorded_materials(entry_page, items, progress)
            if recorded_items is None:
                await self._snapshot_error(
                    entry_page,
                    "existing_entry_already_recorded_unparsed",
                    extra="录入页出现已录入提示，但未解析到具体创意 ID",
                )
                raise RuntimeError("录入页提示素材已录入，但无法识别具体素材")
            return await self._continue_partial_existing_recovery(
                page,
                plan,
                items,
                recorded_items,
                progress,
                recorded_page=entry_page,
            )

        self._write_run_log(
            f"[{datetime.now().isoformat(timespec='seconds')}] "
            "existing recovery: 录入页业务条件已满足，开始检查投放弹窗"
        )
        await self._snapshot_entry_page_probe(entry_page, 2)
        await self._snapshot(entry_page, "existing_creative_units_after_enter_chameleon")
        await self._ensure_chameleon_modal(entry_page, items[0])
        await self._click_chameleon_modal_confirm(entry_page)
        await self._wait_chameleon_card_forms_ready(entry_page, items, progress)
        task_page = await self._fill_card_defaults(entry_page, items[0])
        return task_page or entry_page, items

    async def _continue_partial_existing_recovery(
            self,
            page,
            plan: UserGrowthOrderPlan,
            items: list[UserGrowthVideoItem],
            recorded_items: list[UserGrowthVideoItem],
            progress: ProgressCallback | None,
            *,
            recorded_page=None,
    ):
        """已录入仅覆盖部分素材时，取消这些勾选并继续补录剩余素材。"""
        recorded_ids = {id(item) for item in recorded_items}
        remaining_items = [item for item in items if id(item) not in recorded_ids]
        self._checkpoint(
            plan,
            "upload_processing",
            f"补录发现已录入 {len(recorded_items)}/{len(items)}，剩余 {len(remaining_items)} 个继续录入",
        )
        if recorded_page is not None and recorded_page is not page:
            try:
                await self._close_page_intentionally(recorded_page)
            except Exception:
                pass
        if not remaining_items:
            self._emit(progress, f"本次补录 {len(items)} 个素材均已录入，结束补录分支")
            return None, []

        self._emit(
            progress,
            f"部分素材已录入：确认后取消勾选 {len(recorded_items)} 个，"
            f"其余 {len(remaining_items)} 个继续录入",
        )
        await self._deselect_recorded_creative_units(page, recorded_items, len(remaining_items))
        return await self._enter_chameleon_from_selected_creative_units(
            page,
            plan,
            remaining_items,
            progress,
        )

    def _attach_entry_network_diagnostics(self, context) -> None:
        """记录录入页主文档、失败请求和页面 JS 异常。"""
        if getattr(context, "_usergrowth_entry_diagnostics", False):
            return

        def response_handler(response) -> None:
            try:
                url = response.url
                status = response.status
                if "/aigc/creatives/upload" in url or status >= 400:
                    self._write_run_log(
                        f"[{datetime.now().isoformat(timespec='seconds')}] entry response: "
                        f"status={status}, url={url}"
                    )
                    if "/aigc/creatives/upload" in url:
                        async def capture_entry_document() -> None:
                            try:
                                payload = await response.body()
                                headers = response.headers
                                self._write_run_log(
                                    f"[{datetime.now().isoformat(timespec='seconds')}] entry document body: "
                                    f"bytes={len(payload)}, content_type={headers.get('content-type', '')}, "
                                    f"prefix={payload[:160]!r}"
                                )
                            except Exception as exc:
                                self._write_run_log(
                                    f"[{datetime.now().isoformat(timespec='seconds')}] entry document body failed: "
                                    f"{type(exc).__name__}: {exc}"
                                )
                        asyncio.create_task(capture_entry_document())
            except Exception:
                pass

        def request_failed_handler(request) -> None:
            try:
                failure = request.failure
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] entry request failed: "
                    f"url={request.url}, failure={failure}"
                )
            except Exception:
                pass

        def page_handler(new_page) -> None:
            try:
                new_page.on(
                    "pageerror",
                    lambda exc: self._write_run_log(
                        f"[{datetime.now().isoformat(timespec='seconds')}] entry pageerror: {exc}"
                    ),
                )
                new_page.on(
                    "console",
                    lambda message: self._write_run_log(
                        f"[{datetime.now().isoformat(timespec='seconds')}] entry console: "
                        f"type={message.type}, text={message.text}"
                    ) if message.type in {"error", "warning"} else None,
                )
            except Exception:
                pass

        context.on("response", response_handler)
        context.on("requestfailed", request_failed_handler)
        context.on("page", page_handler)
        context._usergrowth_entry_diagnostics = True

    async def _wait_and_click_entry_materials_button(self, page) -> None:
        """只点击当前列表操作区内真正可用的“录入素材”按钮。

        列表页可能同时挂载隐藏的抽屉、旧弹窗和表格行文本。直接按全文本
        点击有机会命中这些隐藏节点，或命中按钮里的文本层而没有触发按钮
        事件。优先按按钮元素、可见、未禁用和页面底部操作栏筛选。
        """
        deadline = None
        attempt = 0
        while deadline is None or asyncio.get_event_loop().time() < deadline:
            attempt += 1
            candidates = []
            selectors = (
                "button:has-text('录入素材')",
                "[role='button']:has-text('录入素材')",
                ".arco-btn:has-text('录入素材')",
            )
            for selector in selectors:
                try:
                    locators = page.locator(selector)
                    for candidate in await self._visible_locators(locators, limit=20):
                        try:
                            if await candidate.is_enabled():
                                candidates.append(candidate)
                        except Exception:
                            continue
                except Exception:
                    continue
            if candidates:
                # 录入素材通常位于底部批量操作栏，优先选择页面中最靠下的可用按钮。
                scored = []
                for candidate in candidates:
                    try:
                        box = await candidate.bounding_box()
                        if box:
                            scored.append((box["y"] + box["height"], candidate))
                    except Exception:
                        continue
                if scored:
                    _, target = max(scored, key=lambda item: item[0])
                    await self._click_locator_center(page, target)
                    return
                if await self._click_locator(candidates[0]):
                    return
            if attempt % 10 == 0:
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] waiting usable entry-materials button: "
                    f"attempt={attempt}, url={page.url}"
                )
            await self._sleep(0.5)

    async def _log_after_entry_materials_click(self, page) -> None:
        """记录点击录入素材后的同页状态，便于区分弹层、跳转和事件未生效。"""
        try:
            pages = list(page.context.pages)
        except Exception:
            pages = []
        body = await self._body_text(page, timeout_ms=3000)
        lines = [
            f"[{datetime.now().isoformat(timespec='seconds')}] existing recovery after click:",
            f"  current_url: {page.url}",
            f"  pages: {len(pages)}",
        ]
        lines.append("  page_objects: " + str(len(pages)))
        for selector in (".arco-modal", "[role='dialog']", ".arco-drawer", ".arco-message"):
            try:
                visible = await self._visible_locators(page.locator(selector), limit=10)
            except Exception:
                visible = []
            if visible:
                lines.append(f"  visible {selector}: {len(visible)}")
                for item in visible[:3]:
                    lines.append("    " + _compact_text(await self._locator_text(item, timeout_ms=1000))[:500])
        lines.append("  body_tail: " + _compact_text(body)[-1200:])
        self._write_run_log("\n".join(lines))
        await self._snapshot(page, "existing_after_entry_materials_click", screenshot=True)

    async def _wait_for_existing_creative_unit_entry_page(
            self,
            page,
            progress: ProgressCallback | None = None,
            *,
            before_page_ids: set[int] | None = None,
            before_url: str = "",
    ):
        """等待补录原创意单元后进入录入页；兼容同页弹层和新标签页。"""
        attempt = 0
        before_page_ids = before_page_ids or {id(page)}
        while True:
            self._raise_if_cancelled()
            attempt += 1
            try:
                candidates = list(page.context.pages)
            except Exception:
                candidates = []
            # 新标签页一出现就接管，不要求它已经完成 domcontentloaded。
            # 某些录入页会先打开空白/加载中的 tab，再由前端异步填充内容；
            # 先过滤新对象会导致永远停留在原列表页。
            for candidate in reversed(candidates):
                try:
                    if not candidate.is_closed() and id(candidate) not in before_page_ids:
                        try:
                            await candidate.bring_to_front()
                        except Exception:
                            pass
                        try:
                            candidate_url = candidate.url
                        except Exception:
                            candidate_url = "<unavailable>"
                        self._write_run_log(
                            f"[{datetime.now().isoformat(timespec='seconds')}] existing recovery: "
                            f"发现新录入标签页，attempt={attempt}, pages={len(candidates)}, "
                            f"url={candidate_url}"
                        )
                        await self._snapshot_entry_page_probe(candidate, attempt)
                        return candidate
                except Exception:
                    continue
            try:
                if not page.is_closed() and before_url and page.url != before_url:
                    return page
            except Exception:
                pass
            for candidate in reversed(candidates):
                try:
                    if candidate.is_closed():
                        continue
                except Exception:
                    continue
                try:
                    body = await self._body_text(candidate, timeout_ms=2000)
                except Exception:
                    body = ""
                compact_body = _compact_text(body)
                if any(text in compact_body for text in ("已录入", "已经录入", "无需再进行", "无需重复")):
                    return candidate
                try:
                    if await self._first_existing(
                            candidate,
                            (
                                "input[placeholder*='任务ID']",
                                "input[placeholder*='任务']",
                            ),
                    ) or await self._looks_like_chameleon_entry_page(candidate):
                        return candidate
                except Exception:
                    continue
            if attempt == 1 or attempt % 10 == 0:
                self._emit(progress, f"等待录入素材页渲染中，第 {attempt} 次")
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] existing recovery page wait: "
                    f"attempt={attempt}, pages={len(candidates)}"
                )
            await self._sleep(1)

    async def _snapshot_entry_page_probe(self, page, attempt: int) -> None:
        """保存新录入标签页的首屏证据，不阻塞主等待条件。"""
        if not self.debug_dir:
            return
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        prefix = self.debug_dir / f"existing_entry_tab_probe_{attempt}"
        try:
            state = await asyncio.wait_for(
                page.evaluate(
                    """() => ({
                      readyState: document.readyState,
                      bodyLength: document.body ? (document.body.innerText || '').length : 0,
                      title: document.title || '',
                      href: location.href
                    })"""
                ),
                timeout=5.0,
            )
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] existing entry tab state: {state}"
            )
        except Exception as exc:
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] existing entry tab state failed: "
                f"{type(exc).__name__}: {exc}"
            )
        try:
            await asyncio.wait_for(
                page.screenshot(path=str(prefix.with_suffix('.png')), full_page=True, timeout=10000),
                timeout=12.0,
            )
        except Exception as exc:
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] existing entry tab screenshot failed: "
                f"{type(exc).__name__}: {exc}"
            )

    async def _select_creative_units_for_items(self, page, items: list[UserGrowthVideoItem]) -> None:
        """上传后在创意单元列表跨页全选本次生成的所有单元。"""
        expected_count = len(items)
        if expected_count <= 0:
            return
        selected_count = await self._select_creative_unit_rows_across_pages(page, items)
        if selected_count >= expected_count:
            return
        await self._snapshot_error(
            page,
            "creative_unit_selection_incomplete",
            extra=f"expected={expected_count}, selected_count={selected_count}",
        )
        raise RuntimeError(
            f"创意单元未全选完成：期望 {expected_count} 个，页面已选择 {selected_count} 个"
        )

    async def _select_creative_unit_rows_across_pages(
            self,
            page,
            items: list[UserGrowthVideoItem],
    ) -> int:
        """创意单元列表页先全选当前页，再跨页补选到总数达标。"""
        await self._set_page_size_max(page)
        expected_count = len(items)
        seen_pages: set[str] = set()
        selected_count = await self._selected_count(page)

        for page_index in range(1, 101):
            self._raise_if_cancelled()
            await self._wait_creative_unit_table_ready(page, timeout_ms=None)
            page_signature = await self._creative_unit_page_signature(page)
            if page_signature and page_signature in seen_pages:
                break
            if page_signature:
                seen_pages.add(page_signature)

            before_count = await self._selected_count(page)
            await self._wait_and_click_table_select_all(page, timeout_ms=None)
            await self._wait_selected_count(page, max(before_count + 1, 1), timeout_ms=None)
            selected_count = await self._selected_count(page)
            if selected_count <= before_count:
                targets = self._creative_unit_target_tokens(items)
                matched = await self._select_creative_unit_rows_on_current_page(page, targets)
                if matched:
                    await self._wait_selected_count(page, max(before_count + 1, 1), timeout_ms=None)
                    selected_count = await self._selected_count(page)
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] creative unit page {page_index}: "
                f"selected_count={selected_count}, before_count={before_count}"
            )
            if selected_count >= expected_count:
                return selected_count
            if not await self._click_creative_unit_next_page(page):
                break
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] creative unit pagination: "
                f"等待第 {page_index + 1} 页表格刷新"
            )

            async def page_changed() -> bool:
                await self._wait_creative_unit_table_ready(page, timeout_ms=None)
                current_signature = await self._creative_unit_page_signature(page)
                return bool(current_signature and current_signature != page_signature)

            await self._wait_for_result(page_changed, timeout_ms=None, interval_ms=500)

        return selected_count

    @staticmethod
    def _creative_unit_target_tokens(items: list[UserGrowthVideoItem]) -> dict[str, list[str]]:
        """生成创意单元列表行文本可匹配的文件名 token。"""
        targets: dict[str, list[str]] = {}
        for item in items:
            file_name = str(item.file_name or "")
            stem = Path(file_name).stem
            compact_stem = _compact_text(stem).lower()
            compact_name = _compact_text(file_name).lower()
            tokens = [token for token in (compact_stem, compact_name) if token]
            if tokens:
                targets[compact_stem or compact_name] = tokens
        return targets

    async def _select_creative_unit_rows_on_current_page(
            self,
            page,
            targets: dict[str, list[str]],
    ) -> set[str]:
        """选择当前页中属于本次上传文件的创意单元行。"""
        matched: set[str] = set()
        rows = page.locator(
            ".arco-table-tbody tr, .arco-table-tr:not(.arco-table-tr-th), "
            "[class*='arco-table-row']:not([class*='header']), tr, [role='row']"
        )
        for row in await self._visible_locators(rows, limit=200):
            row_text = _compact_text(await self._locator_text(row, timeout_ms=1000)).lower()
            if not row_text:
                continue
            matched_key = ""
            for key, tokens in targets.items():
                if key in matched:
                    continue
                if any(token and token in row_text for token in tokens):
                    matched_key = key
                    break
            if not matched_key:
                continue
            if await self._click_visible_checkbox_box(
                    row,
                    "label.arco-checkbox, .arco-checkbox-mask-wrapper, .arco-checkbox-mask, .arco-checkbox, label",
            ):
                matched.add(matched_key)
        return matched

    async def _deselect_recorded_creative_units(
            self,
            page,
            recorded_items: list[UserGrowthVideoItem],
            expected_remaining_count: int,
    ) -> None:
        """跨页取消已录入素材对应的创意单元勾选，并校验剩余数量。"""
        targets: dict[str, list[str]] = {}
        for item in recorded_items:
            metadata = item.workflow_metadata if isinstance(item.workflow_metadata, dict) else {}
            unit_id = str(metadata.get("existing_creative_unit_id") or "").strip()
            material_id = str(metadata.get("existing_material_id") or "").strip()
            file_name = str(item.file_name or "").strip()
            tokens = [
                _compact_text(value).lower()
                for value in (unit_id, material_id, file_name, Path(file_name).stem)
                if _compact_text(value)
            ]
            key = _compact_text(unit_id or material_id or file_name).lower()
            if key and tokens:
                targets[key] = list(dict.fromkeys(tokens))
        if len(targets) != len(recorded_items):
            raise RuntimeError("部分已录入素材缺少创意单元 ID，无法安全取消勾选")

        await self._return_creative_unit_page_to_first_page(page)
        matched: set[str] = set()
        seen_pages: set[str] = set()
        for page_index in range(1, 101):
            self._raise_if_cancelled()
            await self._wait_creative_unit_table_ready(page, timeout_ms=None)
            signature = await self._creative_unit_page_signature(page)
            if signature and signature in seen_pages:
                break
            if signature:
                seen_pages.add(signature)
            matched.update(
                await self._deselect_creative_unit_rows_on_current_page(
                    page,
                    {key: value for key, value in targets.items() if key not in matched},
                )
            )
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] existing recovery deselect: "
                f"page={page_index}, matched={len(matched)}/{len(targets)}"
            )
            if len(matched) >= len(targets):
                break
            if not await self._click_creative_unit_next_page(page):
                break

            async def page_changed() -> bool:
                await self._wait_creative_unit_table_ready(page, timeout_ms=None)
                current = await self._creative_unit_page_signature(page)
                return bool(current and current != signature)

            await self._wait_for_result(page_changed, timeout_ms=None, interval_ms=500)

        missing = set(targets) - matched
        if missing:
            await self._snapshot_error(
                page,
                "existing_recorded_deselect_incomplete",
                extra=f"missing={','.join(sorted(missing))}",
            )
            raise RuntimeError("未能取消全部已录入素材的勾选：" + ",".join(sorted(missing)))

        async def remaining_count_matches() -> bool:
            return await self._selected_count(page) == expected_remaining_count

        if not await self._wait_for_result(remaining_count_matches, timeout_ms=10000, interval_ms=500):
            actual = await self._selected_count(page)
            await self._snapshot_error(
                page,
                "existing_recorded_deselect_count_mismatch",
                extra=f"expected_remaining={expected_remaining_count}, actual={actual}",
            )
            raise RuntimeError(
                f"取消已录入素材勾选后数量不一致：期望剩余 {expected_remaining_count}，实际 {actual}"
            )

    async def _deselect_creative_unit_rows_on_current_page(
            self,
            page,
            targets: dict[str, list[str]],
    ) -> set[str]:
        """取消当前页中目标创意单元的复选框；已取消的行也视为完成。"""
        matched: set[str] = set()
        if not targets:
            return matched
        rows = page.locator(
            ".arco-table-tbody tr, .arco-table-tr:not(.arco-table-tr-th), "
            "[class*='arco-table-row']:not([class*='header']), tr, [role='row']"
        )
        checkbox_selector = (
            "label.arco-checkbox, .arco-checkbox-mask-wrapper, "
            ".arco-checkbox-mask, .arco-checkbox"
        )
        for row in await self._visible_locators(rows, limit=200):
            row_text = _compact_text(await self._locator_text(row, timeout_ms=1000)).lower()
            if not row_text:
                continue
            matched_key = next(
                (
                    key for key, tokens in targets.items()
                    if key not in matched and any(token and token in row_text for token in tokens)
                ),
                "",
            )
            if not matched_key:
                continue
            boxes = row.locator(checkbox_selector)
            for box in await self._visible_locators(boxes, limit=20):
                if not await self._checkbox_box_is_checked(box):
                    matched.add(matched_key)
                    break
                if not await self._click_locator(box):
                    continue
                await self._sleep(0.2)
                if not await self._checkbox_box_is_checked(box):
                    matched.add(matched_key)
                    break
        return matched

    async def _return_creative_unit_page_to_first_page(self, page) -> None:
        """回到创意单元列表第 1 页，便于跨页精确取消勾选。"""
        while True:
            self._raise_if_cancelled()
            before_signature = await self._creative_unit_page_signature(page)
            pagers = await self._visible_locators(page.locator(".arco-pagination"), limit=20)
            moved = False
            found_pager = False
            for pager in pagers:
                controls = await self._visible_locators(
                    pager.locator("li, button, a, [role='button']"),
                    limit=80,
                )
                numeric: list[tuple[int, object, bool]] = []
                for control in controls:
                    text = _compact_text(await self._locator_text(control, timeout_ms=1000))
                    if not re.fullmatch(r"\d{1,4}", text):
                        continue
                    value = int(text)
                    class_name = str(await control.get_attribute("class") or "").lower()
                    aria_current = str(await control.get_attribute("aria-current") or "").lower()
                    active = "active" in class_name or "current" in class_name or aria_current == "page"
                    numeric.append((value, control, active))
                if not numeric:
                    continue
                found_pager = True
                active_values = [value for value, _, active in numeric if active]
                current = max(active_values) if active_values else min(value for value, _, _ in numeric)
                if current <= 1:
                    return
                previous = [(value, control) for value, control, _ in numeric if value < current]
                if not previous:
                    continue
                target_value, target = min(previous, key=lambda pair: pair[0])
                if await self._click_locator_center(page, target) or await self._click_locator(target):
                    moved = True
                    self._write_run_log(
                        f"[{datetime.now().isoformat(timespec='seconds')}] "
                        f"creative unit pagination: {current} -> {target_value}"
                    )
                    break
            if not found_pager:
                return
            if not moved:
                raise RuntimeError("无法返回创意单元列表第 1 页")

            async def page_changed() -> bool:
                await self._wait_creative_unit_table_ready(page, timeout_ms=None)
                current = await self._creative_unit_page_signature(page)
                return bool(current and current != before_signature)

            await self._wait_for_result(page_changed, timeout_ms=None, interval_ms=500)

    async def _creative_unit_page_signature(self, page) -> str:
        """读取当前创意单元表格可见行签名，用于避免分页循环。"""
        rows = page.locator(".arco-table-tbody tr, .arco-table-tr, [role='row']")
        texts: list[str] = []
        for row in await self._visible_locators(rows, limit=60):
            text = _compact_text(await self._locator_text(row, timeout_ms=1000))
            if text:
                texts.append(text[:80])
        return "|".join(texts)

    async def _click_table_select_all_now(self, page) -> bool:
        """尝试点击当前页表头全选，不等待超时。"""
        candidates = (
            page.locator(".arco-table thead input[type='checkbox']").first,
            page.locator(".arco-table thead .arco-checkbox-mask").first,
            page.locator(".arco-table thead .arco-checkbox-mask-wrapper").first,
            page.locator(".arco-table thead label.arco-checkbox").first,
            page.locator(".arco-table thead .arco-checkbox").first,
            page.locator(".arco-table-header input[type='checkbox']").first,
            page.locator(".arco-table-header .arco-checkbox").first,
            page.locator(".arco-table-th input[type='checkbox']").first,
            page.locator(".arco-table-th .arco-checkbox").first,
            page.get_by_role("button", name="全选").first,
            page.locator("button:has-text('全选')").first,
        )
        for checkbox in candidates:
            try:
                if not await checkbox.count() or not await checkbox.is_visible():
                    continue
                await checkbox.scroll_into_view_if_needed(timeout=3000)
                if await self._checkbox_box_is_checked(checkbox):
                    return True
                if await self._click_locator(checkbox):
                    await page.wait_for_timeout(800)
                    return True
            except Exception:
                continue
        return False

    async def _set_page_size_max(self, page) -> None:
        """把当前表格/列表的分页条数尽量切到最大。"""
        try:
            controls = await self._visible_locators(
                page.locator(
                    ".arco-pagination .arco-pagination-options-size-changer, "
                    ".arco-pagination .arco-select-view, "
                    ".arco-pagination [class*='size-changer'], "
                    ".arco-pagination [class*='pagination-options']"
                ),
                limit=30,
            )
            target = None
            for control in controls:
                text = _compact_text(await self._locator_text(control, timeout_ms=1000))
                if "条/页" in text or "条每页" in text or "/page" in text.lower():
                    target = control
                    break
            if not target:
                for control in (
                        page.get_by_text(re.compile(r"\d+\s*条\s*/\s*页"), exact=False).first,
                        page.get_by_text(re.compile(r"\d+\s*条每页"), exact=False).first,
                ):
                    try:
                        if await control.count() and await control.is_visible():
                            target = control
                            break
                    except Exception:
                        continue
            if not target:
                clicked = await page.evaluate(
                    r"""() => {
                        const visible = el => {
                            if (!el) return false;
                            const style = getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                        };
                        const pager = [...document.querySelectorAll('.arco-pagination')].find(el => visible(el) && /条\s*\/\s*页|条每页/.test(el.innerText || ''));
                        if (!pager) return false;
                        const control = [...pager.querySelectorAll('*')].reverse().find(el => visible(el) && /\d+\s*条\s*\/\s*页|\d+\s*条每页/.test((el.innerText || '').trim()));
                        if (!control) return false;
                        control.click();
                        return true;
                    }"""
                )
                if not clicked:
                    return
                await page.wait_for_timeout(500)
            elif not (await self._click_locator_center(page, target) or await self._click_locator(target)):
                try:
                    await target.evaluate("element => element.click()")
                except Exception:
                    return
            await page.wait_for_timeout(500)
            root = await self._open_dropdown_root(page)
            if root:
                options = await self._visible_locators(
                    root.locator(
                        ".arco-select-option, .arco-cascader-option, [role='option'], "
                        ".arco-dropdown-option, li, [role='menuitem']"
                    ),
                    limit=100,
                )
                best_option = None
                best_value = -1
                for option in options:
                    text = _compact_text(await self._locator_text(option, timeout_ms=1000))
                    match = re.search(r"(\d{1,4})", text)
                    if match and int(match.group(1)) > best_value:
                        best_value = int(match.group(1))
                        best_option = option
                if best_option and best_value > 0:
                    if not await self._click_locator(best_option):
                        await best_option.evaluate("element => element.click()")
                    await page.wait_for_timeout(1200)
                    return
            if await self._click_largest_open_dropdown_option(page):
                await page.wait_for_timeout(1000)
            else:
                await page.evaluate(
                    r"""() => {
                        const visible = el => {
                            if (!el) return false;
                            const style = getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                        };
                        const options = [...document.querySelectorAll('.arco-trigger-popup *, .arco-select-dropdown *, [role="listbox"] *')]
                            .filter(el => visible(el) && /^\d+\s*条\s*\/\s*页$|^\d+\s*条每页$/.test((el.innerText || '').trim()));
                        const option = options.sort((a, b) => parseInt(b.innerText, 10) - parseInt(a.innerText, 10))[0];
                        if (option) option.click();
                    }"""
                )
                await page.wait_for_timeout(1000)
        except Exception:
            return

    async def _click_creative_unit_next_page(self, page) -> bool:
        """只点击创意单元表格自身的下一页，避免误点页面其它分页控件。"""
        pagers = await self._visible_locators(page.locator(".arco-pagination"), limit=20)
        for pager in pagers:
            try:
                controls = await self._visible_locators(
                    pager.locator("li, button, a, [role='button']"),
                    limit=80,
                )
            except Exception:
                continue
            numeric: list[tuple[int, object, bool]] = []
            for control in controls:
                text = _compact_text(await self._locator_text(control, timeout_ms=1000))
                if not re.fullmatch(r"\d{1,4}", text):
                    continue
                value = int(text)
                class_name = str(await control.get_attribute("class") or "").lower()
                aria_current = str(await control.get_attribute("aria-current") or "").lower()
                active = "active" in class_name or "current" in class_name or aria_current == "page"
                numeric.append((value, control, active))
            if not numeric:
                continue
            active_values = [value for value, _, active in numeric if active]
            current = max(active_values) if active_values else min(value for value, _, _ in numeric)
            next_values = [(value, control) for value, control, _ in numeric if value > current]
            if next_values:
                _, next_control = min(next_values, key=lambda pair: pair[0])
                if await self._click_locator_center(page, next_control) or await self._click_locator(next_control):
                    self._write_run_log(
                        f"[{datetime.now().isoformat(timespec='seconds')}] creative unit pagination: "
                        f"{current} -> {min(next_values, key=lambda pair: pair[0])[0]}"
                    )
                    return True
            for control in controls:
                text = _compact_text(await self._locator_text(control, timeout_ms=1000))
                class_name = str(await control.get_attribute("class") or "").lower()
                aria_label = str(await control.get_attribute("aria-label") or "").lower()
                if text in {">", "›", "下一页"} or "next" in aria_label or "next" in class_name:
                    disabled = (
                        str(await control.get_attribute("aria-disabled") or "").lower() == "true"
                        or "disabled" in class_name
                    )
                    if not disabled and (await self._click_locator_center(page, control) or await self._click_locator(control)):
                        self._write_run_log(
                            f"[{datetime.now().isoformat(timespec='seconds')}] creative unit pagination: "
                            f"{current} -> next"
                        )
                        return True
        try:
            clicked = await page.evaluate(
                r"""() => {
                    const visible = el => {
                        if (!el) return false;
                        const style = getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                    };
                    for (const pager of document.querySelectorAll('.arco-pagination')) {
                        if (!visible(pager)) continue;
                        const controls = [...pager.querySelectorAll('li, button, a, [role="button"]')]
                            .filter(visible);
                        const numeric = controls
                            .map(el => ({el, text: (el.innerText || '').trim(), cls: String(el.className || '').toLowerCase(), aria: el.getAttribute('aria-current')}))
                            .filter(item => /^\d+$/.test(item.text));
                        if (!numeric.length) continue;
                        const active = numeric.filter(item => item.cls.includes('active') || item.cls.includes('current') || item.aria === 'page');
                        const current = active.length ? Math.max(...active.map(item => parseInt(item.text, 10))) : Math.min(...numeric.map(item => parseInt(item.text, 10)));
                        const next = numeric.filter(item => parseInt(item.text, 10) > current).sort((a, b) => parseInt(a.text, 10) - parseInt(b.text, 10))[0];
                        if (next) {
                            next.el.scrollIntoView({block: 'center', inline: 'center'});
                            next.el.click();
                            return true;
                        }
                    }
                    return false;
                }"""
            )
            if clicked:
                return True
        except Exception:
            pass
        return False

    async def _click_largest_open_dropdown_option(self, page) -> bool:
        """在当前展开的下拉里点击数值最大的页大小选项。"""
        root = await self._open_dropdown_root(page)
        if not root:
            return False
        best_option = None
        best_value = -1
        options = await self._visible_locators(root.locator(DROPDOWN_OPTION_SELECTOR), limit=80)
        for option in options:
            try:
                text = _compact_text(await self._locator_text(option, timeout_ms=1000))
            except Exception:
                continue
            match = re.search(r"(\d{1,4})", text)
            if not match:
                continue
            value = int(match.group(1))
            if value > best_value:
                best_value = value
                best_option = option
        if best_option and best_value > 0:
            return await self._click_locator(best_option)
        return False

    async def _click_pagination_next(self, page) -> bool:
        """点击列表/表格的下一页。"""
        selectors = (
            ".arco-pagination-item-next",
            ".arco-pagination-next",
            "[aria-label*='next']",
            "button:has-text('下一页')",
            "[role='button']:has-text('下一页')",
        )
        for selector in selectors:
            buttons = page.locator(selector)
            try:
                count = min(await buttons.count(), 20)
            except Exception:
                continue
            for index in range(count):
                button = buttons.nth(index)
                try:
                    if not await button.is_visible():
                        continue
                    class_name = str(await button.get_attribute("class") or "")
                    aria_disabled = str(await button.get_attribute("aria-disabled") or "").lower()
                    disabled = aria_disabled == "true" or "disabled" in class_name or await button.is_disabled()
                    if disabled:
                        continue
                    if await self._click_locator_center(page, button) or await self._click_locator(button):
                        return True
                except Exception:
                    continue
        try:
            clicked = await page.evaluate(
                """() => {
                    const isVisible = el => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                    };
                    const candidates = Array.from(document.querySelectorAll('.arco-pagination-item-next, .arco-pagination-next, button[aria-label*="next"], [role="button"][aria-label*="next"], .arco-pagination button, .arco-pagination [role="button"]'));
                    const button = candidates.find(el => isVisible(el) && !el.className.includes('disabled') && el.getAttribute('aria-disabled') !== 'true' && !el.disabled);
                    if (!button) return false;
                    button.scrollIntoView({ block: 'center', inline: 'center' });
                    button.click();
                    return true;
                }"""
            )
            return bool(clicked)
        except Exception:
            return False

    async def _click_first_material_card(self, page, items: list[UserGrowthVideoItem]) -> bool:
        """尽量稳定地点击红果素材列表第一张卡片的选择角标。"""
        selectors = (
            ".waterfall-item",
            ".common-card-item-eVVh45",
            "[class*='waterfall-item']",
            "[class*='common-card-item']",
            ".arco-card",
            "[class*='material-card']",
            "[class*='material-item']",
            "[class*='creative-card']",
        )
        for selector in selectors:
            try:
                cards = page.locator(selector)
                for card in await self._visible_locators(cards, limit=20):
                    if await self._click_redfruit_material_card_select_hotspot(page, card):
                        return True
            except Exception:
                continue
        rows = page.locator(".arco-table-tbody tr, .arco-table-tr, [role='row']")
        for row in await self._visible_locators(rows, limit=80):
            try:
                before = await self._selected_count(page)
                if not await self._click_visible_checkbox_box(
                        row,
                        "label.arco-checkbox, .arco-checkbox-mask-wrapper, .arco-checkbox-mask, "
                        ".arco-checkbox, input[type='checkbox'], label",
                ):
                    continue
                await page.wait_for_timeout(600)
                if await self._selected_count(page) > before:
                    return True
            except Exception:
                continue
        return False

    async def _click_redfruit_material_card_select_hotspot(self, page, card) -> bool:
        """点击红果素材卡右上角的选择角标，进入批量选择模式。"""
        try:
            if not await card.count() or not await card.is_visible():
                return False
            await card.scroll_into_view_if_needed(timeout=3000)
            try:
                await card.hover()
                await page.wait_for_timeout(250)
            except Exception:
                pass
            box = await card.bounding_box()
            if not box:
                return False
            before = await self._selected_count(page)
            points = (
                (box["x"] + box["width"] - 20, box["y"] + 20),
                (box["x"] + box["width"] - 28, box["y"] + 24),
                (box["x"] + box["width"] - 16, box["y"] + 16),
            )
            for x, y in points:
                try:
                    await page.mouse.click(x, y)
                    await page.wait_for_timeout(800)
                    if await self._selected_count(page) > before:
                        return True
                except Exception:
                    continue
            return False
        except Exception:
            return False

    async def _wait_redfruit_material_selection_bar_visible(self, page) -> bool:
        """等待红果素材列表底部批量操作条出现。"""
        async def bar_visible() -> bool:
            bar = page.locator(".operation-bar-BFu2RW, [class*='operation-bar']").first
            try:
                return bool(await bar.count() and await bar.is_visible())
            except Exception:
                return False

        return bool(await self._wait_for_result(bar_visible, timeout_ms=None, interval_ms=400))

    async def _select_redfruit_all_materials(self, page, expected_count: int) -> bool:
        """红果素材页通过「全选 -> 全选所有」菜单全选所有素材。"""
        if expected_count <= 0:
            return True
        attempt = 0
        while True:
            self._raise_if_cancelled()
            if await self._selected_count(page) == expected_count:
                return True
            attempt += 1
            if not await self._click_redfruit_select_all_button(page):
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] redfruit select-all button not found, "
                    f"attempt={attempt}, expected={expected_count}"
                )
                await page.wait_for_timeout(800)
                continue

            option = await self._wait_redfruit_select_all_menu_option(page)
            if not option:
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] redfruit select-all menu not ready, "
                    f"attempt={attempt}, expected={expected_count}"
                )
                await page.wait_for_timeout(800)
                continue

            if await self._click_locator(option) or await self._click_locator_center(page, option):
                async def selected_exactly_expected() -> bool:
                    return await self._selected_count(page) == expected_count

                await self._wait_for_result(
                    selected_exactly_expected,
                    timeout_ms=None,
                    interval_ms=500,
                )
                return True
            await page.wait_for_timeout(800)

    async def _click_redfruit_select_all_button(self, page) -> bool:
        """点击红果素材底部操作条里的「全选」下拉按钮。"""
        locators = (
            page.locator("[class*='operation-bar'] button:has-text('全选')").first,
            page.locator("button[class*='operation-btn']:has-text('全选')").first,
            page.locator("button:has-text('全选')").first,
            page.get_by_role("button", name=re.compile(r"^全选")).first,
        )
        for locator in locators:
            if await self._click_locator_center(page, locator) or await self._click_locator(locator):
                return True
        return await self._click_text_or_locator(page, "全选")

    async def _wait_redfruit_select_all_menu_option(self, page):
        """等待「全选」下拉菜单项出现，优先点全选所有。"""

        async def find_option():
            text = "全选所有"
            locators = (
                page.locator(f".arco-dropdown-menu-item:has-text('{text}')").first,
                page.locator(f"[role='menuitem']:has-text('{text}')").first,
                page.get_by_text(text, exact=True).first,
            )
            for locator in locators:
                try:
                    if await locator.count() and await locator.is_visible():
                        return locator
                except Exception:
                    continue
            return None

        return await self._wait_for_result(find_option, timeout_ms=8000, interval_ms=300)

    async def _run_material_edit_action(self, page, action_text: str) -> None:
        """打开素材列表底部编辑菜单并点击指定操作。"""
        attempt = 0
        while True:
            self._raise_if_cancelled()
            attempt += 1
            await self._click_if_present(page, "编辑")
            await page.wait_for_timeout(500)
            if await self._click_text_or_locator(page, action_text):
                await page.wait_for_timeout(1200)
                return
            self._write_run_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] wait material edit action "
                f"attempt={attempt}, action={action_text}"
            )
            await page.wait_for_timeout(800)

    async def _open_redfruit_post_review_classification_modal(
            self,
            page,
            progress: ProgressCallback | None,
            required_fields: Iterable[str] = (),
            items: list[UserGrowthVideoItem] | None = None,
    ):
        """打开红果后审分类弹窗，并等待目标后审字段全部出现。"""
        async def restore_selection() -> None:
            if not items:
                return
            await self._wait_material_items_ready(page, items)
            await self._select_all_materials(page, items)

        return await self._open_classification_modal_ready(
            page,
            lambda: self._run_material_edit_action(page, "修改分类标签"),
            required_fields=required_fields,
            progress=progress,
            context_label="红果短剧后审修改分类标签",
            refresh_before_reopen=True,
            on_page_refreshed=restore_selection,
        )

    async def _click_redfruit_modal_action(self, page, texts: tuple[str, ...]) -> None:
        """点击红果弹窗保存按钮，兼容不同按钮文案。"""
        locators = []
        for text in texts:
            locators.extend(
                (
                    page.get_by_text(text, exact=True).first,
                    page.locator(f"button:has-text('{text}')").first,
                )
            )
        if await self._click_first_visible_locator(*locators):
            await page.wait_for_timeout(2000)
            return
        raise RuntimeError(f"未找到弹窗操作按钮：{'/'.join(texts)}")

    async def _refresh_material_list_page(
            self,
            page,
            *,
            settle_delay_seconds: float = 0.0,
            force_reload: bool = False,
    ) -> None:
        """回到素材详情页并刷新列表。"""
        if settle_delay_seconds > 0:
            await self._sleep(settle_delay_seconds)
        if not force_reload:
            for text in ("刷新列表", "刷新"):
                if await self._click_text_or_locator(page, text):
                    await page.wait_for_timeout(2500)
                    return
        try:
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
        except Exception:
            await page.wait_for_timeout(2500)

    @staticmethod
    def _is_target_closed_exception(exc: Exception) -> bool:
        """判断是否属于 Playwright 目标已关闭异常。"""
        message = f"{type(exc).__name__}: {exc}"
        return "TargetClosedError" in message or "Target page, context or browser has been closed" in message

    async def _read_current_task_id(self, page, progress: ProgressCallback | None = None) -> str:
        """从当前任务列表首条数据读取任务ID。"""
        await self._wait_task_list_ready(page, progress=progress)
        task_id_input = await self._wait_first_existing(
            page,
            (
                "input[placeholder*='任务ID']",
                "input[placeholder*='任务']",
            ),
            timeout_ms=10000,
        )
        if not task_id_input:
            raise RuntimeError("未找到任务ID输入框")
        try:
            return self._extract_digits(await task_id_input.input_value(timeout=2000))
        except Exception:
            return self._extract_digits(await self._locator_text(task_id_input, timeout_ms=2000))

    async def _wait_task_list_ready(
            self,
            page,
            timeout_ms: int | None = None,
            progress: ProgressCallback | None = None,
    ) -> None:
        """等待任务ID输入框中出现值后再读取任务ID。"""
        attempt = 0
        deadline = None if timeout_ms is None else asyncio.get_event_loop().time() + timeout_ms / 1000
        while deadline is None or asyncio.get_event_loop().time() < deadline:
            self._raise_if_cancelled()
            attempt += 1
            task_id_input = await self._first_existing(
                page,
                (
                    "input[placeholder*='任务ID']",
                    "input[placeholder*='任务']",
                ),
            )
            if task_id_input:
                try:
                    value = await task_id_input.input_value(timeout=1500)
                except Exception:
                    value = await self._locator_text(task_id_input, timeout_ms=1500)
                if self._extract_digits(value):
                    return
                if attempt % 10 == 0:
                    self._emit(progress, f"等待任务ID填充中，第 {attempt} 次")
            elif attempt % 10 == 0:
                self._emit(progress, f"等待任务ID输入框渲染中，第 {attempt} 次")
            await self._sleep(1)
        raise RuntimeError("等待任务ID输入框渲染超时，未读取到任务ID")

    async def _open_task_detail_for_task_id(
            self,
            page,
            task_id: str,
            progress: ProgressCallback | None,
            *,
            expected_attempts: int | None = None,
            retry_failed_task: bool = False,
            operation_name: str = "素材上传",
            plan: UserGrowthOrderPlan | None = None,
    ):
        """根据任务ID定位任务行，等待成功后点击查看详情。"""
        await self._search_task_by_id(page, task_id)
        await self._wait_task_row_success(
            page,
            task_id,
            progress,
            expected_attempts=expected_attempts,
            retry_failed_task=retry_failed_task,
            operation_name=operation_name,
            plan=plan,
        )
        row = await self._find_task_row(page, task_id)
        if not row:
            await self._snapshot_error(
                page,
                f"task_{task_id}_row_not_found",
                extra=f"task_id={task_id}",
            )
            raise RuntimeError(f"未找到任务 {task_id} 对应行")
        if not await self._click_first_visible_locator(
                row.get_by_text("查看详情", exact=True).first,
                row.locator("a:has-text('查看详情')").first,
                row.locator("button:has-text('查看详情')").first,
        ):
            raise RuntimeError(f"未打开任务 {task_id} 详情")
        await page.wait_for_timeout(2500)
        return page

    async def _open_task_detail_without_wait(self, page, task_id: str):
        """不等待任务成功，直接点击任务行旁边的查看详情。"""
        await self._search_task_by_id(page, task_id)
        row = await self._find_task_row(page, task_id)
        if not row:
            raise RuntimeError(f"未找到任务 {task_id} 对应行")
        before_pages = list(page.context.pages)
        before_url = page.url
        if not await self._click_first_visible_locator(
                row.get_by_text("查看详情", exact=True).first,
                row.locator("a:has-text('查看详情')").first,
                row.locator("button:has-text('查看详情')").first,
        ):
            raise RuntimeError(f"未打开任务 {task_id} 详情")
        target_page = await self._wait_page_change_or_new_page(page, before_pages, before_url, timeout_ms=15000)
        if target_page:
            await target_page.wait_for_timeout(2500)
            return target_page
        await page.wait_for_timeout(2500)
        return page

    async def _search_task_by_id(self, page, task_id: str) -> None:
        """在任务列表页用任务ID精确筛选当前任务。"""
        await self._click_if_present(page, "操作任务")
        search_input = await self._wait_first_existing(
            page,
            (
                "input[placeholder*='任务ID']",
            ),
            timeout_ms=20000,
        )
        if search_input:
            await self._set_task_id_search_input(page, search_input, task_id)
            await page.wait_for_timeout(800)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(2500)
            return
        await page.wait_for_timeout(2500)

    async def _set_task_id_search_input(self, page, locator, task_id: str) -> None:
        """稳定写入任务ID筛选框，优先清空后 fill，并校验最终值。"""
        target_value = str(task_id or "").strip()
        if not target_value:
            raise RuntimeError("任务ID为空，无法写入搜索框")
        try:
            await locator.click(force=True, timeout=5000)
        except Exception:
            pass
        try:
            await locator.fill("", timeout=5000)
            await locator.fill(target_value, timeout=5000)
            current_value = await locator.input_value(timeout=2000)
            if self._extract_digits(current_value) == self._extract_digits(target_value):
                return
        except Exception:
            pass
        try:
            await locator.click(force=True, timeout=5000)
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await self._keyboard_type(page, target_value)
            current_value = await locator.input_value(timeout=2000)
            if self._extract_digits(current_value) == self._extract_digits(target_value):
                return
        except Exception:
            pass
        try:
            await locator.evaluate(
                """(node, value) => {
                    node.value = value;
                    node.dispatchEvent(new Event('input', { bubbles: true }));
                    node.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                target_value,
            )
            current_value = await locator.input_value(timeout=2000)
            if self._extract_digits(current_value) == self._extract_digits(target_value):
                return
        except Exception:
            pass
        raise RuntimeError(f"任务ID未成功写入搜索框: {target_value}")

    async def _wait_task_row_success(
            self,
            page,
            task_id: str,
            progress: ProgressCallback | None,
            *,
            expected_attempts: int | None = None,
            retry_failed_task: bool = False,
            operation_name: str = "素材上传",
            plan: UserGrowthOrderPlan | None = None,
    ) -> None:
        """轮询指定任务行状态；需要时只重试该任务行，累计最多三次。"""
        _ = expected_attempts
        interval_ms = max(int(self.refresh_interval_seconds * 500), 3000)
        attempt = 0
        retry_key = f"{operation_name}:{task_id}"
        task_failure_retries = int(plan.operation_retry_counts.get(retry_key, 0)) if plan else 0
        while True:
            self._raise_if_cancelled()
            attempt += 1
            row = await self._find_task_row(page, task_id)
            row_text = _compact_text(await self._locator_text(row, timeout_ms=3000)) if row else ""
            if "全部成功" in row_text:
                return
            if "失败" in row_text:
                if retry_failed_task and task_failure_retries < OPERATION_TASK_RETRY_LIMIT:
                    task_failure_retries += 1
                    if plan:
                        plan.operation_retry_counts[retry_key] = task_failure_retries
                        self._checkpoint(
                            plan,
                            plan.stage,
                            f"{operation_name}任务 {task_id} 准备执行第 {task_failure_retries}/"
                            f"{OPERATION_TASK_RETRY_LIMIT} 次行级重试",
                        )
                    try:
                        await self._retry_failed_operation_task(
                            page,
                            task_id,
                            progress,
                            operation_name=operation_name,
                            retry_number=task_failure_retries,
                        )
                    except Exception as exc:
                        if self._is_recoverable_session_exception(exc):
                            raise
                        raise UserGrowthOperationTaskFailed(
                            f"{operation_name}任务 {task_id} 第 {task_failure_retries}/"
                            f"{OPERATION_TASK_RETRY_LIMIT} 次行级重试失败：{exc}"
                        ) from exc
                    continue
                if retry_failed_task:
                    raise UserGrowthOperationTaskFailed(
                        f"{operation_name}任务 {task_id} 重试 {OPERATION_TASK_RETRY_LIMIT} 次后仍失败"
                    )
                raise RuntimeError(f"任务 {task_id} 执行失败")
            self._emit(progress, f"等待任务 {task_id} 完成，第 {attempt} 次")
            await self._click_if_present(page, "刷新列表")
            await page.wait_for_timeout(interval_ms)
            await self._search_task_by_id(page, task_id)

    async def _retry_failed_operation_task(
            self,
            page,
            task_id: str,
            progress: ProgressCallback | None,
            *,
            operation_name: str,
            retry_number: int,
    ) -> None:
        """只点击指定失败任务行的重试操作，最多由调用方执行三次。"""
        row = await self._find_task_row(page, task_id)
        if not row:
            await self._search_task_by_id(page, task_id)
            row = await self._find_task_row(page, task_id)
        if not row:
            raise RuntimeError(f"未找到失败任务 {task_id} 对应行，无法重试")

        if not await self._click_first_visible_locator(
                row.get_by_text("重试", exact=True).first,
                row.locator("a:has-text('重试')").first,
                row.locator("button:has-text('重试')").first,
        ):
            raise RuntimeError(f"{operation_name}任务 {task_id} 已失败，但未找到该行重试按钮")

        await page.wait_for_timeout(500)
        dialogs = page.locator(".arco-modal:visible, [role='dialog']:visible")
        try:
            dialog_count = await dialogs.count()
        except Exception:
            dialog_count = 0
        for index in range(dialog_count - 1, -1, -1):
            dialog = dialogs.nth(index)
            dialog_text = _compact_text(await self._locator_text(dialog, timeout_ms=1500))
            if "重试" not in dialog_text:
                continue
            await self._click_first_visible_locator(
                dialog.get_by_text("确定", exact=True).first,
                dialog.get_by_text("重试", exact=True).first,
                dialog.locator("button:has-text('确定')").first,
            )
            break

        self._emit(
            progress,
            f"{operation_name}任务 {task_id} 已失败，已点击该任务行重试"
            f"（{retry_number}/{OPERATION_TASK_RETRY_LIMIT}）",
        )
        self._write_run_log(
            f"[{datetime.now().isoformat(timespec='seconds')}] failed operation task retry: "
            f"operation={operation_name}, task_id={task_id}, "
            f"retry={retry_number}/{OPERATION_TASK_RETRY_LIMIT}"
        )
        self._write_event(
            "failed_operation_task_retry",
            operation=operation_name,
            task_id=task_id,
            retry_number=retry_number,
            retry_limit=OPERATION_TASK_RETRY_LIMIT,
        )
        await page.wait_for_timeout(2000)
        await self._search_task_by_id(page, task_id)

    async def _open_material_list_page(self, page):
        """从任务详情中打开素材/文案列表页，兼容新标签页和当前页跳转。"""
        before_pages = list(page.context.pages)
        before_url = page.url
        for text in ("素材/文案列表查看", "素材列表查看", "文案列表查看", "素材查看"):
            if not await self._click_text_or_locator(page, text):
                continue
            target_page = await self._wait_page_change_or_new_page(page, before_pages, before_url, timeout_ms=15000)
            if target_page:
                await target_page.wait_for_timeout(2500)
                return target_page
        raise RuntimeError("未打开素材/文案列表页")

    async def _read_cids_from_search_input(self, page) -> list[str]:
        """读取素材管理页搜索框中的 CID 列表。"""
        search_input = await self._wait_first_existing(
            page,
            (
                "input[placeholder*='全局搜索']",
                "input[placeholder*='搜索']",
                "input.arco-input",
            ),
            timeout_ms=None,
        )
        if search_input:
            return await self._wait_cids_from_input(search_input)
        text = await self._body_text(page, timeout_ms=5000)
        return self._extract_cids(text)

    async def _fill_cids_from_detail(self, page, items: list[UserGrowthVideoItem]) -> None:
        """进入详情页读取 CID，并回写到素材条目。"""
        await self._click_if_present(page, "查看详情")
        await page.wait_for_timeout(3000)
        cids = await self._copy_or_read_cids(page)
        if not cids:
            await self._snapshot_error(
                page,
                "cid_not_found",
                extra=f"items={len(items)}",
            )
            raise RuntimeError("未读取到 CID")
        for item, cid in zip(items, cids):
            item.cid = cid
            item.cid_material_type = await self._read_material_type_by_cid(page, cid) or item.material_type
            item.status = "success"
            item.message = "上传并送审成功"

    async def _read_cids_from_task_detail_page(
            self,
            page,
            items: list[UserGrowthVideoItem],
            message: str,
    ) -> None:
        """从任务详情页或素材列表页读取 CID，并按指定备注写入条目。"""
        cids: list[str] = []
        cid_page = page
        try:
            material_page = await self._open_material_list_page(page)
            cids = await self._read_cids_from_search_input(material_page)
            cid_page = material_page
        except Exception:
            cids = await self._copy_or_read_cids(page)

        if not cids:
            raise RuntimeError("未读取到 CID")
        if len(cids) < len(items):
            raise RuntimeError(f"读取到的 CID 数量不足：期望 {len(items)}，实际 {len(cids)}")

        for item, cid in zip(items, cids):
            item.cid = cid
            item.cid_material_type = await self._read_material_type_by_cid(cid_page, cid) or item.material_type
            item.status = "success"
            item.message = message

    async def _copy_or_read_cids(self, page) -> list[str]:
        """优先使用一键复制对象 ID，失败时从页面文本中提取 CID。"""
        try:
            await self._click_text(page, "一键复制对象id")
            await page.wait_for_timeout(500)
            text = await page.evaluate(
                "navigator.clipboard && navigator.clipboard.readText ? navigator.clipboard.readText() : ''")
        except Exception:
            text = ""
        if not text:
            text = await page.locator("body").inner_text(timeout=5000)
        return self._extract_cids(text)

    async def _read_material_type_by_cid(self, page, cid: str) -> str:
        """按 CID 查看素材详情，读取分类标签作为回填素材类型。"""
        try:
            row = page.locator(f"tr:has-text('{cid}')").first
            if await row.count():
                button = row.locator("text=查看素材").first
                if await button.count():
                    await button.click()
                    await page.wait_for_timeout(2000)
                    body = await page.locator("body").inner_text(timeout=5000)
                    match = re.search(r"分类标签[:：]?\s*([^\n]+)", body)
                    await self._click_if_present(page, "关闭")
                    if match:
                        return display_material_from_label(match.group(1))
        except Exception:
            return ""
        return ""

    async def _wait_selected_count(self, page, minimum: int, timeout_ms: int = 8000) -> bool:
        """等待页面“已选中”数量达到指定值。"""

        async def enough_selected() -> bool:
            return await self._selected_count(page) >= minimum

        return bool(await self._wait_for_result(enough_selected, timeout_ms=timeout_ms, interval_ms=500))

    async def _selected_count(self, page) -> int:
        """从页面文本中读取当前已选中的行数。"""
        body = await self._body_text(page, timeout_ms=2000)
        match = re.search(r"已(?:选中|选择)\s*(\d+)", body)
        if match:
            return int(match.group(1))
        return 0

    async def _select_row_by_file_name(self, page, file_name: str) -> bool:
        """通过文件名定位列表行，并点击该行的 Arco 复选框。"""
        for locator in (
                page.locator(f"tr:has-text('{file_name}')").first,
                page.locator(f"[role='row']:has-text('{file_name}')").first,
                page.locator(f".arco-table-tr:has-text('{file_name}')").first,
                page.locator(f"[class*='arco-table-tr']:has-text('{file_name}')").first,
                page.locator(f"[class*='arco-table-row']:has-text('{file_name}')").first,
                page.locator(f".ant-table-row:has-text('{file_name}')").first,
        ):
            try:
                if await locator.count() and await locator.is_visible():
                    return await self._click_visible_checkbox_box(
                        locator,
                        "label.arco-checkbox, .arco-checkbox-mask-wrapper, .arco-checkbox-mask, .arco-checkbox, label",
                    )
            except Exception:
                continue
        text_locator = page.get_by_text(file_name, exact=True).first
        for xpath in (
                "xpath=ancestor::tr[1]",
                "xpath=ancestor::*[@role='row'][1]",
                "xpath=ancestor::*[contains(@class, 'arco-table-tr')][1]",
                "xpath=ancestor::*[contains(@class, 'arco-table-row')][1]",
                "xpath=ancestor::*[contains(@class, 'ant-table-row')][1]",
        ):
            try:
                row = text_locator.locator(xpath)
                if await row.count() and await row.is_visible():
                    return await self._click_visible_checkbox_box(
                        row,
                        "label.arco-checkbox, .arco-checkbox-mask-wrapper, .arco-checkbox-mask, .arco-checkbox, label",
                    )
            except Exception:
                continue
        return await self._select_row_by_file_name_dom(page, file_name)

    async def _select_row_by_file_name_dom(self, page, file_name: str) -> bool:
        """按文件名定位行并点击 Arco checkbox 的兜底。"""
        # 列表实现可能是 table、div row 或虚拟表格，这里统一从可见行里按文件名过滤。
        rows = page.locator(
            ".arco-table-tr, [class*='arco-table-tr'], [class*='arco-table-row'], tr, [role='row']"
        ).filter(has_text=file_name)
        for row in await self._visible_locators(rows, limit=30):
            if await self._click_visible_checkbox_box(
                    row,
                    "label.arco-checkbox, .arco-checkbox-mask-wrapper, .arco-checkbox-mask",
            ):
                return True
        return False

    async def _click_visible_checkbox_box(self, scope, selector: str) -> bool:
        """在指定区域内点击可见的复选框外壳，并确认是否选中。"""
        boxes = scope.locator(selector)
        try:
            count = min(await boxes.count(), 30)
        except Exception:
            return False
        for index in range(count):
            box = boxes.nth(index)
            try:
                if not await box.is_visible():
                    continue
                if await self._checkbox_box_is_checked(box):
                    return True
                await box.scroll_into_view_if_needed(timeout=3000)
                await box.click(force=True, timeout=5000)
                await self._sleep(0.2)
                if await self._checkbox_box_is_checked(box):
                    return True
            except Exception:
                continue
        return False

    async def _checkbox_box_is_checked(self, box) -> bool:
        """判断 Arco/原生复选框外壳是否已经处于选中状态。"""
        try:
            input_box = box.locator("input[type='checkbox']").first
            if await input_box.count():
                return await input_box.is_checked()
        except Exception:
            pass
        # Arco checkbox 经常把状态挂在外层 class 上，而不是原生 input checked。
        for locator in (box, box.locator("xpath=ancestor::*[contains(@class, 'arco-checkbox')][1]")):
            try:
                class_name = await locator.get_attribute("class")
                if class_name and "checked" in class_name:
                    return True
            except Exception:
                continue
        return False

    async def _safe_goto(self, page, url: str) -> None:
        """打开页面；网络异常时无限退避等待，不关闭浏览器。"""
        attempt = 0
        delay_seconds = 2.0
        while True:
            self._raise_if_cancelled()
            attempt += 1
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                return
            except Exception as exc:
                if not self._is_network_transient_exception(exc):
                    raise RuntimeError(f"页面打开失败：{url}: {exc}") from exc
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] network wait: "
                    f"operation=goto, url={url}, attempt={attempt}, "
                    f"error={type(exc).__name__}: {exc}"
                )
                if attempt == 1 or attempt % 5 == 0:
                    self._emit(None, f"网络异常，保持浏览器打开等待恢复：{url}（第 {attempt} 次）")
                await self._sleep(delay_seconds)
                delay_seconds = min(delay_seconds * 2, 30.0)

    async def _page_is_blank_or_loading(self, page) -> bool:
        """识别白屏、根节点空白或持续加载状态，避免误判为业务失败后关闭浏览器。"""
        try:
            if page.is_closed():
                return True
        except Exception:
            return True
        try:
            url = str(page.url or "").lower()
            if url in {"", "about:blank"} or url.startswith("chrome-error://"):
                return True
        except Exception:
            return True
        try:
            body_text = _compact_text(await self._body_text(page, timeout_ms=2500))
            if not body_text or body_text in {"加载中", "正在加载", "loading"}:
                return True
            root = page.locator("#root").first
            if await root.count():
                root_text = _compact_text(await self._locator_text(root, timeout_ms=1500))
                if not root_text:
                    return True
            for selector in (
                    ".arco-spin-loading",
                    ".arco-spin-dot-loading",
                    "[class*='loading'][class*='spin']",
            ):
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    return True
        except Exception:
            return True
        return False

    @staticmethod
    def _explicit_page_error_marker(body: str) -> str:
        """返回页面明确展示的可恢复错误；普通空白或加载慢不算错误。"""
        markers = (
            "请求失败",
            "加载失败",
            "搜索失败",
            "网络异常",
            "网络错误",
            "系统异常",
            "服务异常",
            "服务错误",
        )
        return next((marker for marker in markers if marker in str(body or "")), "")

    @staticmethod
    def _is_network_transient_exception(exc: BaseException) -> bool:
        """判断异常是否属于应无限等待的网络/导航抖动。"""
        message = f"{type(exc).__name__}: {exc}".lower()
        network_markers = (
            "err_connection_closed",
            "err_connection_reset",
            "err_connection_refused",
            "err_connection_timed_out",
            "err_timed_out",
            "err_network_changed",
            "err_internet_disconnected",
            "err_name_not_resolved",
            "err_address_unreachable",
            "connection closed",
            "connection reset",
            "connection refused",
            "network changed",
            "internet disconnected",
            "name not resolved",
            "socket hang up",
            "proxy connection failed",
            "connection aborted",
            "network error",
            "failed to fetch",
            "fetch failed",
        )
        if any(marker in message for marker in network_markers):
            return True
        if "timeout" in message or "timed out" in message:
            navigation_markers = (
                "page.goto",
                "page.reload",
                "navigation",
                "load state",
                "domcontentloaded",
            )
            return any(marker in message for marker in navigation_markers)
        return False

    @staticmethod
    def _is_session_closed_exception(exc: BaseException) -> bool:
        """判断 Playwright 浏览器、上下文或页面是否意外断开。"""
        message = f"{type(exc).__name__}: {exc}".lower()
        return any(
            marker in message
            for marker in (
                "targetclosederror",
                "target page, context or browser has been closed",
                "page has been closed",
                "context has been closed",
                "browser has been closed",
                "browser has been disconnected",
                "browsercontext has been closed",
            )
        )

    def _is_recoverable_session_exception(self, exc: BaseException) -> bool:
        """网络抖动或非用户主动造成的浏览器断开都进入无限恢复等待。"""
        if self._cancel_requested():
            return False
        return self._is_network_transient_exception(exc) or self._is_session_closed_exception(exc)

    async def _wait_for_network_recovery(
            self,
            page,
            context,
            progress: ProgressCallback | None,
            operation: str,
            *,
            playwright=None,
            session: dict | None = None,
    ):
        """网络异常或目标断开时保持流程存活，等待首页恢复后返回可用页面。"""
        attempt = 0
        delay_seconds = 2.0
        while True:
            self._raise_if_cancelled()
            attempt += 1
            try:
                await self._capture_storage_state(context)
                page_closed = False
                try:
                    page_closed = page.is_closed()
                except Exception:
                    page_closed = True

                if page_closed:
                    await self._raise_if_user_closed_browser(
                        RuntimeError("TargetClosedError: headed browser page has been closed"),
                        progress,
                    )
                    try:
                        page = await context.new_page()
                    except Exception as exc:
                        await self._raise_if_user_closed_browser(exc, progress)
                        if not self._is_session_closed_exception(exc) or playwright is None:
                            raise
                        old_browser = (session or {}).get("browser")
                        try:
                            if old_browser:
                                await self._close_browser_intentionally(old_browser)
                        except Exception:
                            pass
                        browser = await self._launch_browser(playwright)
                        context = await browser.new_context(**self._context_options())
                        page = await context.new_page()
                        if session is not None:
                            session["browser"] = browser
                        await self._login(page, progress)
                        await self._capture_storage_state(context)
                        await self._enable_post_login_resource_blocking(context, progress)
                    self._wrap_page_speed(page)
                    page.set_default_timeout(self.timeout_ms)
                    page.set_default_navigation_timeout(self.timeout_ms)
                await self._safe_goto(page, HOME_URL)
                await page.wait_for_timeout(1500)
                if not await self._looks_logged_in(page):
                    await self._login(page, progress)
                    await self._capture_storage_state(context)
                    await self._enable_post_login_resource_blocking(context, progress)
                self._emit(progress, f"网络已恢复，继续执行：{operation}")
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] network recovered: "
                    f"operation={operation}, attempt={attempt}, url={page.url}"
                )
                return page
            except Exception as exc:
                if not self._is_recoverable_session_exception(exc):
                    raise
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] network recovery wait: "
                    f"operation={operation}, attempt={attempt}, "
                    f"error={type(exc).__name__}: {exc}"
                )
                if attempt == 1 or attempt % 5 == 0:
                    self._emit(progress, f"网络仍未恢复，浏览器保持打开继续等待：{operation}（第 {attempt} 次）")
                await self._sleep(delay_seconds)
                delay_seconds = min(delay_seconds * 2, 30.0)

    async def _click_text(self, page, text: str) -> None:
        """按文本内容点击按钮、链接或普通可点击文字。"""
        if await self._click_first_visible_locator(
                page.get_by_text(text, exact=True).first,
                page.get_by_text(text, exact=False).first,
                page.locator(f"button:has-text('{text}')").first,
                page.locator(f"a:has-text('{text}')").first,
        ):
            return
        raise RuntimeError(f"未找到可点击文本：{text}")

    async def _click_first(self, page, selectors: tuple[str, ...]) -> None:
        """按选择器顺序点击第一个可见控件。"""
        if await self._click_first_visible_locator(*(page.locator(selector).first for selector in selectors)):
            return
        raise RuntimeError(f"未找到可点击控件：{selectors}")

    async def _click_first_visible_locator(self, *locators) -> bool:
        """点击一组 locator 中第一个可见元素。"""
        for locator in locators:
            if await self._click_locator(locator):
                return True
        return False

    async def _click_locator(self, locator) -> bool:
        """点击单个可见 locator，失败时返回 False。"""
        try:
            if not await locator.count() or not await locator.is_visible():
                return False
            await locator.scroll_into_view_if_needed(timeout=3000)
            await locator.click(force=True)
            return True
        except Exception as exc:
            if self._is_session_closed_exception(exc):
                raise
            return False

    async def _click_locator_center(self, page, locator) -> bool:
        """用真实鼠标点击 locator 中心点。"""
        try:
            if not await locator.count() or not await locator.is_visible():
                return False
            await locator.scroll_into_view_if_needed(timeout=3000)
            box = await locator.bounding_box(timeout=3000)
            if not box:
                return False
            await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            return True
        except Exception:
            return False

    async def _visible_locators(self, locators, limit: int = 30) -> list:
        """返回 locator 集合中可见的前若干个元素。"""
        try:
            count = min(await locators.count(), limit)
        except Exception as exc:
            if self._is_session_closed_exception(exc):
                raise
            return []
        visible = []
        for index in range(count):
            locator = locators.nth(index)
            try:
                if await locator.is_visible():
                    visible.append(locator)
            except Exception as exc:
                if self._is_session_closed_exception(exc):
                    raise
                continue
        return visible

    async def _click_if_present(self, page, text: str) -> None:
        """如果页面上存在某个文本按钮就点击，不存在则忽略。"""
        try:
            await self._click_text(page, text)
        except RuntimeError as exc:
            if self._is_recoverable_session_exception(exc):
                raise
            return

    async def _click_exact_text_in_locator_when_visible(self, container, text: str, timeout_ms: int = 3000) -> bool:
        """等待容器内某个精确文本真的出现后再点击。"""
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        locator = container.locator(f"text={text}")
        while asyncio.get_event_loop().time() < deadline:
            for candidate in await self._visible_locators(locator, limit=20):
                try:
                    await candidate.click(timeout=1000)
                    return True
                except Exception:
                    continue
            await asyncio.sleep(0.2)
        return False

    async def _click_text_or_locator(self, page, text: str) -> bool:
        """点击文本；若不存在则返回 False。"""
        try:
            await self._click_text(page, text)
            return True
        except RuntimeError as exc:
            if self._is_recoverable_session_exception(exc):
                raise
            return False

    async def _fill_first(self, page, selectors: tuple[str, ...], value: str) -> None:
        """找到第一个可见输入框并输入指定文本。"""
        locator = await self._first_existing(page, selectors)
        if not locator:
            raise RuntimeError(f"未找到输入框：{selectors}")
        await self._type_into_locator(locator, page, value)

    async def _type_into_locator(self, locator, page, value: str) -> None:
        """兼容 fill、键盘输入和坐标点击三种方式向输入框写值。

        优先 fill 一次性写入，避免逐字键入对账号/密码/订单ID
        这类短文本带来的额外耗时和触发前端额外逻辑。
        """
        try:
            await locator.fill(value, timeout=5000)
            return
        except Exception:
            pass
        try:
            await locator.click(force=True, timeout=5000)
            await self._keyboard_type(page, value)
            return
        except Exception:
            pass
        box = await locator.bounding_box(timeout=5000)
        if not box:
            raise RuntimeError("输入框不可点击")
        await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        await self._keyboard_type(page, value)

    async def _first_existing(self, page, selectors: tuple[str, ...]):
        """返回第一个存在且可见的元素。"""
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.count() and await locator.is_visible():
                    return locator
            except Exception as exc:
                if self._is_session_closed_exception(exc):
                    raise
                continue
        return None

    async def _first_attached(self, page, selectors: tuple[str, ...]):
        """返回第一个已挂载到 DOM 的元素，适合隐藏 file input。"""
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.count():
                    return locator
            except Exception as exc:
                if self._is_session_closed_exception(exc):
                    raise
                continue
        return None

    async def _wait_first_existing(self, page, selectors: tuple[str, ...], timeout_ms: int = 20000):
        """轮询等待某组选择器中任意一个可见元素出现。"""

        async def find_locator():
            return await self._first_existing(page, selectors)

        return await self._wait_for_result(find_locator, timeout_ms=timeout_ms, interval_ms=800)

    async def _locator_text(self, locator, timeout_ms: int = 3000) -> str:
        """安全读取 locator 文本。"""
        if not locator:
            return ""
        try:
            return await locator.inner_text(timeout=timeout_ms)
        except Exception as exc:
            if self._is_session_closed_exception(exc):
                raise
            return ""

    async def _body_text(self, page, timeout_ms: int = 5000) -> str:
        """读取页面 body 文本；读取失败时返回空字符串。"""
        try:
            return await page.locator("body").inner_text(timeout=timeout_ms)
        except Exception as exc:
            if self._is_session_closed_exception(exc):
                raise
            return ""

    async def _wait_for_page_text(
            self,
            page,
            texts: tuple[str, ...],
            *,
            timeout_ms: int | None = 15000,
            raise_on_timeout: bool = True,
    ) -> bool:
        """等待页面出现指定文本，必要时超时抛错。"""

        async def has_text() -> bool:
            body = await self._body_text(page, timeout_ms=2000)
            return any(text in body for text in texts)

        if await self._wait_for_result(has_text, timeout_ms=timeout_ms, interval_ms=800):
            return True
        if raise_on_timeout:
            raise RuntimeError(f"页面未出现预期内容：{', '.join(texts)}")
        return False

    async def _wait_for_result(self, producer, *, timeout_ms: int | None, interval_ms: int = 800):
        """按固定间隔轮询异步函数，直到返回真值或超时。"""
        deadline = None if timeout_ms is None else asyncio.get_event_loop().time() + timeout_ms / 1000
        while deadline is None or asyncio.get_event_loop().time() < deadline:
            result = await producer()
            if result:
                return result
            await self._sleep(interval_ms / 1000)
        return None

    async def _retry(
            self,
            operation,
            *,
            description: str,
            max_attempts: int = 3,
            base_interval_ms: int = 5000,
    ):
        """
        异步操作重试：
        - 最大尝试次数默认3次
        - 指数退避等待
          第1次失败 -> 2s
          第2次失败 -> 4s
        """

        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                result = await operation(attempt)

                # 返回False表示失败，其他认为成功
                if result is not False:
                    return result

            except Exception as exc:
                if isinstance(exc, UserGrowthFatalPageError):
                    raise
                if self._is_recoverable_session_exception(exc):
                    # 网络错误不能被有限次数重试包装成业务失败；交给 run() 保持
                    # 浏览器存活并做无限指数退避恢复。
                    raise
                last_error = exc

            if attempt < max_attempts:
                wait_ms = base_interval_ms * (2 ** (attempt - 1))

                await self._sleep(
                    wait_ms / 1000
                )

        raise RuntimeError(
            f"{description} failed after {max_attempts} attempts"
        ) from last_error

    async def _wait_page_change_or_new_page(
            self,
            page,
            before_pages: list,
            before_url: str,
            timeout_ms: int | None = 15000,
    ):
        """等待点击后的当前页跳转或新标签页打开。"""
        deadline = None if timeout_ms is None else asyncio.get_event_loop().time() + timeout_ms / 1000
        before_ids = {id(candidate) for candidate in before_pages}
        context = page.context
        while deadline is None or asyncio.get_event_loop().time() < deadline:
            try:
                pages = list(context.pages)
            except Exception:
                pages = []
            for candidate in reversed(pages):
                try:
                    if candidate.is_closed():
                        continue
                    await candidate.wait_for_load_state("domcontentloaded", timeout=1000)
                except Exception:
                    continue
                try:
                    candidate_url = candidate.url
                except Exception:
                    continue
                is_new_page = id(candidate) not in before_ids and candidate_url != "about:blank"
                if is_new_page:
                    self._wrap_page_speed(candidate)
                    return candidate
            try:
                if not page.is_closed() and page.url != before_url:
                    self._wrap_page_speed(page)
                    return page
            except Exception:
                pass
            await self._sleep(0.5)
        return None

    async def _first_table_row(self, page):
        """读取当前页第一个可见表格数据行。"""
        rows = page.locator("tbody tr")
        try:
            count = min(await rows.count(), 10)
        except Exception:
            return None
        for index in range(count):
            row = rows.nth(index)
            try:
                if await row.is_visible():
                    return row
            except Exception:
                continue
        return None

    async def _find_task_row(self, page, task_id: str):
        """按任务ID匹配任务列表中的数据行。"""
        rows = page.locator("tbody tr")
        try:
            count = min(await rows.count(), 20)
        except Exception:
            return None
        compact_task_id = _compact_text(task_id)
        for index in range(count):
            row = rows.nth(index)
            row_text = _compact_text(await self._locator_text(row, timeout_ms=2000))
            if compact_task_id and compact_task_id in row_text:
                return row
        return None

    def _extract_digits(self, text: str) -> str:
        """提取文本中第一个连续任务ID数字。"""
        compact = re.sub(r"\s+", "", text or "")
        match = re.search(r"\b\d{7,}\b", compact)
        return match.group(0) if match else ""

    def _extract_cids(self, text: str) -> list[str]:
        """从任意文本中提取 CID。"""
        return re.findall(r"\b[a-f0-9]{24,40}\b", text or "", flags=re.IGNORECASE)

    async def _wait_and_click_text(
            self,
            page,
            text: str,
            timeout_ms: int | None = 30000,
    ):
        """等待同名文本中的可见节点出现并点击。"""
        deadline = None if timeout_ms is None else asyncio.get_event_loop().time() + timeout_ms / 1000
        attempt = 0
        while deadline is None or asyncio.get_event_loop().time() < deadline:
            attempt += 1
            locator_groups = (
                page.get_by_role("button", name=text, exact=True),
                page.locator("button").filter(has_text=text),
                page.locator("[role='button']").filter(has_text=text),
                page.get_by_text(text, exact=True),
            )
            for locator in locator_groups:
                for candidate in await self._visible_locators(locator, limit=30):
                    try:
                        await candidate.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    try:
                        await candidate.click(force=True, timeout=3000)
                        return
                    except Exception:
                        pass
                    try:
                        button = candidate.locator("xpath=ancestor::button[1]").first
                        if await button.count() and await button.is_visible():
                            await button.click(force=True, timeout=3000)
                            return
                    except Exception:
                        continue
            if attempt % 10 == 0:
                self._write_run_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] waiting visible clickable text: "
                    f"text={text}, attempt={attempt}, url={page.url}"
                )
            await self._sleep(0.5)
        raise RuntimeError(f"等待可见文本失败: {text}")

    async def _click_text_and_wait_page(
            self,
            page,
            text: str,
            *,
            timeout_ms: int | None = 30000,
    ):
        """点击文本后等待新标签页出现或当前页跳转；timeout_ms=None 时不设总超时。"""
        before_pages = list(page.context.pages)
        before_url = page.url
        await self._wait_and_click_text(page, text, timeout_ms=timeout_ms)
        target_page = await self._wait_page_change_or_new_page(
            page,
            before_pages,
            before_url,
            timeout_ms=timeout_ms,
        )
        if not target_page:
            raise RuntimeError(f"点击{text}后未进入目标页面")
        try:
            await target_page.bring_to_front()
        except Exception:
            pass
        return target_page

    async def _wait_cids_from_input(
            self,
            locator,
            *,
            timeout_ms: int | None = None,
            interval_ms: int = 800,
    ) -> list[str]:
        """等待输入框里真正出现 CID 再读取。"""

        async def read_cids():
            try:
                text = await locator.input_value(timeout=2000)
            except Exception:
                try:
                    text = await self._locator_text(locator, timeout_ms=2000)
                except Exception:
                    return None
            cids = self._extract_cids(text)
            return cids or None

        cids = await self._wait_for_result(
            read_cids,
            timeout_ms=timeout_ms,
            interval_ms=interval_ms,
        )
        return cids or []

    async def _click_creative_unit_select_all_checkbox(self, page):
        """在 unit tab 全选所有行。"""
        candidates = (
            page.locator(".arco-table thead .arco-checkbox-mask").first,
            page.locator(".arco-table thead .arco-checkbox-mask-wrapper").first,
            page.locator(".arco-table thead label.arco-checkbox").first,
            page.locator(".arco-table thead .arco-checkbox").first,
        )

        async def click_select_all() -> bool:
            for checkbox in candidates:
                try:
                    if not await checkbox.count() or not await checkbox.is_visible():
                        continue
                    await checkbox.scroll_into_view_if_needed(timeout=3000)
                    if await self._checkbox_box_is_checked(checkbox):
                        return True
                    if not await self._click_locator(checkbox):
                        continue
                    await page.wait_for_timeout(1200)
                    if await self._checkbox_box_is_checked(checkbox):
                        return True
                except Exception:
                    continue
            return False

        while not await click_select_all():
            await page.wait_for_timeout(1200)
        await page.wait_for_timeout(1500)

    async def _wait_and_click_table_select_all(
            self,
            page,
            *,
            timeout_ms: int | None = None,
    ) -> None:
        """等待表格表头全选复选框出现并点击；默认不设总超时。"""

        candidates = (
            page.locator(".arco-table thead input[type='checkbox']").first,
            page.locator(".arco-table thead .arco-checkbox-mask").first,
            page.locator(".arco-table thead .arco-checkbox-mask-wrapper").first,
            page.locator(".arco-table thead label.arco-checkbox").first,
            page.locator(".arco-table thead .arco-checkbox").first,
            page.locator(".arco-table-header input[type='checkbox']").first,
            page.locator(".arco-table-header .arco-checkbox").first,
            page.locator(".arco-table-th input[type='checkbox']").first,
            page.locator(".arco-table-th .arco-checkbox").first,
            # 兜底：弹层里也可能直接放一个「全选」按钮
            page.get_by_role("button", name="全选").first,
            page.locator("button:has-text('全选')").first,
        )

        async def attempt() -> bool:
            for checkbox in candidates:
                try:
                    if not await checkbox.count() or not await checkbox.is_visible():
                        continue
                    await checkbox.scroll_into_view_if_needed(timeout=3000)
                    # 已是选中态直接视为成功
                    if await self._checkbox_box_is_checked(checkbox):
                        return True
                    if not await self._click_locator(checkbox):
                        continue
                    await page.wait_for_timeout(800)
                    if await self._checkbox_box_is_checked(checkbox):
                        return True
                except Exception:
                    continue
            return False

        deadline = None if timeout_ms is None else asyncio.get_event_loop().time() + timeout_ms / 1000
        while deadline is None or asyncio.get_event_loop().time() < deadline:
            if await attempt():
                return
            await page.wait_for_timeout(2000)
        raise RuntimeError("等待表格全选框超时")

    async def _snapshot(self, page, name: str, *, screenshot: bool = False) -> None:
        """保存当前页面文本和截图。

        正常流程的快照（screenshot=False）直接跳过，不写 .txt 也不写 .png，
        避免 debug_dir 被无意义快照塞满。
        错误场景请走 _snapshot_error，里面会用 screenshot=True 走完整流程。
        """
        if not screenshot:
            return
        if not self.debug_dir:
            return
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(char if char.isalnum() or char in ("_", "-") else "_" for char in name)
        try:
            body = await page.locator("body").inner_text(timeout=3000)
        except Exception as exc:
            body = f"<read body failed: {exc}>"
        (self.debug_dir / f"{safe_name}.txt").write_text(
            f"URL: {page.url}\n\n{body}", encoding="utf-8"
        )
        try:
            await page.screenshot(
                path=str(self.debug_dir / f"{safe_name}.png"),
                full_page=True,
            )
        except Exception:
            pass

    async def _snapshot_error(
            self,
            page,
            name: str,
            exc: BaseException | None = None,
            *,
            extra: str | None = None,
    ) -> None:
        """错误场景专用：截图 + 写页面文本 + 写详细错误日志到 run.log。

        exc 不为空时附带异常类型、消息和堆栈到 run.log。
        extra 是附加的纯文本上下文（例如 plan/order 标识）。
        """
        await self._snapshot(page, name, screenshot=True)
        if exc is None and extra is None:
            return
        lines = [
            f"[{datetime.now().isoformat(timespec='seconds')}] ERROR snapshot: {name}",
        ]
        if page is not None:
            try:
                lines.append(f"  url: {page.url}")
            except Exception:
                lines.append("  url: <unavailable>")
        if extra:
            lines.append(f"  context: {extra}")
        if exc is not None:
            lines.append(f"  exc_type: {type(exc).__name__}")
            lines.append(f"  exc_msg: {exc}")
            lines.append("  traceback:")
            lines.append("    " + "\n    ".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ).rstrip())
        self._write_run_log("\n".join(lines))
        try:
            page_url = page.url if page is not None else ""
        except Exception:
            page_url = ""
        self._write_event(
            "error_snapshot",
            name=name,
            url=page_url,
            context=extra or "",
            error_type=type(exc).__name__ if exc is not None else "",
            error_message=str(exc) if exc is not None else "",
        )

    def _emit(self, progress: ProgressCallback | None, message: str) -> None:
        """向调用方发送一条进度消息。"""
        if progress:
            progress(message)

    def _cancel_requested(self) -> bool:
        return bool(self.cancel_event and self.cancel_event.is_set())

    def _raise_if_cancelled(self) -> None:
        if self._cancel_requested():
            raise UserGrowthCancelled("任务已取消")
        if self._user_closed_headed_page:
            exit_code = self._read_browser_process_exit_code()
            close_age = time.monotonic() - self._user_closed_headed_page_at
            if (
                    exit_code == 0
                    or (
                        not self._browser_disconnected
                        and exit_code in {None, 259}
                        and close_age >= 0.75
                    )
            ):
                raise UserGrowthCancelled("检测到用户主动关闭浏览器，任务已停止，不自动重启")
        if not self._browser_disconnected:
            return
        exit_code = self._read_browser_process_exit_code()
        if exit_code == 0:
            raise UserGrowthCancelled("检测到用户主动关闭浏览器，任务已停止，不自动重启")
        if exit_code != 259 or time.monotonic() - self._browser_disconnected_at >= 2.0:
            raise RuntimeError(
                "Browser has been disconnected unexpectedly; "
                f"exit_code={exit_code if exit_code is not None else 'unknown'}"
            )

    async def _watch_cancel(self, session: dict, progress: ProgressCallback | None = None) -> None:
        """后台监听取消事件，取消时关闭浏览器以打断 Playwright 无限等待。"""
        while not self._cancel_requested():
            await asyncio.sleep(0.5)
        self._emit(progress, "收到取消请求，正在关闭浏览器")
        try:
            browser = session.get("browser")
            if browser:
                await self._close_browser_intentionally(browser)
        except Exception:
            pass

    def _scale_ms(self, delay_ms: int | float, *, minimum_ms: int = 0) -> int:
        """按全局操作速度系数缩放毫秒级等待时长。"""
        scaled = int(round(float(delay_ms) / self.operation_speed_factor))
        return max(minimum_ms, scaled)

    def _scale_seconds(self, delay_seconds: int | float, *, minimum_seconds: float = 0.0) -> float:
        """按全局操作速度系数缩放秒级等待时长。"""
        scaled = float(delay_seconds) / self.operation_speed_factor
        return max(minimum_seconds, scaled)

    def _wrap_page_speed(self, page) -> None:
        """包装 page.wait_for_timeout，使现有页面等待自动遵循全局速度。"""
        self._track_page_lifecycle(page)
        if getattr(page, "_usergrowth_speed_wrapped", False):
            return
        original_wait_for_timeout = page.wait_for_timeout

        async def scaled_wait_for_timeout(delay_ms):
            self._raise_if_cancelled()
            remaining_ms = self._scale_ms(delay_ms)
            while remaining_ms > 0:
                self._raise_if_cancelled()
                step_ms = min(remaining_ms, 500)
                await original_wait_for_timeout(step_ms)
                remaining_ms -= step_ms
            self._raise_if_cancelled()

        page.wait_for_timeout = scaled_wait_for_timeout
        page._usergrowth_speed_wrapped = True

    async def _sleep(self, delay_seconds: int | float, *, minimum_seconds: float = 0.0) -> None:
        """使用全局操作速度系数执行 sleep。"""
        remaining = self._scale_seconds(delay_seconds, minimum_seconds=minimum_seconds)
        while remaining > 0:
            self._raise_if_cancelled()
            step = min(remaining, 0.5)
            await asyncio.sleep(step)
            remaining -= step
        self._raise_if_cancelled()

    async def _keyboard_type(self, page, value: str, delay_ms: int = 80) -> None:
        """按全局操作速度系数控制键盘输入节奏。"""
        await page.keyboard.type(value, delay=self._scale_ms(delay_ms))

    async def _wait_creative_unit_table_ready(self, page, timeout_ms: int | None) -> None:
        """等待创意单元页表格可操作，不设总超时。"""

        async def table_ready():
            for locator in (
                    page.locator(".arco-table thead .arco-checkbox-mask").first,
                    page.locator(".arco-table thead .arco-checkbox-mask-wrapper").first,
                    page.locator(".arco-table thead label.arco-checkbox").first,
                    page.locator(".arco-table thead .arco-checkbox").first,
            ):
                try:
                    if await locator.count() and await locator.is_visible():
                        return True
                except Exception:
                    continue
            return False

        if await self._wait_for_result(table_ready, timeout_ms=timeout_ms, interval_ms=500):
            return
        raise RuntimeError("确认提交后等待创意单元表格渲染超时")
