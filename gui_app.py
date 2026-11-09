# -*- coding: utf-8 -*-
"""ModelScope 模型下载器 —— tkinter 图形界面与程序入口。

界面遵循「只填两项就能下载」的原则：顶部只有「模型库名称」与「保存到
文件夹」两个必填项，其余配置收在「高级设置」中并带有合理默认值。

线程模型：
    * 文件清单查询与下载都在 daemon 后台线程中执行，主线程只负责 UI；
    * 后台线程**绝不直接触碰 Tcl**：界面任务只放入线程安全
      ``queue.Queue``，由主线程的 ``root.after`` 定时泵统一排空执行。
      tkinter 非线程安全，从后台线程直接调用 ``root.after(0, ...)`` 会抛
      ``RuntimeError: main thread is not in main loop``，导致日志 / 进度 /
      文件清单被静默吞掉；
    * 通过 ``threading.Event`` 实现停止 / 取消，并防止重复启动多个任务。
"""
from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Callable, List, Optional

# 允许从任意工作目录启动（双击 bat 时 bat 已 cd，这里再做一层保险）。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import ms_downloader_core as core  # noqa: E402  (需在 sys.path 设置之后导入)

APP_TITLE = "ModelScope 模型下载器"
WINDOW_SIZE = (1020, 860)
PREVIEW_TIMEOUT = 20.0
CHECKED = "☑"
UNCHECKED = "☐"

_HINT_MODEL = "必填。格式为 owner/name，例如：Qwen/Qwen2.5-7B-Instruct；也可直接粘贴模型主页链接。"
_HINT_DIR = "必填。模型文件会直接保存在这个文件夹里（不经过缓存软链）；文件夹不存在会自动创建。"
_HINT_REVISION = "版本 / 分支，默认 master。需要特定量化或历史提交时填写分支名、标签或 commit id。"
_HINT_ENDPOINT = "镜像 / API 站点，留空使用官方 https://www.modelscope.cn 。企业内网可填自建镜像地址。"
_HINT_PROXY = "HTTP(S) 代理，例如 http://127.0.0.1:7890 ；留空则沿用系统代理设置。"
_HINT_WORKERS = "并发下载线程数，默认 4。网络较好可适度调大；磁盘慢或容易断流建议调小。"
_HINT_SUBDIR = "便捷输入：只下载某个子目录，例如 onnx 或 onnx/q4 ，等价于自动追加 onnx/* 规则。"
_HINT_ALLOW = "包含规则，多个用逗号分隔，例如 *.safetensors, *.json 或 onnx/* 。留空表示下载全部文件。"
_HINT_IGNORE = "排除规则，多个用逗号分隔，例如 *.md, *.txt, *.png 。与包含规则同时生效时排除优先。"
_HINT_SELECTED = "勾选后，仅下载下方文件清单中被勾选的文件（需先点【查看文件清单】）。"
_HINT_TREE = "单击「选择」列或按空格键可勾选 / 取消；勾选配合「仅下载勾选文件」使用。"
_HINT_DEFAULT = "将鼠标移到输入框上可在此查看该字段的说明。"


class DownloaderApp:
    """主窗口控制器，负责界面构建、后台任务调度与按钮状态机。"""

    def __init__(self, root: tk.Tk) -> None:
        """初始化界面。

        Args:
            root: tkinter 根窗口。
        """
        self.root = root
        self.config: core.AppConfig = core.load_config()
        self._cancel_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._busy = False
        self._model_info: Optional[core.ModelInfo] = None
        self._start_time = 0.0
        self._timer_job: Optional[str] = None
        self._editable: List[Any] = []
        self._hint_job: Optional[str] = None
        # 后台线程绝不直接调用 Tcl（tkinter 非线程安全，跨线程 root.after 会抛
        # "main thread is not in main loop"）。所有界面更新先入线程安全队列，
        # 由主线程的定时泵统一执行。
        self._ui_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._ui_pump_job: Optional[str] = None

        self._init_variables()
        self._build_style()
        self._build_widgets()
        self._fill_from_config()
        self._start_ui_pump()
        self._refresh_sdk_status()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._log("程序已启动。填写「模型库名称」与「保存到文件夹」即可开始下载。")
        self._log("提示：下载前可先点【查看文件清单】确认文件与总大小。")
        # 仅当 SDK 已安装时，才需要后台做真实导入与 local_dir 能力探测。
        # 未安装时 _refresh_sdk_status 已同步给出状态和安装指引，再启动探测
        # 线程不仅无意义，还会与用户立刻触发的 REST 清单查询争用解释器导入
        # 锁（本机实测把首次预览从约 0.4 秒拖慢到 10 秒以上）。
        if core.sdk_is_installed():
            threading.Thread(
                target=self._probe_sdk_async, name="ms-sdk-probe", daemon=True
            ).start()

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------

    def _init_variables(self) -> None:
        """创建所有 tkinter 变量。"""
        self.var_model = tk.StringVar()
        self.var_dir = tk.StringVar()
        self.var_revision = tk.StringVar()
        self.var_endpoint = tk.StringVar()
        self.var_proxy = tk.StringVar()
        self.var_workers = tk.StringVar()
        self.var_subdir = tk.StringVar()
        self.var_allow = tk.StringVar()
        self.var_ignore = tk.StringVar()
        self.var_only_selected = tk.BooleanVar(value=False)
        self.var_hint = tk.StringVar(value=_HINT_DEFAULT)
        self.var_status = tk.StringVar(value="就绪")
        self.var_elapsed = tk.StringVar(value="已用时 00:00")
        self.var_progress = tk.StringVar(value="尚未开始")
        self.var_summary = tk.StringVar(value="尚未获取文件清单")
        self.var_sdk = tk.StringVar(value="正在检测 modelscope SDK ...")

    def _build_style(self) -> None:
        """配置 ttk 样式（字体在缺少微软雅黑时由 Tk 自动回退）。"""
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Sub.TLabel", font=("Microsoft YaHei UI", 9), foreground="#666666")
        style.configure("Hint.TLabel", font=("Microsoft YaHei UI", 9), foreground="#555555")
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=6)
        style.configure("Danger.TButton", font=("Microsoft YaHei UI", 10), padding=5)
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 9), foreground="#333333")
        style.configure("Summary.TLabel", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Treeview", font=("Microsoft YaHei UI", 9), rowheight=22)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

    def _build_widgets(self) -> None:
        """构建主窗口全部控件。"""
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1, uniform="half")
        outer.columnconfigure(1, weight=1, uniform="half")
        outer.rowconfigure(0, weight=0)
        outer.rowconfigure(1, weight=0)
        outer.rowconfigure(2, weight=1)
        outer.rowconfigure(3, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").pack(side=tk.LEFT)
        ttk.Label(
            header,
            text="只填模型库名称 + 保存文件夹，即可下载 ModelScope 大模型",
            style="Sub.TLabel",
        ).pack(side=tk.LEFT, padx=(12, 0))
        self.sdk_label = ttk.Label(header, textvariable=self.var_sdk, style="Status.TLabel")
        self.sdk_label.pack(side=tk.RIGHT)

        basic = ttk.LabelFrame(outer, text=" 下载设置 ", padding=10)
        basic.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        self._build_basic_frame(basic)

        advanced = ttk.LabelFrame(outer, text=" 高级设置 ", padding=10)
        advanced.grid(row=1, column=1, sticky="nsew", padx=(6, 0))
        self._build_advanced_frame(advanced)

        listing = ttk.LabelFrame(outer, text=" 模型文件清单（预览） ", padding=8)
        listing.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(10, 6))
        self._build_listing_frame(listing)

        progress_frame = ttk.LabelFrame(outer, text=" 下载进度与日志 ", padding=8)
        progress_frame.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
        self._build_progress_frame(progress_frame)

    def _build_basic_frame(self, parent: ttk.LabelFrame) -> None:
        """构建「下载设置」区：两个必填项与主操作按钮。"""
        parent.columnconfigure(1, weight=1)

        ttk.Label(parent, text="模型库名称 *").grid(row=0, column=0, sticky="w", pady=3)
        entry_model = ttk.Entry(parent, textvariable=self.var_model)
        entry_model.grid(row=0, column=1, columnspan=2, sticky="ew", pady=3)
        self._bind_hint(entry_model, _HINT_MODEL)
        self._editable.append(entry_model)

        ttk.Label(
            parent, text="示例：Qwen/Qwen2.5-7B-Instruct", style="Sub.TLabel"
        ).grid(row=1, column=1, columnspan=2, sticky="w")

        ttk.Label(parent, text="保存到文件夹 *").grid(row=2, column=0, sticky="w", pady=3)
        entry_dir = ttk.Entry(parent, textvariable=self.var_dir)
        entry_dir.grid(row=2, column=1, sticky="ew", pady=3)
        self._bind_hint(entry_dir, _HINT_DIR)
        self._editable.append(entry_dir)
        browse = ttk.Button(parent, text="浏览...", command=self._browse_dir, width=9)
        browse.grid(row=2, column=2, sticky="ew", padx=(6, 0))
        self._editable.append(browse)

        ttk.Label(
            parent, text="路径支持空格与中文；目录不存在会自动创建", style="Sub.TLabel"
        ).grid(row=3, column=1, columnspan=2, sticky="w")

        buttons = ttk.Frame(parent)
        buttons.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(14, 4))
        self.btn_preview = ttk.Button(
            buttons, text="查看文件清单", command=self._start_preview, width=16
        )
        self.btn_preview.pack(side=tk.LEFT)
        self.btn_download = ttk.Button(
            buttons, text="开始下载", command=self._start_download,
            style="Primary.TButton", width=16,
        )
        self.btn_download.pack(side=tk.LEFT, padx=8)
        self.btn_stop = ttk.Button(
            buttons, text="停止 / 取消", command=self._on_stop,
            style="Danger.TButton", width=14, state="disabled",
        )
        self.btn_stop.pack(side=tk.LEFT)

        ttk.Label(
            parent,
            text="下载中断后再次点击【开始下载】即可续传，已完成文件自动跳过。",
            style="Sub.TLabel", wraplength=430,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _build_advanced_frame(self, parent: ttk.LabelFrame) -> None:
        """构建「高级设置」区：所有可选项都带合理默认值。"""
        parent.columnconfigure(1, weight=1)

        rows = (
            ("版本 / 分支", self.var_revision, _HINT_REVISION, None),
            ("镜像 / API 站点", self.var_endpoint, _HINT_ENDPOINT, None),
            ("HTTP(S) 代理", self.var_proxy, _HINT_PROXY, None),
            ("仅下载子目录", self.var_subdir, _HINT_SUBDIR, None),
            ("包含规则", self.var_allow, _HINT_ALLOW, None),
            ("排除规则", self.var_ignore, _HINT_IGNORE, None),
        )
        row_index = 0
        for label, variable, hint, _unused in rows:
            ttk.Label(parent, text=label).grid(row=row_index, column=0, sticky="w", pady=2)
            entry = ttk.Entry(parent, textvariable=variable)
            entry.grid(row=row_index, column=1, columnspan=2, sticky="ew", pady=2)
            self._bind_hint(entry, hint)
            self._editable.append(entry)
            row_index += 1

        ttk.Label(parent, text="并发线程数").grid(row=row_index, column=0, sticky="w", pady=2)
        spin = ttk.Spinbox(
            parent, from_=1, to=32, textvariable=self.var_workers, width=8
        )
        spin.grid(row=row_index, column=1, sticky="w", pady=2)
        self._bind_hint(spin, _HINT_WORKERS)
        self._editable.append(spin)
        ttk.Label(parent, text="默认 4", style="Sub.TLabel").grid(
            row=row_index, column=2, sticky="w", padx=(6, 0)
        )
        row_index += 1

        check = ttk.Checkbutton(
            parent, text="仅下载文件清单中勾选的文件",
            variable=self.var_only_selected,
        )
        check.grid(row=row_index, column=0, columnspan=3, sticky="w", pady=(6, 2))
        self._bind_hint(check, _HINT_SELECTED)
        self._editable.append(check)
        row_index += 1

        hint_label = ttk.Label(
            parent, textvariable=self.var_hint, style="Hint.TLabel",
            wraplength=440, justify=tk.LEFT,
        )
        hint_label.grid(row=row_index, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _build_listing_frame(self, parent: ttk.LabelFrame) -> None:
        """构建文件清单 Treeview 与汇总工具栏。"""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        columns = ("sel", "name", "size", "kind")
        self.tree = ttk.Treeview(
            parent, columns=columns, show="headings", height=9, selectmode="extended"
        )
        self.tree.heading("sel", text="选择")
        self.tree.heading("name", text="文件路径")
        self.tree.heading("size", text="大小")
        self.tree.heading("kind", text="类型")
        self.tree.column("sel", width=60, anchor="center", stretch=False)
        self.tree.column("name", width=560, anchor="w")
        self.tree.column("size", width=130, anchor="e", stretch=False)
        self.tree.column("kind", width=90, anchor="center", stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<space>", self._on_tree_toggle)
        self._bind_hint(self.tree, _HINT_TREE)

        toolbar = ttk.Frame(parent)
        toolbar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(toolbar, text="全选", command=lambda: self._select_all(True), width=8).pack(
            side=tk.LEFT
        )
        ttk.Button(toolbar, text="全不选", command=lambda: self._select_all(False), width=8).pack(
            side=tk.LEFT, padx=6
        )
        ttk.Button(toolbar, text="反选", command=self._invert_selection, width=8).pack(
            side=tk.LEFT
        )
        ttk.Label(toolbar, textvariable=self.var_summary, style="Summary.TLabel").pack(
            side=tk.RIGHT
        )

    def _build_progress_frame(self, parent: ttk.LabelFrame) -> None:
        """构建进度条、状态栏与带时间戳的滚动日志区。"""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        top = ttk.Frame(parent)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        self.progress_bar = ttk.Progressbar(top, mode="determinate", maximum=100, value=0)
        self.progress_bar.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Label(top, textvariable=self.var_progress, width=40, anchor="w").grid(
            row=0, column=1, sticky="w"
        )

        status = ttk.Frame(parent)
        status.grid(row=1, column=0, sticky="ew", pady=(4, 4))
        ttk.Label(status, textvariable=self.var_status, style="Status.TLabel").pack(side=tk.LEFT)
        ttk.Label(status, textvariable=self.var_elapsed, style="Sub.TLabel").pack(
            side=tk.LEFT, padx=(16, 0)
        )
        ttk.Button(status, text="清空日志", command=self._clear_log, width=10).pack(
            side=tk.RIGHT
        )

        self.log_text = scrolledtext.ScrolledText(
            parent, wrap=tk.WORD, height=9, font=("Microsoft YaHei UI", 9),
            state="disabled", undo=False,
        )
        self.log_text.grid(row=2, column=0, sticky="nsew")

    # ------------------------------------------------------------------
    # 小工具：提示、线程安全投递、日志、进度
    # ------------------------------------------------------------------

    def _bind_hint(self, widget: Any, text: str) -> None:
        """给控件绑定焦点提示，说明文字显示在高级设置区底部。"""
        widget.bind("<FocusIn>", lambda _event, value=text: self.var_hint.set(value), add="+")
        widget.bind("<Enter>", lambda _event, value=text: self.var_hint.set(value), add="+")

    def _ui(self, func: Callable[[], None]) -> None:
        """线程安全地把界面更新投递回 tkinter 主线程。

        后台线程只做 ``Queue.put``（纯 Python 操作，不触碰 Tcl 解释器）；
        主线程由 :meth:`_start_ui_pump` 建立的 ``root.after`` 定时泵排空队列
        并逐个执行，保证所有控件操作都发生在主线程，且不会因跨线程
        ``root.after`` 抛 ``RuntimeError`` 而静默丢失日志 / 进度 / 清单。

        Args:
            func: 无参、只操作界面控件的可调用对象。
        """
        self._ui_queue.put(func)
        if threading.current_thread() is threading.main_thread():
            # 主线程调用时立即排空一次，保持按钮点击等交互的即时响应。
            try:
                self.root.after(0, self._drain_ui_queue)
            except (tk.TclError, RuntimeError):
                pass

    def _drain_ui_queue(self) -> None:
        """在主线程中排空界面任务队列；单个任务失败不影响后续任务。"""
        while True:
            try:
                func = self._ui_queue.get_nowait()
            except queue.Empty:
                return
            try:
                func()
            except Exception:
                # 窗口已关闭、控件已销毁等情况：不把 traceback 抛给用户。
                continue

    def _start_ui_pump(self) -> None:
        """启动主线程的定时泵（每 40ms 排空一次界面任务队列）。"""

        def _tick() -> None:
            self._drain_ui_queue()
            try:
                self._ui_pump_job = self.root.after(40, _tick)
            except (tk.TclError, RuntimeError):
                self._ui_pump_job = None

        _tick()

    def _log(self, message: str) -> None:
        """向日志区追加一条带时间戳的消息（可从任意线程调用）。"""
        line = "[{0}] {1}".format(time.strftime("%H:%M:%S"), message)

        def _append() -> None:
            try:
                self.log_text.configure(state="normal")
                self.log_text.insert("end", line + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
            except tk.TclError:
                pass

        self._ui(_append)

    def _clear_log(self) -> None:
        """清空日志区。"""
        try:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass

    def _progress_mode(self) -> str:
        """返回进度条当前模式。

        Tk 8.6 下 ``ttk.Progressbar.cget("mode")`` 返回 ``_tkinter.Tcl_Obj``
        而非 ``str``，直接与字符串比较会恒为 ``False``（导致切换模式失效、
        任务结束后滚动动画停不下来），因此统一显式转成字符串。
        """
        try:
            return str(self.progress_bar.cget("mode"))
        except tk.TclError:
            return "determinate"

    def _on_progress(self, done: int, total: int, message: str = "") -> None:
        """进度回调：由 core 的后台线程调用，更新在主线程执行。"""

        def _apply() -> None:
            try:
                if message:
                    self.var_status.set(message)
                if total and total > 0:
                    if self._progress_mode() != "determinate":
                        self.progress_bar.stop()
                        self.progress_bar.configure(mode="determinate", maximum=100)
                    percent = max(0.0, min(100.0, done * 100.0 / float(total)))
                    self.progress_bar.configure(value=percent)
                    self.var_progress.set("{0:.1f}%  ·  {1} / {2}".format(
                        percent, core.format_size(done), core.format_size(total)))
                else:
                    if self._progress_mode() != "indeterminate":
                        self.progress_bar.configure(mode="indeterminate", maximum=100)
                        self.progress_bar.start(80)
                    self.var_progress.set("下载中  ·  已落盘 {0}  ·  总大小未知".format(
                        core.format_size(done)))
            except tk.TclError:
                pass

        self._ui(_apply)

    def _reset_progress(self, total: int) -> None:
        """开始新任务时重置进度条；总大小未知则切 indeterminate 滚动模式。"""
        try:
            self.progress_bar.stop()
            if total and total > 0:
                self.progress_bar.configure(mode="determinate", maximum=100, value=0)
                self.var_progress.set("0.0%  ·  0 B / {0}".format(core.format_size(total)))
            else:
                self.progress_bar.configure(mode="indeterminate", maximum=100)
                self.progress_bar.start(80)
                self.var_progress.set("下载中  ·  总大小未知（可先点【查看文件清单】）")
            self.var_status.set("任务进行中 ...")
        except tk.TclError:
            pass

    def _stop_progress_animation(self) -> None:
        """任务结束后停止滚动动画，避免界面看起来仍在下载。"""
        try:
            if self._progress_mode() == "indeterminate":
                self.progress_bar.stop()
                self.progress_bar.configure(mode="determinate", value=100)
                self.var_progress.set("任务结束")
        except tk.TclError:
            pass

    def _start_elapsed_timer(self) -> None:
        """启动「已用时」计时器，确保无进度回调时界面仍有活动反馈。"""
        self._stop_elapsed_timer()

        def _tick() -> None:
            if not self._busy:
                return
            elapsed = max(0, int(time.time() - self._start_time))
            self.var_elapsed.set("已用时 {0:02d}:{1:02d}".format(
                elapsed // 60, elapsed % 60))
            try:
                self._timer_job = self.root.after(1000, _tick)
            except tk.TclError:
                self._timer_job = None

        _tick()

    def _stop_elapsed_timer(self) -> None:
        """停止「已用时」计时器。"""
        if self._timer_job:
            try:
                self.root.after_cancel(self._timer_job)
            except tk.TclError:
                pass
            self._timer_job = None

    def _set_busy(self, busy: bool) -> None:
        """按钮状态机：防止重复启动下载线程。"""
        self._busy = busy
        normal = "disabled" if busy else "normal"
        self.btn_preview.configure(state=normal)
        self.btn_download.configure(state=normal)
        self.btn_stop.configure(state="normal" if busy else "disabled")
        for widget in self._editable:
            try:
                widget.configure(state=normal)
            except tk.TclError:
                pass
        if busy:
            self._cancel_event.clear()
            self._start_time = time.time()
            self._start_elapsed_timer()
        else:
            self._stop_elapsed_timer()

    # ------------------------------------------------------------------
    # 配置与输入校验
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        """把界面输入安全地转为正整数。"""
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            return default
        return number if number > 0 else default

    def _fill_from_config(self) -> None:
        """启动时把上次配置回填到界面。"""
        config = self.config
        self.var_model.set(config.model_id)
        default_dir = str(Path.home() / "ModelScopeDownloads")
        self.var_dir.set(config.target_dir or default_dir)
        self.var_revision.set(config.revision or core.DEFAULT_REVISION)
        self.var_endpoint.set(config.endpoint)
        self.var_proxy.set(config.proxy)
        self.var_workers.set(str(config.max_workers or core.DEFAULT_MAX_WORKERS))
        self.var_subdir.set(config.subdir)
        self.var_allow.set(config.allow_patterns)
        self.var_ignore.set(config.ignore_patterns)
        self.var_only_selected.set(bool(config.only_selected))

    def _collect_config(self) -> core.AppConfig:
        """从界面收集配置并持久化。"""
        config = core.AppConfig(
            model_id=self.var_model.get().strip(),
            target_dir=self.var_dir.get().strip(),
            revision=self.var_revision.get().strip() or core.DEFAULT_REVISION,
            endpoint=self.var_endpoint.get().strip(),
            proxy=self.var_proxy.get().strip(),
            max_workers=self._safe_int(self.var_workers.get(), core.DEFAULT_MAX_WORKERS),
            subdir=self.var_subdir.get().strip(),
            allow_patterns=self.var_allow.get().strip(),
            ignore_patterns=self.var_ignore.get().strip(),
            only_selected=bool(self.var_only_selected.get()),
        )
        core.save_config(config)
        self.config = config
        return config

    def _prepare(self) -> Optional[core.AppConfig]:
        """校验输入，返回规范化后的配置；校验失败时弹窗并返回 ``None``。"""
        if self._busy or (self._worker is not None and self._worker.is_alive()):
            messagebox.showinfo("提示", "已有任务正在进行，请等待完成或点击【停止 / 取消】。")
            return None
        config = self._collect_config()
        try:
            config.model_id = core.normalize_model_id(config.model_id)
        except core.ValidationError as exc:
            messagebox.showerror("输入有误", str(exc))
            return None
        if not config.target_dir:
            messagebox.showerror("输入有误", "请选择保存文件夹。")
            return None
        target = Path(config.target_dir.strip().strip('"')).expanduser()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("路径错误", "无法创建文件夹：\n{0}\n{1}".format(target, exc))
            return None
        config.target_dir = str(target)
        return config

    def _browse_dir(self) -> None:
        """打开文件夹选择对话框。"""
        initial = self.var_dir.get().strip() or str(Path.home())
        chosen = filedialog.askdirectory(
            title="选择模型保存文件夹", initialdir=initial, mustexist=False
        )
        if chosen:
            self.var_dir.set(str(Path(chosen)))

    # ------------------------------------------------------------------
    # 文件清单
    # ------------------------------------------------------------------

    def _selected_paths(self) -> List[str]:
        """返回清单中已勾选的文件路径。"""
        return [
            iid for iid in self.tree.get_children()
            if self.tree.item(iid, "values")[0] == CHECKED
        ]

    def _toggle_row(self, iid: str) -> None:
        """切换单行的勾选状态。"""
        values = list(self.tree.item(iid, "values"))
        values[0] = UNCHECKED if values[0] == CHECKED else CHECKED
        self.tree.item(iid, values=values)

    def _on_tree_click(self, event: tk.Event) -> Optional[str]:
        """点击「选择」列时切换勾选。"""
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        if self.tree.identify_column(event.x) != "#1":
            return None
        iid = self.tree.identify_row(event.y)
        if iid:
            self._toggle_row(iid)
            return "break"
        return None

    def _on_tree_toggle(self, _event: tk.Event) -> str:
        """空格键切换当前选中行的勾选状态。"""
        for iid in self.tree.selection():
            self._toggle_row(iid)
        return "break"

    def _select_all(self, checked: bool) -> None:
        """全选 / 全不选。"""
        mark = CHECKED if checked else UNCHECKED
        for iid in self.tree.get_children():
            values = list(self.tree.item(iid, "values"))
            values[0] = mark
            self.tree.item(iid, values=values)

    def _invert_selection(self) -> None:
        """反选所有行。"""
        for iid in self.tree.get_children():
            self._toggle_row(iid)

    def _show_model_info(self, info: core.ModelInfo) -> None:
        """在主线程中把文件清单填入 Treeview。"""
        self._model_info = info
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for item in info.files:
            self.tree.insert(
                "", "end", iid=item.path,
                values=(
                    CHECKED,
                    item.path,
                    core.format_size(item.size),
                    "LFS 大文件" if item.is_lfs else "普通文件",
                ),
            )
        source = "ModelScope SDK" if info.source == "SDK" else "公开 REST 接口"
        self.var_summary.set("共 {0} 个文件   ·   合计 {1}   ·   来源：{2}".format(
            info.file_count, core.format_size(info.total_size), source))

    def _start_preview(self) -> None:
        """启动后台线程查询文件清单。"""
        config = self._prepare()
        if config is None:
            return
        self._set_busy(True)
        self.var_status.set("正在查询文件清单 ...")
        self._log("=" * 56)
        self._log("开始查询文件清单：{0} @ {1}".format(config.model_id, config.revision))
        self._worker = threading.Thread(
            target=self._preview_worker, args=(config,),
            name="ms-preview", daemon=True,
        )
        self._worker.start()

    def _preview_worker(self, config: core.AppConfig) -> None:
        """文件清单查询线程体。"""
        try:
            core.apply_environment(config.endpoint, config.proxy)
            info = core.fetch_repo_files(
                model_id=config.model_id,
                revision=config.revision,
                endpoint=config.endpoint,
                proxy=config.proxy,
                timeout=PREVIEW_TIMEOUT,
                prefer_sdk=True,
                log_cb=self._log,
                cancel_event=self._cancel_event,
            )
            self._ui(lambda: self._show_model_info(info))
            self._log("文件清单已更新，确认无误后点击【开始下载】。")
        except core.DownloadCancelled:
            self._log("已停止查询文件清单。")
        except core.DownloaderError as exc:
            self._log("查询失败：{0}".format(exc))
            self._ui(lambda: messagebox.showerror("查询失败", str(exc)))
        except Exception as exc:  # 兜底，避免后台线程静默退出
            self._log("查询失败（未预期错误）：{0}: {1}".format(type(exc).__name__, exc))
            self._ui(lambda: messagebox.showerror(
                "查询失败", "未预期错误：{0}".format(exc)))
        finally:
            self._ui(lambda: self._set_busy(False))
            self._ui(lambda: self.var_status.set("就绪"))

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------

    def _start_download(self) -> None:
        """启动后台线程下载模型。"""
        if not core.sdk_is_installed():
            self._show_install_guide()
            return
        config = self._prepare()
        if config is None:
            return
        allow, ignore = core.resolve_patterns(
            config.allow_patterns, config.ignore_patterns, config.subdir
        )
        if config.only_selected:
            paths = self._selected_paths()
            if not paths:
                messagebox.showinfo(
                    "提示",
                    "请先在文件清单中勾选要下载的文件，或关闭【仅下载文件清单中勾选的文件】。",
                )
                return
            for path in paths:
                if path not in allow:
                    allow.append(path)
            self._log("仅下载勾选的 {0} 个文件".format(len(paths)))

        total = 0
        if self._model_info is not None and self._model_info.model_id == config.model_id:
            total = self._model_info.total_size

        self._set_busy(True)
        self._reset_progress(total)
        self._log("=" * 56)
        self._log("开始下载：{0} @ {1} -> {2}".format(
            config.model_id, config.revision, config.target_dir))
        self._worker = threading.Thread(
            target=self._download_worker,
            args=(config, allow, ignore, total),
            name="ms-download", daemon=True,
        )
        self._worker.start()

    def _download_worker(
        self,
        config: core.AppConfig,
        allow: List[str],
        ignore: List[str],
        total: int,
    ) -> None:
        """下载线程体。"""
        try:
            result = core.download_model(
                model_id=config.model_id,
                target_dir=config.target_dir,
                revision=config.revision,
                allow_patterns=allow,
                ignore_patterns=ignore,
                max_workers=config.max_workers,
                endpoint=config.endpoint,
                proxy=config.proxy,
                total_bytes=total,
                cancel_event=self._cancel_event,
                log_cb=self._log,
                progress_cb=self._on_progress,
            )
            self._log("下载任务已结束。")
            self._ui(lambda: messagebox.showinfo(
                "下载完成", "模型已保存到：\n{0}".format(result)))
            self._ui(lambda: self.var_status.set("下载完成"))
        except core.DownloadCancelled:
            self._log("已停止；已完成文件保留，下次点击【开始下载】自动续传。")
            self._ui(lambda: self.var_status.set("已停止（可续传）"))
        except core.DependencyError as exc:
            self._log("依赖缺失：{0}".format(exc))
            self._ui(self._show_install_guide)
            self._ui(lambda: self.var_status.set("依赖缺失"))
        except core.DownloaderError as exc:
            self._log("任务失败：{0}".format(exc))
            self._ui(lambda: messagebox.showerror("下载失败", str(exc)))
            self._ui(lambda: self.var_status.set("任务失败"))
        except Exception as exc:
            self._log("下载失败（未预期错误）：{0}: {1}".format(type(exc).__name__, exc))
            self._ui(lambda: messagebox.showerror(
                "下载失败", "未预期错误：{0}".format(exc)))
            self._ui(lambda: self.var_status.set("任务失败"))
        finally:
            self._ui(lambda: self._set_busy(False))
            self._ui(self._stop_progress_animation)

    def _on_stop(self) -> None:
        """请求停止当前后台任务。

        取消事件会被 core 的 SDK 进度回调在每个数据块检查并抛出
        ``DownloadCancelled``，从而真实中断 SDK 下载循环；任务结束后
        ``finally`` 复位按钮状态机，界面给出「已停止（可续传）」的明确反馈。
        停止按钮在请求后置为 disabled 防止重复点击，任务结束时由
        ``_set_busy(False)`` 统一恢复（空闲态本就不可点）。
        """
        if not self._busy:
            return
        self._cancel_event.set()
        self._log("已请求停止：取消信号已送达 SDK 进度回调，将在下一个数据块中断下载。")
        self._log("注意：SDK 正在传输的单个大文件若无数据块到达，需等待网络超时；"
                  "已下载内容保留，下次点击【开始下载】可续传。")
        self.var_status.set("正在停止 ...")
        try:
            self.btn_stop.configure(state="disabled")
        except tk.TclError:
            pass
        # 主线程调用时立即排空队列：点击后日志 / 状态即时可见，无需等 40ms
        # 定时泵，避免用户误以为「点了没反应」（Bug B 的可感知性修复）。
        if threading.current_thread() is threading.main_thread():
            self._drain_ui_queue()

    # ------------------------------------------------------------------
    # 依赖检测与退出
    # ------------------------------------------------------------------

    def _refresh_sdk_status(self) -> None:
        """快速显示 SDK 安装状态（不真正导入，避免阻塞启动）。"""
        if core.sdk_is_installed():
            self.var_sdk.set("正在检测 modelscope SDK ...")
            try:
                self.sdk_label.configure(foreground="#666666")
            except tk.TclError:
                pass
        else:
            self.var_sdk.set("modelscope 未安装｜请执行 {0}（清单预览仍可用）".format(
                core.INSTALL_COMMAND))
            try:
                self.sdk_label.configure(foreground="#b42318")
            except tk.TclError:
                pass
            # 同步写入日志，保证用户启动后第一眼就能看到安装指引，
            # 不必等待后台 SDK 探测线程结束。
            self._log("提示：未检测到 modelscope SDK，下载功能暂不可用。")
            self._log("       请执行 {0}，或直接双击【安装依赖.bat】。".format(
                core.INSTALL_COMMAND))
            self._log("       文件清单预览不依赖 SDK，现在仍可正常使用。")

    def _probe_sdk_async(self) -> None:
        """后台完成 SDK 真实导入与 ``local_dir`` 能力探测。"""
        available = core.is_sdk_available()
        version = core.sdk_version()
        supports = core.probe_local_dir_support()

        def _apply() -> None:
            if available:
                strategy = "local_dir 直写" if supports else "cache_dir 回退复制"
                self.var_sdk.set("modelscope 已就绪（版本 {0}）｜{1}".format(
                    version or "未知", strategy))
                try:
                    self.sdk_label.configure(foreground="#1a7f37")
                except tk.TclError:
                    pass
            else:
                self.var_sdk.set("modelscope 不可用｜请执行 {0}".format(
                    core.INSTALL_COMMAND))
                try:
                    self.sdk_label.configure(foreground="#b42318")
                except tk.TclError:
                    pass

        self._ui(_apply)
        if not available:
            if core.sdk_is_installed():
                # 已安装却导入失败：启动时未提示过，这里补充说明。
                self._log("modelscope 已安装但导入失败（可能缺少运行依赖）。")
                self._log("请执行 {0} 或 pip install -U modelscope 后重启程序。".format(
                    core.INSTALL_COMMAND))
            # 未安装的情况已在 _refresh_sdk_status 中同步提示，避免重复日志。
        else:
            self._log("modelscope SDK 已加载（版本 {0}）。".format(version or "未知"))
            self._log("下载策略：{0}".format(
                "local_dir 直写目标文件夹" if supports
                else "cache_dir 下载后同步到目标文件夹（本机 SDK 不支持 local_dir）"))

    def _show_install_guide(self) -> None:
        """SDK 缺失时给出清晰的一键安装指引。"""
        self._log("未安装 modelscope SDK，无法下载（文件清单预览仍可用）。")
        self._log("请在项目目录执行：{0}".format(core.INSTALL_COMMAND))
        messagebox.showwarning(
            "需要先安装依赖",
            "未检测到 modelscope SDK，无法开始下载。\n\n"
            "请在本程序所在目录打开命令行并执行：\n\n"
            "    {0}\n\n"
            "也可以直接双击目录下的【安装依赖.bat】。\n"
            "安装完成后重新启动本程序即可。\n\n"
            "说明：文件清单预览不依赖 SDK，现在仍可正常使用。".format(
                core.INSTALL_COMMAND),
        )

    def _on_close(self) -> None:
        """关闭窗口前保存配置并做必要确认。"""
        if self._busy:
            if not messagebox.askyesno(
                "确认退出",
                "还有任务正在进行，确定要退出吗？\n（建议先点击【停止 / 取消】）",
            ):
                return
            self._cancel_event.set()
        try:
            self._collect_config()
        except Exception:
            pass
        self._stop_elapsed_timer()
        if self._ui_pump_job:
            try:
                self.root.after_cancel(self._ui_pump_job)
            except tk.TclError:
                pass
            self._ui_pump_job = None
        self._drain_ui_queue()
        self.root.destroy()


def main() -> None:
    """程序入口：创建根窗口并进入 tkinter 主循环。"""
    try:  # Windows 高 DPI 下让界面不模糊；非 Windows 或旧系统直接忽略。
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    root.title(APP_TITLE)
    width, height = WINDOW_SIZE
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    root.geometry("{0}x{1}+{2}+{3}".format(
        width, height,
        max(0, (screen_width - width) // 2),
        max(0, (screen_height - height) // 2),
    ))
    root.minsize(940, 720)
    DownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # pythonw.exe 无控制台，异常时落一份日志便于排查。
        import traceback
        _error = traceback.format_exc()
        try:
            with open(str(Path(__file__).with_name("启动错误.log")), "w",
                      encoding="utf-8") as _handle:
                _handle.write(_error)
        except Exception:
            pass
        raise
