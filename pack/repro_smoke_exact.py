# -*- coding: utf-8 -*-
"""严格对照：完全复刻 ``entry.smoke_download`` 的 ``download_model`` 参数。

目的：验证「上一轮 smoke-download 没崩」是否与调用参数（只下 1 个
README.md / max_workers=1 / apply_env=False / 不传 progress_cb）有关。
本脚本把六个标准流置为 ``None``（模拟用户双击 exe），然后用与
``pack/entry.py::smoke_download`` **完全一致**的参数调用
``core.download_model``。若仍然抛 ``AttributeError``，即可排除「小文件 /
单线程 / apply_env / 未传回调」这些差异，把根因锁定在启动方式的标准句柄上。

用法：
    pack/.venv/Scripts/python.exe pack/repro_smoke_exact.py
结果写入 ``pack/repro_smoke_exact_result.json``。只下载几 KB 的 README.md。
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
RESULT = ROOT / "pack" / "repro_smoke_exact_result.json"


def main() -> int:
    """执行严格参数对照并落盘结果。"""
    real_stdout, real_stderr, real_stdin = sys.stdout, sys.stderr, sys.stdin
    logs: list = []
    report: dict = {
        "scenario": "六个标准流置为 None（模拟 windowed 双击启动）",
        "params": ("与 entry.smoke_download 完全一致："
                   "allow_patterns=['README.md'], max_workers=1, "
                   "apply_env=False, 不传 progress_cb / cancel_event / "
                   "progress_interval / endpoint / proxy"),
        "python": sys.version.split()[0],
    }

    sys.stdout = None
    sys.stderr = None
    sys.stdin = None
    sys.__stdout__ = None
    sys.__stderr__ = None
    sys.__stdin__ = None

    try:
        import ms_downloader_core as core

        dest = Path(tempfile.mkdtemp(prefix="msdl-smoke-exact-"))
        report["target_dir"] = str(dest)
        returned = core.download_model(
            model_id="Qwen/Qwen2.5-0.5B-Instruct",
            target_dir=str(dest),
            revision="master",
            allow_patterns=["README.md"],
            max_workers=1,
            log_cb=logs.append,
            apply_env=False,
        )
        report["verdict"] = "NO_CRASH（参数差异才是主因）"
        report["exception"] = None
        report["traceback"] = ""
        report["returned"] = str(returned)
        hits = [p for p in dest.rglob("README.md") if p.is_file()]
        report["downloaded_files"] = [
            {"path": str(p), "size": p.stat().st_size} for p in hits]
    except BaseException as exc:  # noqa: BLE001 - 对照实验要捕获一切
        report["verdict"] = "REPRODUCED_WITH_SMOKE_PARAMS（与参数无关）"
        report["exception"] = "{}: {}".format(type(exc).__name__, exc)
        report["traceback"] = traceback.format_exc()
        report["returned"] = ""
        report["downloaded_files"] = []
    report["logs"] = logs

    sys.stdout, sys.stderr, sys.stdin = real_stdout, real_stderr, real_stdin
    RESULT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print("[smoke-exact] 结果写入：{}".format(RESULT))
    print("[smoke-exact] verdict = {} | exception = {}".format(
        report["verdict"], report["exception"]))
    if report["traceback"]:
        print("[smoke-exact] ---- 关键 traceback 行 ----")
        for line in report["traceback"].splitlines():
            if ("tqdm" in line or "modelscope" in line
                    or "ms_downloader_core" in line
                    or "AttributeError" in line):
                print("   " + line.strip())
    print("[smoke-exact] 日志：{}".format(" | ".join(logs[-4:])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
