# -*- coding: utf-8 -*-
"""复现 Bug：文件清单里只勾 1 个文件，实际却下载整模型。

诊断思路（不联网、不下载任何字节）
--------------------------------
用**真实 GUI 代码**（``gui_app.DownloaderApp`` + 真实 Treeview 勾选逻辑）
走一遍「点【查看文件清单】→ 只留 1 个勾选 → 点【开始下载】」，把真正传给
``core.download_model`` 的 ``allow_patterns`` / ``ignore_patterns`` /
``total_bytes`` 抓下来。SDK 的过滤语义是「``allow_patterns`` 为空 = 全部下载」，
因此只要抓到空列表，就证明「勾选完全没传到 SDK」。

用法（项目根目录，必须用装了 tkinter 的隔离 venv）：
    pack/.venv/Scripts/python.exe pack/repro_select_bug.py --tag before
    pack/.venv/Scripts/python.exe pack/repro_select_bug.py --tag after

结果写入 ``pack/repro_select_bug_<tag>.json``，便于修复前后 A/B 对照。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 真实模型 Qwen/Qwen2.5-0.5B-Instruct 的清单（11 个文件 / 953 MB）此处只保留
# 4 个代表项：足够证明「勾选 1 个 vs 勾选全部」的差异，且完全离线。
FAKE_FILES = (
    ("README.md", 4917, False),
    ("config.json", 660, False),
    (".gitattributes", 1519, False),
    ("model.safetensors", 988097824, True),
)


class _FakeMessagebox:
    """拦截弹窗，避免脚本被模态对话框卡住。"""

    def __init__(self) -> None:
        self.calls: list = []

    def _record(self, kind: str, title: str, message: str) -> str:
        self.calls.append({"kind": kind, "title": title, "message": message})
        return "ok"

    def showinfo(self, title: str = "", message: str = "", **_kw) -> str:
        return self._record("info", title, message)

    def showerror(self, title: str = "", message: str = "", **_kw) -> str:
        return self._record("error", title, message)

    def showwarning(self, title: str = "", message: str = "", **_kw) -> str:
        return self._record("warning", title, message)


def main() -> int:
    """驱动真实 GUI 勾选逻辑并落盘「传给 SDK 的参数」。"""
    tag = "run"
    if "--tag" in sys.argv:
        tag = sys.argv[sys.argv.index("--tag") + 1]

    # 隔离 APPDATA：不碰用户真实配置，也不写入 OneDrive。
    sandbox = Path(tempfile.mkdtemp(prefix="msdl-repro-select-"))
    os.environ["APPDATA"] = str(sandbox / "appdata")

    import tkinter as tk

    import gui_app
    import ms_downloader_core as core

    captured: dict = {}
    fake_box = _FakeMessagebox()

    def _fake_download_model(**kwargs):
        """记录 GUI 真正传给 SDK 的过滤参数，不联网。"""
        captured.update(kwargs)
        return str(kwargs.get("target_dir", ""))

    gui_app.messagebox = fake_box  # 弹窗全部拦截
    original_installed = core.sdk_is_installed
    original_download = core.download_model
    core.sdk_is_installed = lambda: True          # 跳过 SDK 安装检查
    core.download_model = _fake_download_model    # 拦截真实下载

    report: dict = {
        "tag": tag,
        "python": sys.version.split()[0],
        "listing": [{"path": p, "size": s} for p, s, _ in FAKE_FILES],
        "selection": "只勾选 README.md（1/4）",
    }
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        app = gui_app.DownloaderApp(root)
        app.var_model.set("Qwen/Qwen2.5-0.5B-Instruct")
        app.var_dir.set(str(sandbox / "out"))
        app.var_revision.set("master")
        app.var_allow.set("")
        app.var_ignore.set("")

        info = core.ModelInfo(
            model_id="Qwen/Qwen2.5-0.5B-Instruct",
            revision="master",
            files=[core.RepoFile(path=p, size=s, is_lfs=lfs)
                   for p, s, lfs in FAKE_FILES],
            source="REST",
        )
        app._show_model_info(info)
        app._select_all(False)
        app._toggle_row("README.md")
        app._drain_ui_queue()
        report["checked_rows"] = app._selected_paths()
        report["only_selected_var"] = bool(app.var_only_selected.get())

        app.btn_download.invoke()
        app._drain_ui_queue()
        worker = app._worker
        if worker is not None:
            worker.join(15)
        app._drain_ui_queue()
        root.update_idletasks()

        report["allow_patterns_sent_to_sdk"] = captured.get("allow_patterns")
        report["ignore_patterns_sent_to_sdk"] = captured.get("ignore_patterns")
        report["total_bytes_sent_to_sdk"] = captured.get("total_bytes")
        report["model_total_bytes"] = info.total_size
        report["log"] = _log_text(app)
        report["popups"] = fake_box.calls
        allow = captured.get("allow_patterns") or []
        report["verdict"] = (
            "BUG_REPRODUCED：allow_patterns 为空 → SDK 下载该模型全部文件"
            if not allow else
            "FIXED：allow_patterns={} → 只下载勾选文件".format(allow))
    except Exception as exc:  # noqa: BLE001 - 复现脚本要捕获一切
        import traceback

        report["verdict"] = "SCRIPT_ERROR"
        report["exception"] = "{}: {}".format(type(exc).__name__, exc)
        report["traceback"] = traceback.format_exc()
    finally:
        core.sdk_is_installed = original_installed
        core.download_model = original_download
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass

    out = ROOT / "pack" / "repro_select_bug_{}.json".format(tag)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print("[repro-select] 结果：{}".format(out))
    print("[repro-select] 勾选行：{}".format(report.get("checked_rows")))
    print("[repro-select] only_selected 变量：{}".format(
        report.get("only_selected_var")))
    print("[repro-select] 传给 SDK 的 allow_patterns：{}".format(
        report.get("allow_patterns_sent_to_sdk")))
    print("[repro-select] 传给 SDK 的 total_bytes：{}（模型总大小 {}）".format(
        report.get("total_bytes_sent_to_sdk"), report.get("model_total_bytes")))
    print("[repro-select] 结论：{}".format(report.get("verdict")))
    return 0


def _log_text(app) -> str:
    """取出日志区全文，便于在报告里核对 GUI 明确写了什么。"""
    try:
        return app.log_text.get("1.0", "end")
    except Exception:
        return ""


if __name__ == "__main__":
    sys.exit(main())
