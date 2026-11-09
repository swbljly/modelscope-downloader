# -*- coding: utf-8 -*-
"""诊断：为什么上一轮 ``--smoke-download`` 没暴露 Bug A。

对比同一枚 windowed exe 在**两种启动方式**下的标准流状态：

* ``popen_devnull``：``verify_exe.py`` 的做法，Popen 把 DEVNULL 句柄传给子进程。
* ``detached_no_handles``：用 Win32 ``CreateProcessW`` 且 ``bInheritHandles=False``，
  精确模拟用户「双击 exe」（资源管理器不给子进程任何标准句柄）。

探针用 exe 自带的 ``--version`` 分支：该分支执行 ``print(...)``。
若 ``sys.stdout is None``，print 会抛 ``AttributeError``，被 entry.py 兜底写入
「启动错误.log」并以退出码 1 结束；若标准流有效，则正常返回 0、不写日志。
因此「是否生成启动错误.log」就是 windowed exe 内标准流是否为 None 的可靠判据。
"""
from __future__ import annotations

import ctypes
import json
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "ModelScope下载器.exe"
CRASH_LOG = ROOT / "dist" / "启动错误.log"
RESULT = ROOT / "pack" / "probe_stdio_result.json"


class STARTUPINFO(ctypes.Structure):
    """Win32 STARTUPINFOW（只用到 dwFlags / wShowWindow / 标准句柄）。"""

    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    """Win32 PROCESS_INFORMATION。"""

    _fields_ = [
        ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
    ]


def _clear_crash() -> None:
    try:
        CRASH_LOG.unlink()
    except FileNotFoundError:
        pass


def _read_crash() -> str:
    try:
        return CRASH_LOG.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return ""


def run_popen_devnull() -> dict:
    """Popen + DEVNULL（verify_exe.py 的启动方式），返回退出码与崩溃日志。"""
    proc = subprocess.Popen(
        [str(EXE), "--version"], cwd=str(EXE.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        rc = proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = -1
    return {"returncode": rc, "crash_log": _read_crash()}


def run_detached_no_handles() -> dict:
    """CreateProcessW + bInheritHandles=False：不给子进程任何标准句柄（模拟双击）。"""
    kernel32 = ctypes.windll.kernel32
    create_process = kernel32.CreateProcessW
    create_process.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.LPVOID, wintypes.LPVOID,
        wintypes.BOOL, wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFO), ctypes.POINTER(PROCESS_INFORMATION),
    ]
    create_process.restype = wintypes.BOOL

    si = STARTUPINFO()
    si.cb = ctypes.sizeof(si)
    si.dwFlags = 0x00000001  # STARTF_USESHOWWINDOW
    si.wShowWindow = 0       # SW_HIDE
    pi = PROCESS_INFORMATION()
    cmdline = ctypes.create_unicode_buffer('"{}" --version'.format(EXE))
    flags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    ok = create_process(
        None, cmdline, None, None, False, flags, None,
        str(EXE.parent), ctypes.byref(si), ctypes.byref(pi),
    )
    if not ok:
        return {"returncode": -1, "crash_log": _read_crash(),
                "error": "CreateProcessW 失败，GetLastError={}".format(
                    kernel32.GetLastError())}
    kernel32.WaitForSingleObject(pi.hProcess, 60000)
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
    kernel32.CloseHandle(pi.hProcess)
    kernel32.CloseHandle(pi.hThread)
    return {"returncode": int(code.value), "crash_log": _read_crash()}


def main() -> int:
    """依次跑两种启动方式，落盘对比结果。"""
    if not EXE.is_file():
        print("[probe] 找不到 exe：{}".format(EXE))
        return 2
    report: dict = {"exe": str(EXE), "cases": {}}
    for label, runner in (
        ("popen_devnull", run_popen_devnull),
        ("detached_no_handles", run_detached_no_handles),
    ):
        _clear_crash()
        data = runner()
        # 给 onefile 子进程一点时间写完日志
        for _ in range(10):
            if data["crash_log"]:
                break
            time.sleep(0.3)
            data["crash_log"] = _read_crash()
        data["stdout_is_None"] = ("NoneType" in data["crash_log"]
                                  and "write" in data["crash_log"])
        report["cases"][label] = data
        print("[probe] {} -> rc={} | stdout_is_None={} | crash_log={}".format(
            label, data["returncode"], data["stdout_is_None"],
            bool(data["crash_log"])))
        if data["crash_log"]:
            print(data["crash_log"].strip()[:600])
    RESULT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print("[probe] 结果写入：{}".format(RESULT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
