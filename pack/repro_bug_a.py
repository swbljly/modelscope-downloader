# -*- coding: utf-8 -*-
"""复现 Bug A：模拟 PyInstaller --windowed 的 ``sys.stdout/stderr is None``。

用法（必须用隔离 venv 的解释器）：
    pack/.venv/Scripts/python.exe pack/repro_bug_a.py

本脚本把三个标准流（含 ``sys.__stdout__`` 等）全部置为 ``None``，然后**走与
GUI 下载完全相同的调用链**：``core.download_model`` 内部会构造 SDK 进度回调
（``progress_callbacks``）并真实联网下载一个几 KB 的 README.md。任何异常都会
带完整 traceback 写入 ``pack/repro_bug_a_result.json``，便于定位到具体文件:行号。

注意：只下载 README.md，不下载任何模型权重；目标目录是临时目录，不碰用户
OneDrive 里的任何文件。
"""
from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RESULT = ROOT / "pack" / "repro_bug_a_result.json"


def main() -> int:
    """执行复现并把结果落盘（不依赖任何可能已失效的标准流）。

    ``--with-guard`` 会加载修复后的 ``pack/entry.py`` 并安装标准流兜底，
    用**完全相同的调用链**再跑一遍，形成 Bug A 修复前后的 A/B 对照。
    """
    real_stdout = sys.stdout
    real_stderr = sys.stderr
    real_stdin = sys.stdin
    with_guard = "--with-guard" in sys.argv[1:]

    report: dict = {
        "scenario": ("sys.stdout/stderr/stdin 与 __*__ 全部置为 None"
                     "（模拟 windowed 双击启动）；"
                     + ("已加载 pack/entry.py 安装标准流兜底"
                        if with_guard else "未安装兜底（修复前基线）")),
        "model": "Qwen/Qwen2.5-0.5B-Instruct",
        "allow_patterns": ["README.md"],
        "python": sys.version.split()[0],
    }
    logs: list = []
    guard_installed = False

    # ---- 关键：模拟 PyInstaller --windowed 无控制台的真实运行状态 ----
    sys.stdout = None
    sys.stderr = None
    sys.stdin = None
    sys.__stdout__ = None
    sys.__stderr__ = None
    sys.__stdin__ = None

    if with_guard:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_msdl_entry_for_repro", ROOT / "pack" / "entry.py")
        entry_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry_mod)  # 模块级已自动安装一次兜底
        entry_mod._install_stdio_guard()    # 幂等；再次确认
        guard_installed = bool(entry_mod._STDIO_GUARD_INSTALLED)
    report["guard_installed"] = guard_installed

    try:
        import ms_downloader_core as core

        dest = Path(tempfile.mkdtemp(prefix="msdl-repro-a-"))
        report["target_dir"] = str(dest)
        returned = core.download_model(
            model_id="Qwen/Qwen2.5-0.5B-Instruct",
            target_dir=str(dest),
            revision="master",
            allow_patterns=["README.md"],
            max_workers=1,
            cancel_event=None,
            log_cb=logs.append,
            progress_cb=lambda done, total, message: None,
            apply_env=False,
        )
        report["exception"] = None
        report["exception_type"] = ""
        report["traceback"] = ""
        report["verdict"] = ("FIXED（同一链路，装兜底后正常下载）"
                             if with_guard else "NO_CRASH（未复现）")
        report["returned"] = str(returned)
        hits = [p for p in dest.rglob("README.md") if p.is_file()]
        report["downloaded_files"] = [{"path": str(p), "size": p.stat().st_size}
                                      for p in hits]
    except BaseException as exc:  # noqa: BLE001 - 复现脚本要捕获一切
        report["exception"] = "{}: {}".format(type(exc).__name__, exc)
        report["exception_type"] = type(exc).__name__
        report["traceback"] = traceback.format_exc()
        if "NoneType" in str(exc):
            report["verdict"] = ("STILL_BROKEN（兜底未生效）" if with_guard
                                 else "REPRODUCED（修复前基线）")
        else:
            report["verdict"] = "OTHER_ERROR"
        report["returned"] = ""
        report["downloaded_files"] = []
    report["logs"] = logs

    # ---- 恢复真实标准流，写报告并打印摘要 ----
    sys.stdout = real_stdout
    sys.stderr = real_stderr
    sys.stdin = real_stdin
    RESULT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print("[repro] 结果写入：{}".format(RESULT))
    print("[repro] with_guard={} | 兜底已安装={} | verdict={} | exception={}".format(
        with_guard, guard_installed, report["verdict"], report["exception"]))
    print("[repro] 命中文件：{}".format(report["downloaded_files"]))
    if report["traceback"]:
        print("[repro] ---- traceback ----")
        print(report["traceback"])
    if logs:
        print("[repro] ---- 下载日志（末尾 8 条）----")
        for line in logs[-8:]:
            print("   " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
