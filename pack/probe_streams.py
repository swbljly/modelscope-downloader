# -*- coding: utf-8 -*-
"""诊断：GUI 子系统进程在「有无继承标准句柄」两种启动方式下的标准流状态。

用 venv 里的 ``pythonw.exe``（Windows GUI 子系统，与 PyInstaller --windowed
同一类进程）运行一个只写 JSON 的子脚本，直接报告 ``sys.stdout/stderr/stdin``
与 ``sys.__stdout__/__stderr__/__stdin__`` 是否为 ``None``。

这解释了上一轮 ``--smoke-download`` 为何 3/3 PASS：
``verify_exe.py`` 用 ``Popen(stdout=DEVNULL, stderr=DEVNULL)`` 给子进程传了
有效标准句柄；而用户双击 exe 时资源管理器不给任何句柄。
"""
from __future__ import annotations

import ctypes
import json
import subprocess
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYW = ROOT / "pack" / ".venv" / "Scripts" / "pythonw.exe"
RESULT = ROOT / "pack" / "probe_streams_result.json"

CHILD_CODE = '''# -*- coding: utf-8 -*-
import json, sys
from pathlib import Path
data = {
    "stdout_none": sys.stdout is None,
    "stderr_none": sys.stderr is None,
    "stdin_none": sys.stdin is None,
    "stdout_type": type(sys.stdout).__name__,
    "stderr_type": type(sys.stderr).__name__,
    "stdin_type": type(sys.stdin).__name__,
    "dunder_stdout_none": getattr(sys, "__stdout__", None) is None,
    "dunder_stderr_none": getattr(sys, "__stderr__", None) is None,
    "dunder_stdin_none": getattr(sys, "__stdin__", None) is None,
}
Path(sys.argv[1]).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
'''


class STARTUPINFO(ctypes.Structure):
    """Win32 STARTUPINFOW。"""

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


def _wait_for(out: Path, seconds: float = 20.0) -> dict:
    """等待子进程把 JSON 报告写完。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if out.is_file():
            try:
                return json.loads(out.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(0.2)
    return {"error": "子进程未写出报告"}


def run_popen_devnull(child: Path, out: Path) -> dict:
    """Popen + DEVNULL：verify_exe.py 的启动方式。"""
    proc = subprocess.Popen(
        [str(PYW), str(child), str(out)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(child.parent),
    )
    try:
        rc = proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = -1
    data = _wait_for(out)
    data["returncode"] = rc
    return data


def run_detached_no_handles(child: Path, out: Path) -> dict:
    """CreateProcessW + bInheritHandles=False：模拟用户双击（无任何标准句柄）。"""
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
    cmdline = ctypes.create_unicode_buffer(
        '"{}" "{}" "{}"'.format(PYW, child, out))
    flags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    ok = create_process(
        None, cmdline, None, None, False, flags, None,
        str(child.parent), ctypes.byref(si), ctypes.byref(pi),
    )
    if not ok:
        return {"error": "CreateProcessW 失败，GetLastError={}".format(
            kernel32.GetLastError())}
    kernel32.WaitForSingleObject(pi.hProcess, 20000)
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
    kernel32.CloseHandle(pi.hProcess)
    kernel32.CloseHandle(pi.hThread)
    data = _wait_for(out)
    data["returncode"] = int(code.value)
    return data


def main() -> int:
    """跑两种启动方式并落盘对比结果。"""
    if not PYW.is_file():
        print("[streams] 找不到 {}".format(PYW))
        return 2
    work = Path(tempfile.mkdtemp(prefix="msdl-streams-"))
    child = work / "_child.py"
    child.write_text(CHILD_CODE, encoding="utf-8")
    report: dict = {"pythonw": str(PYW), "cases": {}}
    for label, runner in (
        ("popen_devnull", run_popen_devnull),
        ("detached_no_handles", run_detached_no_handles),
    ):
        out = work / (label + ".json")
        data = runner(child, out)
        report["cases"][label] = data
        print("[streams] {} -> {}".format(label, json.dumps(
            data, ensure_ascii=False, sort_keys=True)))
    RESULT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print("[streams] 结果写入：{}".format(RESULT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
