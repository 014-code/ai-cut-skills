from __future__ import annotations

import contextlib
import importlib
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Callable, Iterable


APP_TITLE = "UserGrowth 自动化上传桌面端"


def _prepare_imports() -> None:
    """Allow the launcher to run from source as well as from PyInstaller."""
    if getattr(sys, "frozen", False):
        return
    skill_root = Path(__file__).resolve().parents[1]
    scripts = skill_root / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))


class _QueueStream:
    encoding = "utf-8"

    def __init__(self, output_queue: queue.Queue[tuple[str, str]]) -> None:
        self.output_queue = output_queue

    def write(self, value: str) -> int:
        if value:
            self.output_queue.put(("line", value))
        return len(value)

    def flush(self) -> None:
        return None

    def reconfigure(self, **_kwargs: object) -> None:
        return None


class _RunnerThread(threading.Thread):
    def __init__(self, runner_name: str, argv: list[str], output_queue: queue.Queue[tuple[str, str]]) -> None:
        super().__init__(daemon=True)
        self.runner_name = runner_name
        self.argv = argv
        self.output_queue = output_queue

    def run(self) -> None:
        stream = _QueueStream(self.output_queue)
        code = 1
        try:
            _prepare_imports()
            runner = importlib.import_module(self.runner_name)
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                try:
                    result = runner.main(self.argv)
                except SystemExit as exc:
                    result = exc.code
            code = int(result) if isinstance(result, int) else 0
        except BaseException as exc:  # noqa: BLE001
            self.output_queue.put(("line", f"桌面端启动流程失败：{type(exc).__name__}: {exc}\n"))
        finally:
            self.output_queue.put(("done", str(code)))


def _split_lines(value: str) -> list[str]:
    return [line.strip() for line in value.replace("，", "\n").splitlines() if line.strip()]


def _add_value(argv: list[str], flag: str, value: str) -> None:
    value = value.strip()
    if value:
        argv.extend((flag, value))


class UserGrowthDesktop(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1040x780")
        self.minsize(900, 650)
        self.output_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.runner_thread: _RunnerThread | None = None
        self._build_variables()
        self._build_ui()
        self.after(100, self._drain_output)

    def _build_variables(self) -> None:
        local_app = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        default_output = local_app / "AIVideoEditor" / "UserGrowthAutomation"
        self.workflow = tk.StringVar(value="soda_music")
        self.video_folder = tk.StringVar()
        self.manifest = tk.StringVar()
        self.resume_task = tk.StringVar()
        self.output_root = tk.StringVar(value=str(default_output))
        self.order_id = tk.StringVar()
        self.account = tk.StringVar()
        self.password = tk.StringVar()
        self.song_excel = tk.StringVar()
        self.backfill_excel = tk.StringVar()
        self.all_videos = tk.BooleanVar(value=True)
        self.split_by_song = tk.BooleanVar(value=False)
        self.recursive = tk.BooleanVar(value=True)
        self.headless = tk.BooleanVar(value=False)
        self.upload_concurrency = tk.StringVar(value="1")
        self.task_name = tk.StringVar(value="usergrowth_upload")
        self.storage_state = tk.StringVar()
        self.storage_state_output = tk.StringVar()
        self.redfruit_bid_map = tk.StringVar()
        self.redfruit_genre = tk.StringVar()
        self.redfruit_layout = tk.StringVar()
        self.redfruit_material_mode = tk.StringVar()
        self.redfruit_ai_tag = tk.StringVar(value="创意AI素材")
        self.redfruit_extra_tags = tk.StringVar()
        self.custom_template_name = tk.StringVar()
        self.custom_tags = tk.StringVar()
        self.tomato_input = tk.StringVar()
        self.tomato_output = tk.StringVar(value=str(default_output / "番茄音乐打标"))
        self.tomato_customer_id = tk.StringVar()
        self.tomato_material_url = tk.StringVar()
        self.tomato_account = tk.StringVar()
        self.tomato_password = tk.StringVar()
        self.tomato_concurrency = tk.StringVar(value="1")
        self.tomato_headless = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="就绪。预检不会打开浏览器；正式执行需要再次确认。")

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Action.TButton", padding=(12, 6))

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="仅调用当前技能脚本，汽水音乐、红果短剧、番茄音乐保持独立流程。账号密码不会保存。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(3, 10))

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        upload_tab = ttk.Frame(notebook, padding=10)
        tomato_tab = ttk.Frame(notebook, padding=10)
        notebook.add(upload_tab, text="上传：汽水 / 红果")
        notebook.add(tomato_tab, text="番茄音乐：CID 打标")
        self._build_upload_tab(upload_tab)
        self._build_tomato_tab(tomato_tab)

        log_frame = ttk.LabelFrame(outer, text="运行日志", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.log_text = ScrolledText(log_frame, height=12, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)
        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(8, 0))
        ttk.Label(bottom, textvariable=self.status, style="Hint.TLabel").pack(side="left", fill="x", expand=True)
        self.clear_button = ttk.Button(bottom, text="清空日志", command=self._clear_log)
        self.clear_button.pack(side="right")

    def _build_upload_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(10, weight=1)
        self._path_row(parent, 0, "视频文件夹", self.video_folder, directory=True)
        self._path_row(parent, 1, "批次 manifest JSON（可选）", self.manifest, directory=False, file_types=(("JSON", "*.json"), ("所有文件", "*.*")))
        self._path_row(parent, 2, "断点任务目录 / task.json", self.resume_task, directory=False, file_types=None)
        self._path_row(parent, 3, "输出目录", self.output_root, directory=True)
        self._entry_row(parent, 4, "上传工单 ID", self.order_id)
        self._entry_row(parent, 5, "账号", self.account)
        self._entry_row(parent, 6, "密码", self.password, show="*")
        self._entry_row(parent, 7, "任务名称", self.task_name)
        self._entry_row(parent, 8, "登录态 JSON（可选）", self.storage_state)
        self._entry_row(parent, 9, "登录态输出 JSON（可选）", self.storage_state_output)

        options = ttk.LabelFrame(parent, text="上传设置", padding=8)
        options.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(8, 5))
        for col in range(8):
            options.columnconfigure(col, weight=1 if col in (1, 5) else 0)
        ttk.Label(options, text="流程").grid(row=0, column=0, sticky="w")
        ttk.Combobox(options, textvariable=self.workflow, state="readonly", values=("soda_music", "redfruit_short_drama"), width=22).grid(row=0, column=1, sticky="ew", padx=(5, 14))
        ttk.Label(options, text="并发批次").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(options, from_=1, to=10, textvariable=self.upload_concurrency, width=7).grid(row=0, column=3, sticky="w", padx=(5, 14))
        ttk.Checkbutton(options, text="全部视频", variable=self.all_videos).grid(row=0, column=4, sticky="w")
        ttk.Checkbutton(options, text="按歌曲自动拆批", variable=self.split_by_song).grid(row=0, column=5, sticky="w")
        ttk.Checkbutton(options, text="递归扫描", variable=self.recursive).grid(row=0, column=6, sticky="w")
        ttk.Checkbutton(options, text="无头浏览器", variable=self.headless).grid(row=0, column=7, sticky="w")

        details = ttk.Frame(parent)
        details.grid(row=10, column=0, columnspan=3, sticky="nsew", pady=(2, 0))
        details.columnconfigure(1, weight=1)
        details.columnconfigure(3, weight=1)
        self._path_row(details, 0, "歌曲库 Excel（汽水）", self.song_excel, directory=False, file_types=(("Excel", "*.xlsx *.xlsm *.xls"), ("所有文件", "*.*")), label_column=0, value_column=1)
        self._path_row(details, 1, "回填 Excel（汽水）", self.backfill_excel, directory=False, file_types=(("Excel", "*.xlsx *.xlsm *.xls"), ("所有文件", "*.*")), label_column=0, value_column=1)
        self._entry_row(details, 0, "红果 BID 映射 JSON", self.redfruit_bid_map, label_column=2, value_column=3)
        self._entry_row(details, 1, "红果题材 / 版式", self.redfruit_genre, label_column=2, value_column=3)
        self._entry_row(details, 2, "红果素材模式", self.redfruit_material_mode, label_column=2, value_column=3)
        self._entry_row(details, 3, "红果版式覆盖", self.redfruit_layout, label_column=2, value_column=3)
        self._entry_row(details, 4, "红果 AI 自定义标签", self.redfruit_ai_tag, label_column=2, value_column=3)
        self._entry_row(details, 5, "红果额外自定义标签", self.redfruit_extra_tags, label_column=2, value_column=3)
        self._entry_row(details, 6, "汽水模板名称", self.custom_template_name, label_column=2, value_column=3)
        ttk.Label(details, text="汽水自定义标签（每行一个）").grid(row=7, column=0, sticky="nw", pady=(7, 0))
        self.custom_tags_box = tk.Text(details, height=3, width=28, wrap="word")
        self.custom_tags_box.grid(row=7, column=1, sticky="ew", padx=(5, 14), pady=(5, 0))
        ttk.Label(details, text="补录创意单元 ID（每行或中文逗号分隔）").grid(row=7, column=2, sticky="nw", pady=(7, 0))
        self.existing_ids_box = tk.Text(details, height=3, width=28, wrap="word")
        self.existing_ids_box.grid(row=7, column=3, sticky="ew", padx=(5, 0), pady=(5, 0))

        actions = ttk.Frame(parent)
        actions.grid(row=11, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.upload_dry_button = ttk.Button(actions, text="预检", style="Action.TButton", command=self._precheck_upload)
        self.upload_dry_button.pack(side="left")
        self.upload_live_button = ttk.Button(actions, text="开始正式执行", style="Action.TButton", command=self._start_upload)
        self.upload_live_button.pack(side="left", padx=8)
        ttk.Label(actions, text="红果会执行当前技能的前置校验、上传、ARLP 和分类标签状态机；断点任务请填上方任务路径。", style="Hint.TLabel").pack(side="left", padx=8)

    def _build_tomato_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)
        self._path_row(parent, 0, "CID/BID 输入 JSON 或 Excel", self.tomato_input, directory=False, file_types=(("JSON/Excel", "*.json *.xlsx *.xlsm *.xls"), ("所有文件", "*.*")))
        self._path_row(parent, 1, "输出目录", self.tomato_output, directory=True)
        self._entry_row(parent, 2, "客户 ID", self.tomato_customer_id)
        self._entry_row(parent, 3, "素材管理 URL（可选）", self.tomato_material_url)
        self._entry_row(parent, 4, "账号", self.tomato_account)
        self._entry_row(parent, 5, "密码", self.tomato_password, show="*")
        self._entry_row(parent, 6, "并发 BID 批次", self.tomato_concurrency)
        ttk.Label(parent, text="指定 BID（每行或中文逗号分隔，可留空处理输入中的全部 BID）").grid(row=7, column=0, sticky="nw", pady=(8, 0))
        self.tomato_bid_box = tk.Text(parent, height=5, wrap="word")
        self.tomato_bid_box.grid(row=7, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        ttk.Checkbutton(parent, text="无头浏览器", variable=self.tomato_headless).grid(row=8, column=1, sticky="w", pady=8)
        actions = ttk.Frame(parent)
        actions.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.tomato_dry_button = ttk.Button(actions, text="预检", style="Action.TButton", command=self._precheck_tomato)
        self.tomato_dry_button.pack(side="left")
        self.tomato_live_button = ttk.Button(actions, text="开始正式打标", style="Action.TButton", command=self._start_tomato)
        self.tomato_live_button.pack(side="left", padx=8)
        ttk.Label(actions, text="飞书在线查询/写回仍使用现有 CLI 参数和环境变量，桌面端不保存密钥。", style="Hint.TLabel").pack(side="left", padx=8)

    def _entry_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, show: str = "", label_column: int = 0, value_column: int = 1) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=label_column, sticky="w", pady=3)
        entry = ttk.Entry(parent, textvariable=variable, show=show)
        entry.grid(row=row, column=value_column, sticky="ew", padx=(8, 0), pady=3)

    def _path_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, directory: bool, file_types: tuple[tuple[str, str], ...] | None = (("所有文件", "*.*"),), label_column: int = 0, value_column: int = 1) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=label_column, sticky="w", pady=3)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=value_column, sticky="ew", padx=(8, 5), pady=3)
        ttk.Button(parent, text="浏览...", command=lambda: self._choose_path(variable, directory, file_types)).grid(row=row, column=value_column + 1, sticky="e", pady=3)

    def _choose_path(self, variable: tk.StringVar, directory: bool, file_types: tuple[tuple[str, str], ...] | None) -> None:
        selected = filedialog.askdirectory() if directory else filedialog.askopenfilename(filetypes=file_types or (("所有文件", "*.*"),))
        if selected:
            variable.set(selected)

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        for button in (self.upload_dry_button, self.upload_live_button, self.tomato_dry_button, self.tomato_live_button, self.clear_button):
            button.configure(state=state)

    def _run(self, runner_name: str, argv: list[str], description: str) -> None:
        if self.runner_thread and self.runner_thread.is_alive():
            messagebox.showinfo(APP_TITLE, "已有任务正在运行，请等待当前任务结束。")
            return
        self._append_log(f"\n===== {description} =====\n")
        self.status.set(f"正在执行：{description}")
        self._set_running(True)
        self.runner_thread = _RunnerThread(runner_name, argv, self.output_queue)
        self.runner_thread.start()

    def _upload_args(self, live: bool) -> list[str]:
        argv: list[str] = []
        resume = self.resume_task.get().strip()
        if resume:
            argv.extend(("--resume-task", resume))
        else:
            _add_value(argv, "--manifest", self.manifest.get())
            _add_value(argv, "--video-folder", self.video_folder.get())
            _add_value(argv, "--output-root", self.output_root.get())
            _add_value(argv, "--order-id", self.order_id.get())
            _add_value(argv, "--task-name", self.task_name.get())
            _add_value(argv, "--workflow", self.workflow.get())
            if self.all_videos.get():
                argv.append("--all-videos")
            if self.split_by_song.get():
                argv.append("--split-by-song")
            argv.append("--recursive" if self.recursive.get() else "--no-recursive")
            _add_value(argv, "--song-excel", self.song_excel.get())
            _add_value(argv, "--backfill-excel", self.backfill_excel.get())
            _add_value(argv, "--redfruit-bid-map", self.redfruit_bid_map.get())
            _add_value(argv, "--redfruit-default-genre", self.redfruit_genre.get())
            _add_value(argv, "--redfruit-layout-override", self.redfruit_layout.get())
            _add_value(argv, "--redfruit-material-mode-override", self.redfruit_material_mode.get())
            _add_value(argv, "--redfruit-ai-custom-tag", self.redfruit_ai_tag.get())
            for tag in _split_lines(self.redfruit_extra_tags.get()):
                argv.extend(("--redfruit-extra-custom-tag", tag))
            _add_value(argv, "--custom-tag-template-name", self.custom_template_name.get())
            for tag in _split_lines(self.custom_tags_box.get("1.0", "end")):
                argv.extend(("--custom-tag", tag))
            for unit_id in _split_lines(self.existing_ids_box.get("1.0", "end")):
                argv.extend(("--existing-creative-unit-id", unit_id))
        _add_value(argv, "--account", self.account.get())
        _add_value(argv, "--password", self.password.get())
        _add_value(argv, "--storage-state", self.storage_state.get())
        _add_value(argv, "--storage-state-output", self.storage_state_output.get())
        try:
            concurrency = max(1, min(10, int(self.upload_concurrency.get().strip() or "1")))
        except ValueError:
            raise ValueError("上传并发批次必须是 1 到 10 的整数") from None
        argv.extend(("--concurrency", str(concurrency)))
        if self.headless.get():
            argv.append("--headless")
        if live:
            argv.extend(("--live", "--confirm-live"))
        return argv

    def _tomato_args(self, live: bool) -> list[str]:
        argv: list[str] = []
        _add_value(argv, "--input", self.tomato_input.get())
        _add_value(argv, "--output-root", self.tomato_output.get())
        _add_value(argv, "--customer-id", self.tomato_customer_id.get())
        _add_value(argv, "--material-url", self.tomato_material_url.get())
        _add_value(argv, "--account", self.tomato_account.get())
        _add_value(argv, "--password", self.tomato_password.get())
        for bid in _split_lines(self.tomato_bid_box.get("1.0", "end")):
            argv.extend(("--bid", bid))
        try:
            concurrency = max(1, min(10, int(self.tomato_concurrency.get().strip() or "1")))
        except ValueError:
            raise ValueError("番茄并发 BID 批次必须是 1 到 10 的整数") from None
        argv.extend(("--concurrency", str(concurrency)))
        if self.tomato_headless.get():
            argv.append("--headless")
        if live:
            argv.extend(("--live", "--confirm-live"))
        return argv

    def _precheck_upload(self) -> None:
        try:
            argv = self._upload_args(live=False)
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self._run("usergrowth_upload", argv, "UserGrowth 上传预检")

    def _start_upload(self) -> None:
        try:
            argv = self._upload_args(live=True)
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        target = self.workflow.get()
        order = self.order_id.get().strip() or "断点任务"
        if not messagebox.askyesno(APP_TITLE, f"即将正式执行 {target}，目标：{order}。\n\n会打开浏览器并可能写入平台数据，确认开始吗？"):
            return
        self._run("usergrowth_upload", argv, "UserGrowth 正式执行")

    def _precheck_tomato(self) -> None:
        try:
            argv = self._tomato_args(live=False)
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self._run("tomato_music_tagging", argv, "番茄音乐打标预检")

    def _start_tomato(self) -> None:
        try:
            argv = self._tomato_args(live=True)
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        if not messagebox.askyesno(APP_TITLE, "即将正式给 UserGrowth 素材追加 bid_ 标签。\n\n确认开始吗？"):
            return
        self._run("tomato_music_tagging", argv, "番茄音乐正式打标")

    def _drain_output(self) -> None:
        try:
            while True:
                kind, value = self.output_queue.get_nowait()
                if kind == "line":
                    self._append_log(value)
                elif kind == "done":
                    code = int(value)
                    self._set_running(False)
                    self.status.set("执行完成。" if code == 0 else f"执行结束，返回码 {code}；请查看日志和任务目录。")
                    self._append_log(f"\n===== 执行结束，返回码 {code} =====\n")
        except queue.Empty:
            pass
        self.after(100, self._drain_output)


def smoke_test() -> int:
    _prepare_imports()
    importlib.import_module("usergrowth_upload")
    importlib.import_module("tomato_music_tagging")
    print("UserGrowth desktop smoke test passed")
    return 0


def main() -> int:
    if "--smoke-test" in sys.argv[1:]:
        return smoke_test()
    _prepare_imports()
    app = UserGrowthDesktop()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
