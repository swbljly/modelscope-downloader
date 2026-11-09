# -*- coding: utf-8 -*-
"""BugFix 验收：在「等价用户双击」的隔离环境里实测新 exe。

用法（项目根目录，用任意 Python 3 即可，只用标准库）：
    pack/.venv/Scripts/python.exe pack/verify_bugfix.py

与旧 ``verify_exe.py`` 的关键差别
--------------------------------
旧脚本用 ``subprocess.Popen(stdout=DEVNULL, stderr=DEVNULL)`` 启动 exe，
**给子进程传了有效的标准句柄**，windowed 进程里 ``sys.stderr`` 是
TextIOWrapper，因此永远触发不了 Bug A（这也是上一轮 smoke-download 3/3
PASS 却仍被用户双击崩掉的根本原因，见 ``pack/probe_streams.py``）。

本脚本改用 Win32 ``CreateProcessW``：
* ``bInheritHandles=False`` + 不设 ``STARTF_USESTDHANDLES`` —— 子进程拿不到
  任何标准句柄，CPython 会把六个标准流全部初始化为 None，与资源管理器双击
  完全一致；
* ``CREATE_UNICODE_ENVIRONMENT`` 传入 PATH 完全不含 Python 的干净环境；
* ``DETACHED_PROCESS | CREATE_NO_WINDOW``，由本脚本作为守护父进程同步等待
  onefile 子进程写完报告，避免父 shell 退出回收残留子进程。

实测四个用例：
    1. ``--self-check``        运行时自检（含 windowed 标准流兜底项）
    2. ``--smoke-progress``    启用 SDK 进度回调 + 真实下载 README.md
    3. ``--smoke-cancel``      停止/取消控制流（可控 fake SDK，不联网）
    4. 无参数 GUI              双击启动后持续运行 8 秒不崩溃、无错误日志
另跑一个「诊断对照」用例：用旧脚本的 Popen(DEVNULL) 方式跑自检，展示两种
启动方式下标准流状态的差异（不计入 PASS 判定）。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "ModelScope下载器.exe"

DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESHOWWINDOW = 0x00000001
WAIT_OBJECT_0 = 0x00000000


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


def clean_env() -> dict:
    """构造 PATH 完全不含 Python 的运行环境（并清掉 Python 相关变量）。"""
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV",
                "PYTHONEXECUTABLE", "PYTHONSTARTUP"):
        env.pop(key, None)
    env["PATH"] = r"C:\Windows\System32;C:\Windows"
    return env


def _env_block(env: dict):
    """把 dict 转成 CreateProcessW 需要的 Unicode 环境块（双 NUL 结尾）。"""
    items = sorted(
        ((str(k), str(v)) for k, v in env.items() if not str(k).startswith("=")),
        key=lambda pair: pair[0].upper(),
    )
    text = "".join("{}={}\0".format(k, v) for k, v in items) + "\0"
    return ctypes.create_unicode_buffer(text, len(text) + 1)


def launch_isolated(exe: Path, args: list, workdir: Path, env: dict):
    """以「无标准句柄 + 干净环境」启动 exe，返回 PROCESS_INFORMATION。

    这是本脚本的核心：``bInheritHandles=False`` 且不给 STARTF_USESTDHANDLES，
    子进程的标准流会全部是 None，与用户双击 exe 完全一致。
    """
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
    si.dwFlags = STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    pi = PROCESS_INFORMATION()
    env_buf = _env_block(env)
    cmdline = ctypes.create_unicode_buffer(
        '"{}"{}'.format(exe, "".join(" " + str(a) for a in args)))
    flags = DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT
    ok = create_process(
        None, cmdline, None, None, False, flags,
        ctypes.cast(env_buf, ctypes.c_void_p),
        str(workdir), ctypes.byref(si), ctypes.byref(pi),
    )
    if not ok:
        raise RuntimeError("CreateProcessW 失败，GetLastError={}".format(
            kernel32.GetLastError()))
    return pi


def _wait(kernel32, handle, millis: int) -> int:
    """封装 WaitForSingleObject，返回原始状态码。"""
    return kernel32.WaitForSingleObject(handle, millis)


def _read_report(path: Path):
    """读取 JSON 报告；尚未写完时返回 None。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def run_case(exe: Path, args: list, report: str, workdir: Path,
             env: dict, timeout: int = 300):
    """启动一个用例并等待报告落盘，返回 (报告 dict|None, 退出码, 耗时秒)。"""
    rp = workdir / report
    for path in (rp, workdir / report.replace(".json", ".txt")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    started = time.time()
    kernel32 = ctypes.windll.kernel32
    pi = launch_isolated(exe, args, workdir, env)
    data = None
    rc = -1
    try:
        while time.time() - started < timeout:
            if rp.is_file():
                data = _read_report(rp)
                if data is not None:
                    break
            if _wait(kernel32, pi.hProcess, 0) == WAIT_OBJECT_0:
                # 进程已退出但报告可能还在落盘，给 6 秒宽限
                for _ in range(12):
                    if rp.is_file():
                        data = _read_report(rp)
                        if data is not None:
                            break
                    time.sleep(0.5)
                break
            time.sleep(0.4)
        if _wait(kernel32, pi.hProcess, 20000) != WAIT_OBJECT_0:
            kernel32.TerminateProcess(pi.hProcess, 1)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        rc = int(code.value)
    finally:
        kernel32.CloseHandle(pi.hProcess)
        kernel32.CloseHandle(pi.hThread)
    return data, rc, time.time() - started


def run_gui_case(exe: Path, workdir: Path, env: dict, seconds: int = 8):
    """无参数启动 GUI（真实双击场景），观察 seconds 秒内是否存活且无错误日志。"""
    crash = workdir / "启动错误.log"
    try:
        crash.unlink()
    except FileNotFoundError:
        pass
    kernel32 = ctypes.windll.kernel32
    pi = launch_isolated(exe, [], workdir, env)
    time.sleep(seconds)
    alive = _wait(kernel32, pi.hProcess, 0) != WAIT_OBJECT_0
    crash_text = ""
    if crash.is_file():
        crash_text = crash.read_text(encoding="utf-8", errors="replace")
    if alive:
        # onefile 会派生子进程，/T 结束整棵进程树（父脚本环境 PATH 完整）。
        subprocess.run(
            ["taskkill", "/PID", str(int(pi.dwProcessId)), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    kernel32.CloseHandle(pi.hProcess)
    kernel32.CloseHandle(pi.hThread)
    return alive, crash_text


def run_control_case(exe: Path, workdir: Path, env: dict) -> str:
    """诊断对照：用旧 verify_exe.py 的 Popen(DEVNULL) 方式跑一次自检。

    只用于展示「两种启动方式下标准流状态不同」，不计入验收结论。
    """
    workdir.mkdir(parents=True, exist_ok=True)
    copied = workdir / exe.name
    if not copied.is_file():
        shutil.copy2(exe, copied)
    rp = workdir / "自检结果.json"
    try:
        rp.unlink()
    except FileNotFoundError:
        pass
    proc = subprocess.Popen(
        [str(copied), "--self-check"], cwd=str(workdir), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    data = None
    deadline = time.time() + 120
    while time.time() < deadline:
        if rp.is_file():
            data = _read_report(rp)
            if data is not None:
                break
        if proc.poll() is not None:
            time.sleep(0.3)
            if rp.is_file():
                data = _read_report(rp)
            break
        time.sleep(0.4)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
    if data is None:
        return "（对照用例未生成报告）"
    for item in data.get("checks", []):
        if "标准流" in str(item.get("name", "")):
            return str(item.get("detail", ""))
    return "（自检报告中没有标准流项）"


def print_report(label: str, data, rc: int, elapsed: float,
                 all_ok: list) -> None:
    """打印单个用例的逐项结果。"""
    print("=== {} ===".format(label))
    if data is None:
        print("   FAIL 未生成报告 | 退出码={} | 耗时 {:.1f}s".format(rc, elapsed))
        all_ok.append(False)
        print("")
        return
    ok = bool(data.get("ok"))
    print("   overall_ok={} | 退出码={} | 耗时 {:.1f}s | 用例 {} 项".format(
        ok, rc, elapsed, len(data.get("checks", []))))
    for item in data.get("checks", []):
        flag = "PASS" if item.get("ok") else "FAIL"
        detail = str(item.get("detail", "")).replace("\n", " ")[:190]
        print("   [{}] {} — {}".format(flag, item.get("name"), detail))
    all_ok.append(ok)
    print("")


def main() -> int:
    """执行全部验收用例并输出总结论。"""
    if not EXE.is_file():
        print("[verify-bugfix] 找不到产物：{}".format(EXE))
        return 2
    digest = hashlib.md5(EXE.read_bytes()).hexdigest()
    work = Path(tempfile.mkdtemp(prefix="msdl-bugfix-"))
    exe_copy = work / EXE.name
    shutil.copy2(EXE, exe_copy)
    env = clean_env()

    print("[verify-bugfix] 隔离目录：{}".format(work))
    print("[verify-bugfix] exe 体积：{:.2f} MB | md5：{}".format(
        EXE.stat().st_size / 1048576, digest))
    print("[verify-bugfix] PATH = {} （不含任何 Python）".format(env["PATH"]))
    print("[verify-bugfix] 启动方式：CreateProcessW / 无标准句柄（等价双击，"
          "六个标准流均为 None）")
    print("")

    cases = [
        ("运行时自检（8 项，含 windowed 标准流兜底）",
         ["--self-check"], "自检结果.json"),
        ("启用 SDK 进度回调 + 真实下载 README.md",
         ["--smoke-progress"], "进度回调自检结果.json"),
        ("停止/取消控制流（可控 fake SDK）",
         ["--smoke-cancel"], "取消自检结果.json"),
    ]
    results: list = []
    for label, args, report in cases:
        data, rc, elapsed = run_case(exe_copy, args, report, work, env)
        print_report(label, data, rc, elapsed, results)

    print("=== 无参数 GUI 双击启动（持续 8 秒）===")
    alive, crash_text = run_gui_case(exe_copy, work, env, seconds=8)
    gui_ok = alive and not crash_text
    print("   进程存活={} | 启动错误.log={}".format(
        alive, "无" if not crash_text else "存在"))
    if crash_text:
        print(crash_text[:800])
    print("   [{}] GUI 启动后未崩溃".format("PASS" if gui_ok else "FAIL"))
    results.append(gui_ok)
    print("")

    crash = work / "启动错误.log"
    print("隔离目录启动错误日志：{}".format(
        "无" if not crash.is_file() else
        crash.read_text(encoding="utf-8", errors="replace")[:800]))

    # 诊断对照：解释上一轮 smoke-download 为何没暴露 Bug A
    control = work / "control"
    print("")
    print("=== 诊断对照（不计入 PASS）：旧 verify_exe.py 的 Popen(DEVNULL) 方式 ===")
    print("   " + run_control_case(EXE, control, env))
    print("   说明：该方式给子进程传了有效标准句柄，sys.stderr 是 TextIOWrapper，")
    print("         因此永远触发不了双击时的 None.write 崩溃。")
    print("")

    final = all(results)
    print("[verify-bugfix] 报告目录：{}".format(work))
    print("[verify-bugfix] FINAL_ALL_OK = {}".format(final))
    return 0 if final else 1


if __name__ == "__main__":
    sys.exit(main())
