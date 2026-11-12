# -*- coding: utf-8 -*-
"""ModelScope 下载器 · 单文件 exe 入口。

运行方式：
* 无参数              —— 启动图形界面（用户正常双击）。
* ``--self-check``    —— 内置自检，结果写到 exe 同目录的「自检结果.txt / .json」。
* ``--smoke-download``—— 真实联网下载一个几 KB 的模型文件，验证 SDK 全链路，
                         结果写到「下载自检结果.txt / .json」。
* ``--smoke-progress``—— 在真实 windowed（无标准句柄）环境下，强制走
                         「启用 SDK 进度回调 + 真实下载」链路，验证旧版双击
                         即崩的 AttributeError 已修复，结果写到
                         「进度回调自检结果.txt / .json」。
* ``--smoke-cancel``  —— 用可控 fake SDK 验证「停止 / 取消」能真实中断 SDK
                         下载循环并复位 GUI 状态机，结果写到
                         「取消自检结果.txt / .json」。
* ``--smoke-select``  —— 验证「文件清单勾选即下载范围」：真实 GUI 里只勾 1
                         个小文件后真实下载，断言目标文件夹里只有该文件，
                         结果写到「勾选下载自检结果.txt / .json」。

自检开关是刻意保留在交付 exe 里的：单文件程序脱离 Python 环境后，用户也能
用一条命令自证「SDK / tkinter / 网络」是否正常，便于排查网络或杀软问题。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# 源码模式（非 frozen）下把项目根加入 sys.path，便于直接运行本文件自检；
# 打包成 exe 后模块已内置，此分支不执行，无副作用。
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# windowed 模式标准流兜底（Bug A 根因修复）
# ---------------------------------------------------------------------------
# PyInstaller --windowed 且由资源管理器双击启动时，进程没有任何标准句柄，
# CPython 会把 sys.stdin/stdout/stderr（以及 sys.__stdin__ 等）初始化为 None。
# tqdm 4.70 在 tqdm/std.py::__init__ 把 sys.stderr 包进
# tqdm/utils.py::ObjectWrapper（DisableOnWriteError），首次刷新进度条时经
# __getattr__ 访问 None.write，抛
#     AttributeError: 'NoneType' object has no attribute 'write'
# modelscope_hub/_download.py:1303 的 `with tqdm(...)` 就是触发点（旧版 GUI
# 日志「已启用 SDK 进度回调」后约 1 秒崩溃）。
# 这里在导入 modelscope / tkinter 之前，把失效的标准流换成指向 os.devnull 的
# 真实 TextIOWrapper：具备 write/flush/isatty/fileno/encoding 等完整接口，
# 第三方库探测时不会再炸，输出静默丢弃（与 windowed 无控制台语义一致）。
_STDIO_GUARD_INSTALLED: bool = False


def _install_stdio_guard() -> bool:
    """把 windowed 下为 ``None`` 的标准流替换为安全的 devnull 流。

    源码模式（有真实控制台）三个流都有效时直接返回 ``False``，不改动任何
    ``sys`` 属性、不打开任何文件，保证源码模式行为完全不变。

    Returns:
        是否实际安装过兜底（至少替换 / 补齐了一个流）。
    """
    global _STDIO_GUARD_INSTALLED
    need_in = sys.stdin is None or getattr(sys, "__stdin__", None) is None
    need_out = sys.stdout is None or getattr(sys, "__stdout__", None) is None
    need_err = sys.stderr is None or getattr(sys, "__stderr__", None) is None
    if not (need_in or need_out or need_err):
        return False
    import os
    try:
        reader = (open(os.devnull, "r", encoding="utf-8", errors="ignore")
                  if need_in else None)
        writer_out = (open(os.devnull, "w", encoding="utf-8", errors="ignore")
                      if need_out else None)
        writer_err = (open(os.devnull, "w", encoding="utf-8", errors="ignore")
                      if need_err else None)
    except OSError:
        # 极端情况下连 nul 都打不开：保持原状，文件日志兜底仍然有效。
        return False
    if sys.stdin is None and reader is not None:
        sys.stdin = reader
    if sys.stdout is None and writer_out is not None:
        sys.stdout = writer_out
    if sys.stderr is None and writer_err is not None:
        sys.stderr = writer_err
    # 第三方库（logging.lastResort / tqdm / colorama）常直接读 __stderr__，
    # 必须一并补齐；让 __xxx__ 与 xxx 指向同一对象，避免比较时出现歧义。
    if getattr(sys, "__stdin__", None) is None:
        sys.__stdin__ = sys.stdin
    if getattr(sys, "__stdout__", None) is None:
        sys.__stdout__ = sys.stdout
    if getattr(sys, "__stderr__", None) is None:
        sys.__stderr__ = sys.stderr
    _STDIO_GUARD_INSTALLED = True
    return True


# 必须在任何 modelscope / tkinter 导入之前执行。本模块所有第三方 import 都在
# 函数体内惰性完成，因此这里的模块级调用一定早于它们。
_install_stdio_guard()


def app_dir() -> Path:
    """返回 exe 所在目录（而非 onefile 解压出来的 _MEI 临时目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _write_report(name: str, title: str, checks: list, ok: bool) -> int:
    """把自检结果同时写成可读文本与 JSON，便于人工查看和自动化断言。"""
    out_dir = app_dir()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        title,
        "时间：" + stamp,
        "程序：" + str(Path(sys.executable).resolve()),
        "总体结果：" + ("全部通过" if ok else "存在失败项"),
        "=" * 60,
    ]
    for label, passed, detail in checks:
        lines.append("[{}] {}".format("PASS" if passed else "FAIL", label))
        lines.append("       " + str(detail).replace("\n", "\n       "))
    lines.append("")
    lines.append("提示：这是一个纯自检报告，不影响程序使用，可随时删除。")
    try:
        (out_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
        (out_dir / (Path(name).stem + ".json")).write_text(
            json.dumps(
                {
                    "ok": ok,
                    "time": stamp,
                    "checks": [
                        {"name": a, "ok": bool(b), "detail": str(c)}
                        for a, b, c in checks
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:  # 只读目录等极端情况，windowed 下 stderr 可能不可用
        pass
    return 0 if ok else 1


def self_check() -> int:
    """验证 exe 内部的 Python / SDK / GUI 运行时是否完整。"""
    checks: list = []

    def add(label, fn) -> None:
        try:
            detail = fn()
            checks.append((label, True, detail))
        except Exception as exc:  # noqa: BLE001 - 自检要让每一项都跑完
            checks.append(
                (label, False, "{}: {}\n{}".format(
                    type(exc).__name__, exc, traceback.format_exc(limit=4)))
            )

    def _runtime():
        meipass = getattr(sys, "_MEIPASS", "")
        return "frozen={} | Python {} | _MEIPASS={}".format(
            getattr(sys, "frozen", False), sys.version.split()[0],
            meipass or "(无，源码模式运行)")

    def _stdio():
        """标准流必须全部可用，否则 tqdm 会写 None 而崩溃（Bug A 回归项）。"""
        guard = globals().get("_STDIO_GUARD_INSTALLED", False)
        streams = (
            sys.stdin, sys.stdout, sys.stderr,
            getattr(sys, "__stdin__", None),
            getattr(sys, "__stdout__", None),
            getattr(sys, "__stderr__", None),
        )
        if any(stream is None or not hasattr(stream, "write")
               for stream in streams):
            raise RuntimeError("存在为 None / 不可写的标准流，tqdm 会崩溃")
        return ("stdout={} | stderr={} | stdin={} | __stderr__={} | "
                "兜底已安装={} | 六个流均可用=True").format(
                    type(sys.stdout).__name__, type(sys.stderr).__name__,
                    type(sys.stdin).__name__,
                    type(getattr(sys, "__stderr__", None)).__name__, guard)

    def _sdk():
        import inspect
        import modelscope
        from modelscope.hub.snapshot_download import snapshot_download
        sig = inspect.signature(snapshot_download)
        names = list(sig.parameters)
        return ("modelscope {} | snapshot_download 参数 {} 个 | "
                "local_dir={} | allow_patterns={} | max_workers={}").format(
                    getattr(modelscope, "__version__", "未知"), len(names),
                    "local_dir" in names, "allow_patterns" in names,
                    "max_workers" in names)

    def _hub_api():
        from modelscope.hub.api import HubApi
        methods = [m for m in ("get_model_files", "list_model_files",
                               "get_model_file_list") if hasattr(HubApi, m)]
        return "HubApi 可用，文件清单方法：{}".format(", ".join(methods) or "（运行时探测）")

    def _cert():
        import certifi
        p = Path(certifi.where())
        return "证书路径存在={} | 大小={} 字节".format(
            p.is_file(), p.stat().st_size if p.is_file() else 0)

    def _core():
        import ms_downloader_core as core
        available, func, version = core._load_sdk()
        return ("SDK 可用={} | 版本={} | local_dir 探测={} | "
                "find_spec={}").format(
                    available, version or "未知",
                    core.probe_local_dir_support(), core.sdk_is_installed())

    def _tk():
        import tkinter as tk
        root = tk.Tk()
        try:
            root.withdraw()
            return "Tk {} | 主窗口构造成功".format(root.tk.call("info", "patchlevel"))
        finally:
            root.destroy()

    def _gui():
        import tkinter as tk
        import gui_app
        root = tk.Tk()
        try:
            root.withdraw()
            app = gui_app.DownloaderApp(root)
            root.update_idletasks()
            has_log = hasattr(app, "_log")
            return "DownloaderApp 完整构造成功 | 日志控件可用={}".format(has_log)
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    add("运行环境（是否已脱离本机 Python）", _runtime)
    add("windowed 标准流兜底（tqdm 不会写 None）", _stdio)
    add("modelscope SDK 与 snapshot_download 签名", _sdk)
    add("HubApi 文件清单接口", _hub_api)
    add("HTTPS 根证书（certifi）", _cert)
    add("下载核心层与 local_dir 兼容探测", _core)
    add("tkinter 运行时与 tcl/tk 数据", _tk)
    add("完整 GUI 窗口构造", _gui)

    ok = all(item[1] for item in checks)
    return _write_report("自检结果.txt", "ModelScope 下载器 · 单文件 exe 自检报告", checks, ok)


def smoke_download(target: str = "") -> int:
    """真实下载一个几 KB 的模型文件，验证 SDK 下载链路真的能落到指定文件夹。"""
    checks: list = []
    dest = Path(target) if target else Path(tempfile.mkdtemp(prefix="msdl-selfcheck-"))
    logs: list = []

    try:
        import ms_downloader_core as core

        # 先验证清单查询（SDK HubApi 路径），只是一次轻量 API 调用
        try:
            info = core.fetch_repo_files(
                "Qwen/Qwen2.5-0.5B-Instruct", log_cb=logs.append)
            checks.append(("文件清单查询", info.file_count > 0,
                           "{} 个文件，合计 {}".format(
                               info.file_count, core.format_size(info.total_size))))
        except Exception as exc:
            checks.append(("文件清单查询", False,
                           "{}: {}".format(type(exc).__name__, exc)))

        # 再真实下载：只取 README.md（几 KB），不下载模型权重
        logs.append("---- 开始真实下载冒烟测试 ----")
        returned = core.download_model(
            model_id="Qwen/Qwen2.5-0.5B-Instruct",
            target_dir=str(dest),
            revision="master",
            allow_patterns=["README.md"],
            max_workers=1,
            log_cb=logs.append,
            apply_env=False,
        )
        hits = [p for p in dest.rglob("README.md") if p.is_file()]
        sizes = [p.stat().st_size for p in hits]
        checks.append((
            "真实下载并落到指定文件夹",
            bool(hits) and max(sizes) > 0,
            "目标目录={} | SDK 返回={} | 命中 README.md {} 个，大小={} 字节".format(
                dest, returned, len(hits), sizes)))
        checks.append(("下载日志", True, "\n".join(logs[-12:])))
    except Exception as exc:
        checks.append(("真实下载并落到指定文件夹", False,
                       "{}: {}\n{}\n日志：\n{}".format(
                           type(exc).__name__, exc,
                           traceback.format_exc(limit=6), "\n".join(logs[-12:]))))
        checks.append(("下载日志", True, "\n".join(logs[-12:])))

    ok = all(item[1] for item in checks)
    return _write_report(
        "下载自检结果.txt",
        "ModelScope 下载器 · 真实下载冒烟测试（仅下载 README.md，不含权重）",
        checks, ok)


def smoke_progress(target: str = "") -> int:
    """在真实 windowed 环境验证「启用 SDK 进度回调 + 真实下载」不再崩溃。

    与 GUI 下载完全同一条调用链：``core.download_model`` 内部构造
    ``progress_callbacks`` 并交给 ``snapshot_download``，SDK 在
    ``modelscope_hub/_download.py:1303`` 创建 tqdm 总进度条 —— 这正是旧版
    双击启动约 1 秒后抛 ``AttributeError`` 的位置。

    旧版 ``--smoke-download`` 之所以没暴露问题，是因为 ``verify_exe.py`` 用
    ``Popen(stdout=DEVNULL, stderr=DEVNULL)`` 给子进程传了有效标准句柄，
    windowed 进程里 ``sys.stderr`` 是 TextIOWrapper 而非 None；本用例由
    ``verify_bugfix.py`` 以「无标准句柄（等价双击）」方式启动，才能真正覆盖。
    """
    checks: list = []
    dest = Path(target) if target else Path(tempfile.mkdtemp(prefix="msdl-progress-"))
    logs: list = []

    def _streams() -> str:
        return ("stdout={} | stderr={} | stdin={} | __stderr__={} | "
                "兜底已安装={}").format(
                    type(sys.stdout).__name__, type(sys.stderr).__name__,
                    type(sys.stdin).__name__,
                    type(getattr(sys, "__stderr__", None)).__name__,
                    globals().get("_STDIO_GUARD_INSTALLED", False))

    try:
        if (sys.stderr is None or getattr(sys, "__stderr__", None) is None
                or sys.stdout is None):
            raise RuntimeError("标准流仍为 None，tqdm 会崩溃")
        checks.append(("windowed 标准流安全（六个流非 None）", True, _streams()))
    except Exception as exc:
        checks.append(("windowed 标准流安全（六个流非 None）", False,
                       "{}: {}".format(type(exc).__name__, exc)))

    try:
        import ms_downloader_core as core

        logs.append("---- 开始：启用 SDK 进度回调的真实下载 ----")
        returned = core.download_model(
            model_id="Qwen/Qwen2.5-0.5B-Instruct",
            target_dir=str(dest),
            revision="master",
            allow_patterns=["README.md"],
            max_workers=1,
            log_cb=logs.append,
            progress_cb=lambda done, total, message: None,
            apply_env=False,
        )
        callback_enabled = any("已启用 SDK 进度回调" in line for line in logs)
        callback_lines = [line for line in logs if "进度回调" in line]
        hits = [p for p in dest.rglob("README.md") if p.is_file()]
        sizes = [p.stat().st_size for p in hits]
        checks.append(("SDK 进度回调确实已启用并交给 SDK", callback_enabled,
                       "；".join(callback_lines) or "（未启用）"))
        checks.append(("真实下载落到指定文件夹且无 AttributeError",
                       bool(hits) and max(sizes, default=0) > 0,
                       "目标目录={} | SDK 返回={} | 命中 README.md {} 个，大小={} 字节".format(
                           dest, returned, len(hits), sizes)))
        checks.append(("下载日志", True, "\n".join(logs[-12:])))
    except Exception as exc:
        checks.append(("真实下载落到指定文件夹且无 AttributeError", False,
                       "{}: {}\n{}\n日志：\n{}".format(
                           type(exc).__name__, exc,
                           traceback.format_exc(limit=6), "\n".join(logs[-12:]))))
        checks.append(("下载日志", True, "\n".join(logs[-12:])))

    ok = all(item[1] for item in checks)
    return _write_report(
        "进度回调自检结果.txt",
        "ModelScope 下载器 · 进度回调真实下载验证（windowed 无标准句柄环境）",
        checks, ok)


def smoke_cancel() -> int:
    """验证「停止 / 取消」能真实中断 SDK 下载循环并复位状态机（Bug B）。

    不联网、不下载任何权重：注入一个可控的 fake ``snapshot_download``，它像
    真实 SDK 一样实例化 ``progress_callbacks`` 并逐块调用 ``update``；在下载
    中途置位 cancel_event，期望：

    1. core 的进度回调抛出 ``DownloadCancelled``，中断 fake SDK 的下载循环；
    2. core.download_model 把 SDK 抛出的异常统一转译为 ``DownloadCancelled``
       （而不是误报「任务失败」的 DownloadError）；
    3. GUI 的按钮状态机在任务结束后能完整复位。
    """
    checks: list = []
    logs: list = []
    dest = Path(tempfile.mkdtemp(prefix="msdl-cancel-"))
    cancel_event = threading.Event()
    captured: dict = {"callbacks": None, "blocks": 0}

    def _fake_snapshot_download(
        model_id: str,
        revision: str = "master",
        local_dir: str = "",
        cache_dir: str = "",
        allow_patterns: Optional[list] = None,
        ignore_patterns: Optional[list] = None,
        max_workers: int = 1,
        progress_callbacks: Optional[list] = None,
        **kwargs: object,
    ) -> str:
        """模拟 SDK：逐块上报进度，第 3 块时用户点了【停止 / 取消】。

        同时复刻真实 SDK 的关键行为（``modelscope_hub/_download.py``
        1308-1334）：下载线程抛出的异常会被 ``future.result()`` 收集进
        ``failures``，最后统一抛 ``FileDownloadError``。这样才能验证 core 的
        「取消异常转译」分支，而不只是异常直接穿透的 trivial 情况。
        """
        captured["callbacks"] = progress_callbacks
        callbacks = [cls("README.md", 4917)
                     for cls in (progress_callbacks or [])]
        failure = None
        try:
            for block in range(1, 11):
                if block == 3:
                    cancel_event.set()
                captured["blocks"] = block
                for callback in callbacks:
                    callback.update(512)
        except Exception as exc:  # 模拟 SDK 收集失败文件
            failure = exc
        if failure is not None:
            raise RuntimeError(
                "1 of 1 file(s) failed to download（模拟 SDK FileDownloadError）"
            ) from failure
        return str(local_dir or dest)

    try:
        import ms_downloader_core as core

        backup = dict(core._SDK_CACHE)
        core._SDK_CACHE.clear()
        core._SDK_CACHE.update(
            checked=True, available=True, func=_fake_snapshot_download,
            version="fake-1.0", error="")
        outcome = "NO_EXCEPTION"
        detail = ""
        try:
            try:
                core.download_model(
                    model_id="Qwen/Qwen2.5-0.5B-Instruct",
                    target_dir=str(dest),
                    revision="master",
                    allow_patterns=["README.md"],
                    max_workers=1,
                    cancel_event=cancel_event,
                    log_cb=logs.append,
                    progress_cb=lambda done, total, message: None,
                    progress_interval=0.2,
                    apply_env=False,
                )
            except core.DownloadCancelled as exc:
                outcome = "DownloadCancelled"
                detail = str(exc)
            except Exception as exc:  # noqa: BLE001 - 记录实际异常类型
                outcome = type(exc).__name__
                detail = str(exc)
        finally:
            core._SDK_CACHE.clear()
            core._SDK_CACHE.update(backup)

        checks.append(("进度回调类已注入 SDK（取消注入点存在）",
                       bool(captured["callbacks"]),
                       "SDK 收到 progress_callbacks={} 个类".format(
                           len(captured["callbacks"] or []))))
        checks.append(("取消信号真实中断 SDK 下载循环",
                       outcome == "DownloadCancelled" and captured["blocks"] == 3,
                       "结果={} | 中断于第 {}/10 块 | 异常={} | 日志尾部：{}".format(
                           outcome, captured["blocks"], detail,
                           " / ".join(logs[-3:]))))
        checks.append(("取消被识别为「已停止」而非「任务失败」",
                       any("已响应停止请求" in line or "下载已取消" in line
                           for line in logs),
                       "\n".join(logs[-6:])))
    except Exception as exc:
        checks.append(("取消信号真实中断 SDK 下载循环", False,
                       "{}: {}\n{}".format(type(exc).__name__, exc,
                                           traceback.format_exc(limit=6))))

    # GUI 状态机：点击停止 -> 事件置位 / 按钮禁用 -> 任务结束 -> 完整复位。
    try:
        import tkinter as tk
        import gui_app

        root = tk.Tk()
        try:
            root.withdraw()
            app = gui_app.DownloaderApp(root)
            app._set_busy(True)
            app._on_stop()
            root.update_idletasks()
            cancel_set = app._cancel_event.is_set()
            stop_disabled = str(app.btn_stop.cget("state")) == "disabled"
            app._set_busy(False)  # 模拟 worker 的 finally
            app._drain_ui_queue()
            root.update_idletasks()
            recovered = (not app._busy
                         and str(app.btn_download.cget("state")) == "normal")
            checks.append(("GUI 停止按钮状态机可完整复位",
                           cancel_set and stop_disabled and recovered,
                           "cancel_set={} | 停止按钮已禁用={} | 任务结束后复位={}".format(
                               cancel_set, stop_disabled, recovered)))
        finally:
            try:
                root.destroy()
            except Exception:
                pass
    except Exception as exc:
        checks.append(("GUI 停止按钮状态机可完整复位", False,
                       "{}: {}\n{}".format(type(exc).__name__, exc,
                                           traceback.format_exc(limit=4))))

    ok = all(item[1] for item in checks)
    return _write_report(
        "取消自检结果.txt",
        "ModelScope 下载器 · 停止/取消控制流验证（可控 fake SDK，不联网）",
        checks, ok)


class _QuietMessagebox:
    """自检用弹窗替身：记录内容但不显示，避免模态框卡住无人值守的自检。"""

    def __init__(self) -> None:
        self.calls: list = []

    def _record(self, kind: str, title: str, message: str) -> str:
        self.calls.append({"kind": kind, "title": title, "message": message})
        return "ok"

    def showinfo(self, title: str = "", message: str = "", **kwargs: Any) -> str:
        return self._record("info", title, message)

    def showerror(self, title: str = "", message: str = "", **kwargs: Any) -> str:
        return self._record("error", title, message)

    def showwarning(self, title: str = "", message: str = "", **kwargs: Any) -> str:
        return self._record("warning", title, message)

    def askyesno(self, title: str = "", message: str = "", **kwargs: Any) -> bool:
        self._record("ask", title, message)
        return False


def _pump(app: Any, root: Any, seconds: float = 0.05) -> None:
    """驱动界面队列与事件循环（无 mainloop 的自检里等价于定时泵）。"""
    try:
        app._drain_ui_queue()
    except Exception:
        pass
    try:
        root.update()
    except Exception:
        pass
    if seconds:
        time.sleep(seconds)


def _download_running(app: Any) -> bool:
    """判断当前是否有下载任务在跑（含按钮状态机标记）。"""
    worker = getattr(app, "_worker", None)
    if worker is not None and worker.is_alive():
        return True
    return bool(getattr(app, "_busy", False))


def _wait_worker(app: Any, root: Any, timeout: float) -> float:
    """等待后台任务结束并排空界面队列，返回耗时秒数。"""
    started = time.time()
    while time.time() - started < timeout:
        _pump(app, root)
        if not _download_running(app):
            _pump(app, root, 0.2)
            return time.time() - started
    return time.time() - started


def _log_text(app: Any) -> str:
    """读取日志区全文（自检断言直接看用户能看到的内容）。"""
    try:
        return app.log_text.get("1.0", "end")
    except Exception:
        return ""


def _scope_line(text: str, keyword: str) -> str:
    """从日志全文里挑出包含关键字的最后一行，便于写进报告。"""
    lines = [line.strip() for line in text.splitlines() if keyword in line]
    return lines[-1] if lines else "（日志中没有「{}」）".format(keyword)


def _target_files(root: Path) -> list:
    """列出目标文件夹里的所有文件（相对路径，含隐藏文件）。

    隐藏文件 / 目录同样计入：只勾 1 个文件时，SDK 若不听话多下了
    ``.gitattributes`` 之类的小文件，也必须被这条断言抓出来。
    """
    result: list = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            try:
                result.append(path.relative_to(root).as_posix())
            except ValueError:
                result.append(str(path))
    return result


def smoke_select(target: str = "") -> int:
    """验证「文件清单勾选即下载范围」端到端真实生效。

    用**真实 GUI**（``gui_app.DownloaderApp`` + 真实 Treeview 勾选）在
    windowed（无标准句柄）环境里跑完整链路：

    1. 点【查看文件清单】拿到真实清单（``Qwen/Qwen2.5-0.5B-Instruct``）；
    2. 一个都不勾 → 必须被拦下，不启动下载线程（绝不静默下整模型）；
    3. 全部勾选 → 等价「下载全部」：``allow_patterns`` 为空、进度分母是整模型
       大小、日志写「本次下载 M/M 个文件」（此步注入 fake 只抓参数，不联网）；
    4. 未获取清单 → 下载全部，且日志明确写「未获取清单，本次下载全部文件」；
    5. 只勾 ``README.md`` → **真实下载**，断言目标文件夹里只有该文件，且进度
       分母等于勾选文件的字节数（而不是整模型总大小）。

    Args:
        target: 目标文件夹（下载内容直接落在里面）；为空时用系统临时目录下的
            ``out`` 子目录。

    Returns:
        进程退出码（0 表示全部通过）。
    """
    checks: list = []
    logs: list = []
    model_id = "Qwen/Qwen2.5-0.5B-Instruct"
    want_file = "README.md"

    if target:
        dest = Path(target)
    else:
        dest = Path(tempfile.mkdtemp(prefix="msdl-select-")) / "out"
    dest.mkdir(parents=True, exist_ok=True)
    # 隔离配置目录：自检不读写用户真实的 %APPDATA%/ModelScopeDownloader。
    config_home = Path(tempfile.mkdtemp(prefix="msdl-select-appdata-"))
    os.environ["APPDATA"] = str(config_home)

    root = None
    app = None
    try:
        import tkinter as tk

        import gui_app
        import ms_downloader_core as core

        streams_ok = (sys.stdout is not None and sys.stderr is not None
                      and getattr(sys, "__stdout__", None) is not None
                      and getattr(sys, "__stderr__", None) is not None)
        checks.append((
            "windowed 标准流安全（无控制台句柄下仍可下载）",
            streams_ok,
            "stdout={} | stderr={} | __stdout__={} | 兜底已安装={}".format(
                type(sys.stdout).__name__, type(sys.stderr).__name__,
                type(getattr(sys, "__stdout__", None)).__name__,
                globals().get("_STDIO_GUARD_INSTALLED", False))))

        box = _QuietMessagebox()
        gui_app.messagebox = box
        captured: list = []

        def _record_only(**kwargs: Any) -> str:
            """只记录 GUI 传给 SDK 的参数，不联网。"""
            captured.append(dict(kwargs))
            return str(kwargs.get("target_dir", ""))

        root = tk.Tk()
        root.withdraw()
        app = gui_app.DownloaderApp(root)
        app.var_model.set(model_id)
        app.var_dir.set(str(dest))
        app.var_revision.set("master")
        app._drain_ui_queue()

        # ---- 1. 真实清单 ----
        app.btn_preview.invoke()
        preview_elapsed = _wait_worker(app, root, 180)
        info = app._model_info
        rows = list(app.tree.get_children())
        info_ok = (info is not None and len(rows) == info.file_count
                   and want_file in rows)
        checks.append((
            "真实文件清单已获取（清单可勾选）",
            info_ok,
            "{} 个文件 / Treeview {} 行 / 合计 {} / 耗时 {:.1f}s / 含 {}={}".format(
                getattr(info, "file_count", 0), len(rows),
                core.format_size(getattr(info, "total_size", 0)),
                preview_elapsed, want_file, want_file in rows)))
        if not info_ok:
            raise RuntimeError("未取到可勾选的文件清单，后续勾选用例无法验证")

        total_count = info.file_count
        model_bytes = info.total_size
        small_bytes = 0
        for item in info.files:
            if item.path == want_file:
                small_bytes = item.size
        logs.append("清单文件：{}；{}={} 字节".format(
            [item.path for item in info.files], want_file, small_bytes))

        # ---- 2. 一个都不勾 → 必须被拦下 ----
        app._select_all(False)
        app._drain_ui_queue()
        popups_before = len(box.calls)
        app.btn_download.invoke()
        _pump(app, root, 0.3)
        blocked = (not _download_running(app) and len(box.calls) > popups_before
                   and "勾选" in str(box.calls[-1]["message"]))
        checks.append((
            "全不选 + 开始下载 = 明确拦下（不静默下载整模型）",
            blocked,
            "是否启动下载={} | 弹窗标题={} | 提示含「勾选」={}".format(
                _download_running(app),
                box.calls[-1]["title"] if len(box.calls) > popups_before else "（无）",
                "勾选" in str(box.calls[-1]["message"]) if box.calls else False)))

        original_download = core.download_model
        # ---- 3. 全部勾选 = 下载全部 ----
        app._select_all(True)
        app._drain_ui_queue()
        try:
            core.download_model = _record_only
            captured.clear()
            app.btn_download.invoke()
            _wait_worker(app, root, 60)
        finally:
            core.download_model = original_download
        all_allow = captured[0].get("allow_patterns") if captured else None
        all_total = captured[0].get("total_bytes") if captured else -1
        all_text = _log_text(app)
        checks.append((
            "全部勾选 = 下载全部（allow_patterns 为空 + 日志写 M/M）",
            all_allow == [] and all_total == model_bytes
            and "本次下载 {0}/{0} 个文件".format(total_count) in all_text,
            "allow_patterns={} | 进度分母={}（整模型 {}）| {}".format(
                all_allow, all_total, model_bytes,
                _scope_line(all_text, "本次下载"))))

        # ---- 4. 未获取清单 → 下载全部，但日志必须写明 ----
        saved_info = app._model_info
        app._model_info = None
        for iid in app.tree.get_children():
            app.tree.delete(iid)
        app._drain_ui_queue()
        try:
            core.download_model = _record_only
            captured.clear()
            app.btn_download.invoke()
            _wait_worker(app, root, 60)
        finally:
            core.download_model = original_download
        none_allow = captured[0].get("allow_patterns") if captured else None
        none_text = _log_text(app)
        checks.append((
            "未获取清单时下载全部且日志明确写出",
            none_allow == [] and "未获取清单，本次下载全部文件" in none_text,
            "allow_patterns={} | {}".format(
                none_allow, _scope_line(none_text, "未获取清单"))))

        # ---- 5. 只勾 1 个文件 → 真实下载，断言目录里只有它 ----
        app._show_model_info(saved_info)
        app._select_all(False)
        app._toggle_row(want_file)
        app._drain_ui_queue()
        checked = app._selected_paths()
        totals: list = []
        original_progress = app._on_progress

        def _spy_progress(done: int, total: int, message: str = "") -> None:
            totals.append(int(total or 0))
            return original_progress(done, total, message)

        app._on_progress = _spy_progress
        try:
            app.btn_download.invoke()
            download_elapsed = _wait_worker(app, root, 300)
        finally:
            app._on_progress = original_progress
        sel_text = _log_text(app)
        files = _target_files(dest)
        nonzero_totals = sorted({value for value in totals if value})
        checks.append((
            "只勾 1 个文件 → 目标文件夹里只有该文件",
            files == [want_file] and checked == [want_file],
            "勾选={} | 目标文件夹={} | 耗时 {:.1f}s".format(
                checked, files, download_elapsed)))
        checks.append((
            "进度分母 = 勾选文件字节数（而非整模型总大小）",
            bool(nonzero_totals) and nonzero_totals == [small_bytes],
            "进度分母取值={} | {}={} 字节 | 整模型={} 字节".format(
                nonzero_totals, want_file, small_bytes, model_bytes)))
        checks.append((
            "日志写明本次下载范围（N/M 个文件 + 合计）",
            "本次下载 1/{} 个文件".format(total_count) in sel_text,
            _scope_line(sel_text, "本次下载")))
        logs.append("单文件下载日志尾部：" + " / ".join(
            [line.strip() for line in sel_text.splitlines() if line.strip()][-4:]))
    except Exception as exc:  # noqa: BLE001 - 自检要让每一项都跑完
        checks.append((
            "勾选下载链路执行", False,
            "{}: {}\n{}".format(type(exc).__name__, exc,
                                traceback.format_exc(limit=6))))
    finally:
        if app is not None and root is not None:
            try:
                _pump(app, root, 0)
            except Exception:
                pass
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        # 自检产生的临时配置目录顺手清掉，避免反复自检在 %TEMP% 里留垃圾。
        try:
            import shutil

            shutil.rmtree(str(config_home), ignore_errors=True)
        except Exception:
            pass

    ok = all(item[1] for item in checks)
    checks.append(("自检补充信息", True, "\n".join(logs) or "（无）"))
    return _write_report(
        "勾选下载自检结果.txt",
        "ModelScope 下载器 · 文件清单勾选即下载范围（含 1 次真实小文件下载）",
        checks, ok)


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--self-check":
        return self_check()
    if args and args[0] == "--smoke-download":
        return smoke_download(args[1] if len(args) > 1 else "")
    if args and args[0] == "--smoke-progress":
        return smoke_progress(args[1] if len(args) > 1 else "")
    if args and args[0] == "--smoke-cancel":
        return smoke_cancel()
    if args and args[0] == "--smoke-select":
        return smoke_select(args[1] if len(args) > 1 else "")
    if args and args[0] in ("--version", "-V"):
        import modelscope
        print("ModelScope 下载器 · 单文件版 | Python {} | modelscope {}".format(
            sys.version.split()[0], getattr(modelscope, "__version__", "未知")))
        return 0
    import gui_app
    gui_app.main()
    return 0


if __name__ == "__main__":
    try:
        _code = main()
    except Exception:
        # --windowed 无控制台，异常落盘便于排查
        try:
            (app_dir() / "启动错误.log").write_text(
                traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        _code = 1
    sys.exit(_code)
