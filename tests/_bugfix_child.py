# -*- coding: utf-8 -*-
"""QA 独立验证的子进程载荷（可在完全无标准句柄的 pythonw.exe 下运行）。

文件名故意不以 ``test_`` 开头，避免被 ``unittest discover`` 收集。
本脚本**不向任何标准流输出**：所有结果写入调用方指定的 JSON 文件，因此
可以在 ``sys.stdin/stdout/stderr`` 全为 ``NoneType`` 的进程里安全运行。

用法::

    pythonw.exe _bugfix_child.py <mode> <out.json> [extra]

mode:
    probe            只报告六个标准流的实际类型
    guard_source     在「源码模式（有真实控制台）」下验证 guard 是纯 no-op
    download         走完整 core.download_model + 真实 SDK 链路（A/B 对照）
                     extra == "guard" 时先安装 stdio 兜底
"""
from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STREAM_NAMES = (
    "stdout", "stderr", "stdin", "__stdout__", "__stderr__", "__stdin__",
)


def _write(path, data):
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _stream_types():
    return {name: type(getattr(sys, name, None)).__name__
            for name in STREAM_NAMES}


def _stream_iface():
    out = {}
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        out[name] = {
            "type": type(stream).__name__,
            "write": callable(getattr(stream, "write", None)),
            "flush": callable(getattr(stream, "flush", None)),
            "isatty": callable(getattr(stream, "isatty", None)),
            "fileno": callable(getattr(stream, "fileno", None)),
            "encoding": getattr(stream, "encoding", None),
        }
    return out


def _load_entry_module():
    """以独立模块名加载 pack/entry.py（其模块级会安装 stdio 兜底）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "qa_entry_under_test", str(ROOT / "pack" / "entry.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mode_probe(out):
    _write(out, {
        "mode": "probe",
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "streams": _stream_types(),
        "executable": sys.executable,
    })


def mode_guard_source(out):
    """有真实控制台时，guard 必须 return False 且不触碰任何 sys 属性。"""
    before_types = _stream_types()
    before_ids = {name: id(getattr(sys, name, None)) for name in STREAM_NAMES}
    module = _load_entry_module()
    after_module_ids = {name: id(getattr(sys, name, None))
                        for name in STREAM_NAMES}
    second_return = module._install_stdio_guard()
    after_ids = {name: id(getattr(sys, name, None)) for name in STREAM_NAMES}
    _write(out, {
        "mode": "guard_source",
        "types_before": before_types,
        "streams_after": _stream_types(),
        "module_level_installed": bool(module._STDIO_GUARD_INSTALLED),
        "second_call_return": bool(second_return),
        "ids_unchanged": (before_ids == after_module_ids == after_ids),
        "iface": _stream_iface(),
    })


def mode_download(out, with_guard):
    """在真实「无标准句柄」进程里走完整下载链（真实 SDK / 真实 README.md）。"""
    result = {
        "mode": "download",
        "with_guard": bool(with_guard),
        "streams_before": _stream_types(),
        "exception": None,
        "exception_type": "",
        "traceback": "",
        "downloaded": [],
        "logs": [],
    }
    try:
        if with_guard:
            module = _load_entry_module()
            result["guard_installed"] = bool(module._STDIO_GUARD_INSTALLED)
            result["streams_after"] = _stream_types()
            result["iface"] = _stream_iface()

        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import ms_downloader_core as core

        dest = Path(tempfile.mkdtemp(prefix="qa-bugfix-ab-"))
        result["target_dir"] = str(dest)
        logs = []
        returned = core.download_model(
            model_id="Qwen/Qwen2.5-0.5B-Instruct",
            target_dir=str(dest),
            revision="master",
            allow_patterns=["README.md"],
            max_workers=1,
            log_cb=logs.append,
            apply_env=False,
        )
        result["returned"] = str(returned)
        result["logs"] = logs
        result["downloaded"] = [
            {"name": p.name, "size": p.stat().st_size}
            for p in dest.rglob("README.md") if p.is_file()
        ]
    except BaseException as exc:  # noqa: BLE001 - 载荷要把任何崩溃写进证据
        result["exception"] = "{}: {}".format(type(exc).__name__, exc)
        result["exception_type"] = type(exc).__name__
        result["traceback"] = traceback.format_exc()
    _write(out, result)


def main():
    mode = sys.argv[1]
    out = sys.argv[2]
    extra = sys.argv[3] if len(sys.argv) > 3 else ""
    try:
        if mode == "probe":
            mode_probe(out)
        elif mode == "guard_source":
            mode_guard_source(out)
        elif mode == "download":
            mode_download(out, extra == "guard")
        else:
            _write(out, {"mode": mode, "error": "unknown mode"})
    except BaseException as exc:  # noqa: BLE001
        _write(out, {"mode": mode,
                     "fatal": "{}: {}".format(type(exc).__name__, exc),
                     "traceback": traceback.format_exc()})


if __name__ == "__main__":
    main()
