# -*- coding: utf-8 -*-
"""验证单文件 exe 是否真的自包含、能否完成真实下载。

用法：
    python pack/verify_exe.py            # 用系统 Python 即可，只用标准库

做法：
1. 把 exe 复制到一个全新临时目录，模拟「用户只拿到一个 exe」。
2. 构造一个 **PATH 里没有任何 Python**、且清掉 PYTHONHOME/PYTHONPATH 的环境。
3. 由本脚本作为守护父进程同步启动 exe（onefile + windowed 从 shell 直接启动会
   立即交还控制权，若父 shell 随后退出，尚未写完报告的子进程会被一并清理）。
4. 依次跑 --self-check 与 --smoke-download，等待各自的 JSON 报告落盘并解析。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "ModelScope下载器.exe"


def clean_env() -> dict:
    """故意构造一个「这台电脑没装 Python」的运行环境。"""
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV",
                "PYTHONEXECUTABLE", "PYTHONSTARTUP"):
        env.pop(key, None)
    env["PATH"] = r"C:\Windows\System32;C:\Windows"
    return env


def run_case(exe: Path, args: list, report: str, workdir: Path,
             env: dict, timeout: int = 300):
    """启动 exe 并等待报告落盘，返回 (报告 dict 或 None, 退出码, 耗时秒)。"""
    rp = workdir / report
    txt = workdir / report.replace(".json", ".txt")
    for path in (rp, txt):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    started = time.time()
    proc = subprocess.Popen(
        [str(exe)] + args, cwd=str(workdir), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    def read_report():
        try:
            return json.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            return None

    while time.time() - started < timeout:
        if rp.is_file():
            data = read_report()
            if data is not None:
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return data, proc.returncode, time.time() - started
        if proc.poll() is not None:
            # 进程已退出但报告可能还在落盘，给 5 秒宽限
            for _ in range(10):
                if rp.is_file():
                    data = read_report()
                    if data is not None:
                        return data, proc.returncode, time.time() - started
                time.sleep(0.5)
            return read_report(), proc.returncode, time.time() - started
        time.sleep(0.5)

    if proc.poll() is None:
        proc.kill()
    return None, proc.returncode, time.time() - started


def main() -> int:
    if not EXE.is_file():
        print("[verify] 找不到产物：{}".format(EXE))
        return 2

    work = Path(tempfile.mkdtemp(prefix="msdl-verify-"))
    exe_copy = work / EXE.name
    shutil.copy2(EXE, exe_copy)
    env = clean_env()
    print("[verify] 隔离目录：{}".format(work))
    print("[verify] exe 体积：{:.1f} MB".format(EXE.stat().st_size / 1048576))
    print("[verify] PATH = {} （不含任何 Python）".format(env["PATH"]))
    print("")

    cases = [
        ("运行时自检", ["--self-check"], "自检结果.json"),
        ("真实联网下载", ["--smoke-download"], "下载自检结果.json"),
    ]
    all_ok = True
    for label, args, report in cases:
        data, rc, elapsed = run_case(exe_copy, args, report, work, env)
        print("=== {} ===".format(label))
        if data is None:
            print("   FAIL 未生成报告 | 退出码={} | 耗时 {:.1f}s".format(rc, elapsed))
            all_ok = False
            continue
        print("   overall_ok={} | 退出码={} | 耗时 {:.1f}s | 用例 {} 项".format(
            data.get("ok"), rc, elapsed, len(data.get("checks", []))))
        for item in data.get("checks", []):
            flag = "PASS" if item.get("ok") else "FAIL"
            detail = str(item.get("detail", "")).replace("\n", " ")[:150]
            print("   [{}] {} — {}".format(flag, item.get("name"), detail))
        if not data.get("ok"):
            all_ok = False
        print("")

    crash = work / "启动错误.log"
    print("启动错误日志：{}".format(
        "存在（见下）\n" + crash.read_text(encoding="utf-8", errors="replace")
        if crash.is_file() else "无"))
    print("[verify] 报告目录：{}".format(work))
    print("[verify] FINAL_ALL_OK = {}".format(all_ok))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
