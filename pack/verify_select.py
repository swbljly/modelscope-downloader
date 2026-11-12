# -*- coding: utf-8 -*-
"""BugFix 验收：在「等价用户双击」的隔离环境里实测新 exe 的「勾选即下载范围」。

用法（项目根目录；只需标准库 + Windows）：
    pack/.venv/Scripts/python.exe pack/verify_select.py

为什么必须用 Win32 ``CreateProcessW`` 而不是 ``Popen``
---------------------------------------------------
``--windowed`` 的 onefile 程序在「双击」启动时没有任何标准句柄，
``sys.stdout/stderr/stdin``（含 ``__stdout__`` 等）全部是 ``None``；这会让
tqdm / 日志库炸 ``AttributeError: 'NoneType' object has no attribute 'write'``
（详见 ``pack/verify_bugfix.py`` 的历史）。因此验收必须复刻双击：

* ``bInheritHandles=False`` + 不设 ``STARTF_USESTDHANDLES`` —— 子进程六个
  标准流全是 ``None``；
* ``DETACHED_PROCESS | CREATE_NO_WINDOW`` —— onefile 会派生子进程，由本脚本
  作为**守护父进程同步等待**报告落盘，避免父 shell 一退出就把子进程回收；
* ``CREATE_UNICODE_ENVIRONMENT`` + PATH 完全不含 Python —— 确保用的是 exe
  内置解释器，而不是本机某个 Python。

用例（``--smoke-select``）在 exe 内真实执行：
    1. 真实查询清单（Qwen/Qwen2.5-0.5B-Instruct，11 个文件 / 953 MB）；
    2. 全不选 → 必须被拦下；
    3. 全选 → 等价下载全部（allow_patterns 为空 + 日志 M/M）；
    4. 未获取清单 → 下载全部且日志写明；
    5. 只勾 README.md → 真实下载，目标文件夹里**只有**该文件。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# 复用已验证的「无标准句柄启动 + 守护父进程同步等待」实现，避免重复造轮子。
from verify_bugfix import (  # noqa: E402  (需先设置 sys.path)
    EXE,
    clean_env,
    run_case,
)

REPORT_JSON = "勾选下载自检结果.json"
REPORT_TXT = "勾选下载自检结果.txt"
TARGET_SUBDIR = "out"
WANT_FILE = "README.md"


def _md5(path: Path) -> str:
    """计算文件 md5。"""
    digest = hashlib.md5()
    with open(str(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _print_report(data, rc: int, elapsed: float) -> bool:
    """打印 exe 自检报告并返回是否全部通过。"""
    if data is None:
        print("   FAIL 未生成报告 | 退出码={} | 耗时 {:.1f}s".format(rc, elapsed))
        return False
    ok = bool(data.get("ok"))
    print("   overall_ok={} | 退出码={} | 耗时 {:.1f}s | 检查项 {} 个".format(
        ok, rc, elapsed, len(data.get("checks", []))))
    for item in data.get("checks", []):
        flag = "PASS" if item.get("ok") else "FAIL"
        detail = str(item.get("detail", "")).replace("\n", " ")[:200]
        print("   [{}] {} — {}".format(flag, item.get("name"), detail))
    return ok


def _process_image_path(pid: int) -> str:
    """按 PID 取进程的完整镜像路径（无权访问时返回空串）。"""
    kernel32 = ctypes.windll.kernel32
    process_query_limited_information = 0x1000
    handle = kernel32.OpenProcess(
        process_query_limited_information, False, int(pid))
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _residual_processes(work: Path) -> list:
    """返回仍在本轮临时目录里运行的 exe 进程（只统计我们自己启动的那份）。

    用户机器上可能本来就开着 `D:\\...\\ModelScope下载器.exe`（用户自己的
    实例），那不是本脚本的残留，不能算失败；因此按「可执行文件路径是否位于
    本轮隔离目录」精确过滤。
    """
    try:
        proc = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
        )
    except Exception:
        return []
    try:
        text = proc.stdout.decode("mbcs", errors="replace")
    except Exception:
        text = proc.stdout.decode("utf-8", errors="replace")
    prefix = str(work).lower()
    residual = []
    for line in text.splitlines():
        if "ModelScope" not in line or ".exe" not in line:
            continue
        fields = [item.strip().strip('"') for item in line.split('","')]
        pid_text = fields[1] if len(fields) > 1 else ""
        pid_text = pid_text.strip('"').strip()
        try:
            pid = int(pid_text)
        except (TypeError, ValueError):
            continue
        image = _process_image_path(pid).lower()
        if image.startswith(prefix):
            residual.append({"pid": pid, "image": image})
    return residual


def _other_running_instances(work: Path) -> list:
    """列出不属于本轮验证的其它 ModelScope下载器.exe 进程（仅作提示）。"""
    try:
        proc = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
        )
    except Exception:
        return []
    try:
        text = proc.stdout.decode("mbcs", errors="replace")
    except Exception:
        text = proc.stdout.decode("utf-8", errors="replace")
    prefix = str(work).lower()
    others = []
    for line in text.splitlines():
        if "ModelScope" not in line or ".exe" not in line:
            continue
        fields = [item.strip().strip('"') for item in line.split('","')]
        pid_text = fields[1] if len(fields) > 1 else ""
        pid_text = pid_text.strip('"').strip()
        try:
            pid = int(pid_text)
        except (TypeError, ValueError):
            continue
        image = _process_image_path(pid)
        if not image.lower().startswith(prefix):
            others.append({"pid": pid, "image": image})
    return others


def _repro_summary() -> str:
    """把修复前后的 A/B 复现证据拼成一行（文件不存在时明确说明）。"""
    lines = []
    for tag, label in (("before", "修复前"), ("after", "修复后")):
        path = HERE / "repro_select_bug_{}.json".format(tag)
        if not path.is_file():
            lines.append("{}：（缺少 {}）".format(label, path.name))
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            lines.append("{}：读取失败 {}".format(label, exc))
            continue
        lines.append("{}：allow_patterns={} | 进度分母={}".format(
            label, data.get("allow_patterns_sent_to_sdk"),
            data.get("total_bytes_sent_to_sdk")))
    return " ; ".join(lines)


def main() -> int:
    """执行端到端验收并输出总结论。"""
    if not EXE.is_file():
        print("[verify-select] 找不到产物：{}".format(EXE))
        return 2
    digest = _md5(EXE)
    size_mb = EXE.stat().st_size / 1048576
    work = Path(tempfile.mkdtemp(prefix="msdl-verify-select-"))
    exe_copy = work / EXE.name
    shutil.copy2(EXE, exe_copy)
    target = work / TARGET_SUBDIR
    env = clean_env()

    print("[verify-select] 隔离目录：{}".format(work))
    print("[verify-select] exe 体积：{:.2f} MB | md5：{}".format(size_mb, digest))
    print("[verify-select] PATH = {} （不含任何 Python）".format(env["PATH"]))
    print("[verify-select] 启动方式：CreateProcessW / 无标准句柄（等价双击）")
    print("[verify-select] 修复前后复现证据：{}".format(_repro_summary()))
    print("")

    results = []
    print("=== exe --smoke-select（真实 GUI 勾选 + 真实下载 1 个小文件）===")
    data, rc, elapsed = run_case(
        exe_copy, ["--smoke-select", str(target)], REPORT_JSON, work, env,
        timeout=600)
    results.append(_print_report(data, rc, elapsed))
    print("")

    # 独立于 exe 报告的第二道验证：直接看磁盘。
    print("=== 独立磁盘校验：目标文件夹内容 ===")
    files = []
    if target.is_dir():
        files = sorted(
            path.relative_to(target).as_posix()
            for path in target.rglob("*") if path.is_file()
        )
    disk_ok = files == [WANT_FILE]
    print("   目标目录={} | 文件列表={}".format(target, files))
    print("   [{}] 目标文件夹里只有 {}".format(
        "PASS" if disk_ok else "FAIL", WANT_FILE))
    results.append(disk_ok)
    print("")

    print("=== 残留进程检查（只统计本轮隔离目录里的 exe）===")
    residual = _residual_processes(work)
    print("   [{}] 无本轮残留进程{}".format(
        "PASS" if not residual else "FAIL",
        "" if not residual else "：" + str(residual)))
    results.append(not residual)
    others = _other_running_instances(work)
    if others:
        print("   提示：检测到其它正在运行的 ModelScope下载器.exe（非本轮验证残留，"
              "多为用户自己的旧实例或被占用）：{}".format(others))
    print("")

    final = all(results)
    print("[verify-select] FINAL_ALL_OK = {}".format(final))
    if final:
        shutil.rmtree(str(work), ignore_errors=True)
        print("[verify-select] 已清理临时目录：{}".format(work))
    else:
        print("[verify-select] 保留现场以便排查：{}".format(work))
    return 0 if final else 1


if __name__ == "__main__":
    sys.exit(main())
