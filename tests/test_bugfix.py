# -*- coding: utf-8 -*-
"""BugFix 快捷路径 · QA 独立回归验证（严过关 / Yan）。

与工程师自测脚本 ``pack/verify_bugfix.py`` **不共用任何实现**：本文件用
``ctypes`` 直接调用 ``CreateProcessW(bInheritHandles=False)`` 制造「完全没有
标准句柄」的子进程（等价资源管理器双击），并与 ``Popen(stdout=DEVNULL)``
做 A/B 对照；所有真实下载只允许拉 ``README.md``（约 5 KB），目标目录一律
落在 ``tempfile.TemporaryDirectory``，绝不写入用户 OneDrive，绝不污染 dist。

分组：
    A 快照与静态          TestSnapshotAndStatic
    B Bug A 独立复验      TestStdioDifference / TestExeNoHandles / TestABControl
    C Bug B 独立复验      TestCancelLogic / TestGuiStopPaths
    D 既有回归            （由 discover 一并执行 test_core.py / test_gui.py）
    E 交付一致性          TestDeliveryConsistency
"""
from __future__ import annotations

import ast
import atexit
import ctypes
import hashlib
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CHILD = ROOT / "tests" / "_bugfix_child.py"
VENV_PY = ROOT / "pack" / ".venv" / "Scripts" / "python.exe"
VENV_PYTHONW = ROOT / "pack" / ".venv" / "Scripts" / "pythonw.exe"
EXE = ROOT / "dist" / "ModelScope下载器.exe"
IS_WIN = os.name == "nt"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ms_downloader_core as core  # noqa: E402

try:
    import tkinter as tk  # noqa: F401
    HAS_TK = True
except Exception:  # 便携版 3.13 无 tkinter
    HAS_TK = False
    tk = None

if HAS_TK:
    import gui_app  # noqa: E402
else:
    gui_app = None


# ---------------------------------------------------------------------------
# 无标准句柄进程启动器（不经过 shell，父进程同步等待）
# ---------------------------------------------------------------------------

def _quote(text) -> str:
    return '"{}"'.format(str(text).replace('"', '\\"'))


def _spawn_no_handles(application, args, cwd, timeout_ms=240000):
    """CreateProcessW：bInheritHandles=False + STARTUPINFO 不设标准句柄。

    这是本次质量事故的核心：不给子进程任何 stdin/stdout/stderr 句柄，
    CPython 在 windowed 子系统下会把六个 ``sys.*`` 流全部初始化为 ``None``。
    父进程用 ``WaitForSingleObject`` **同步等待**，不会提前退出回收子进程
    （onefile windowed 从 shell 启动会立即交还控制权，是上一轮踩过的坑）。
    """
    if not IS_WIN:
        return {"returncode": None, "timed_out": False,
                "error": "CreateProcessW 需要 Windows"}
    from ctypes import wintypes

    class STARTUPINFO(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", ctypes.c_void_p),
            ("lpDesktop", ctypes.c_void_p),
            ("lpTitle", ctypes.c_void_p),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", ctypes.c_void_p),
            ("hStdOutput", ctypes.c_void_p),
            ("hStdError", ctypes.c_void_p),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", ctypes.c_void_p),
            ("hThread", ctypes.c_void_p),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = k32.CreateProcessW
    create.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFO), ctypes.POINTER(PROCESS_INFORMATION),
    ]
    create.restype = wintypes.BOOL
    wait = k32.WaitForSingleObject
    wait.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    wait.restype = wintypes.DWORD
    getcode = k32.GetExitCodeProcess
    getcode.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    getcode.restype = wintypes.BOOL
    close = k32.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    terminate = k32.TerminateProcess
    terminate.argtypes = [ctypes.c_void_p, wintypes.UINT]

    cmdline = " ".join([_quote(application)] + [_quote(a) for a in args])
    buf = ctypes.create_unicode_buffer(cmdline)
    si = STARTUPINFO()
    si.cb = ctypes.sizeof(si)
    si.dwFlags = 0  # 关键：不设 STARTF_USESTDHANDLES
    pi = PROCESS_INFORMATION()
    ok = create(str(application), buf, None, None, False, 0, None,
                str(cwd), ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        return {"returncode": None, "timed_out": False,
                "error": "CreateProcessW 失败 err={}".format(ctypes.get_last_error())}
    WAIT_OBJECT_0, WAIT_TIMEOUT = 0, 0x102
    _record_spawn(pi.dwProcessId, application)  # 登记 PID：残留判据之一
    rc = wait(pi.hProcess, timeout_ms)
    timed_out = rc == WAIT_TIMEOUT
    if timed_out:
        terminate(pi.hProcess, 1)
        wait(pi.hProcess, 5000)
    code = wintypes.DWORD()
    getcode(pi.hProcess, ctypes.byref(code))
    close(pi.hThread)
    close(pi.hProcess)
    return {"returncode": code.value, "timed_out": timed_out, "error": ""}


def _read_json(path):
    p = Path(path)
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 一次性「重活」：所有子进程只跑一遍，结果缓存给各测试类共享
# ---------------------------------------------------------------------------
_HEAVY = None


def _ensure_heavy():
    global _HEAVY
    if _HEAVY is not None:
        return _HEAVY
    res = {"errors": {}, "processes": []}
    tmp_obj = tempfile.TemporaryDirectory(prefix="qa-bugfix-")
    tmp = Path(tmp_obj.name)
    tmp_obj_ref = tmp_obj
    atexit.register(tmp_obj_ref.cleanup)
    res["tmp"] = tmp
    res["tmp_isolation_ok"] = True

    def step(key, fn):
        try:
            res[key] = fn()
        except Exception as exc:  # noqa: BLE001 - 单项失败不阻塞其余验证
            res["errors"][key] = "{}: {}\n{}".format(
                type(exc).__name__, exc, traceback.format_exc(limit=5))
            res[key] = None

    # ---------- 1. 两种启动方式的标准流差异（独立实测） ----------
    def probe_devnull():
        out = tmp / "probe_devnull.json"
        proc = subprocess.Popen(
            [str(VENV_PYTHONW), str(CHILD), "probe", str(out)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, cwd=str(tmp))
        proc.wait(timeout=90)
        return {"returncode": proc.returncode, "data": _read_json(out)}

    def probe_nohandles():
        out = tmp / "probe_nohandles.json"
        spawn = _spawn_no_handles(
            str(VENV_PYTHONW), [str(CHILD), "probe", str(out)], tmp, 90000)
        spawn["data"] = _read_json(out)
        res["processes"].append(("probe_nohandles", spawn))
        return spawn

    step("probe_devnull", probe_devnull)
    step("probe_nohandles", probe_nohandles)

    # ---------- 2. A/B 对照：同一链路、真实无句柄进程 ----------
    def ab_noguard():
        out = tmp / "ab_noguard.json"
        spawn = _spawn_no_handles(
            str(VENV_PYTHONW), [str(CHILD), "download", str(out)],
            tmp, 240000)
        spawn["data"] = _read_json(out)
        res["processes"].append(("ab_noguard", spawn))
        return spawn

    def ab_guard():
        out = tmp / "ab_guard.json"
        spawn = _spawn_no_handles(
            str(VENV_PYTHONW),
            [str(CHILD), "download", str(out), "guard"], tmp, 240000)
        spawn["data"] = _read_json(out)
        res["processes"].append(("ab_guard", spawn))
        return spawn

    step("ab_noguard", ab_noguard)
    step("ab_guard", ab_guard)

    # ---------- 3. 复制 exe 到隔离目录后驱动（dist 零污染） ----------
    def prepare_exe():
        target = tmp / EXE.name
        shutil.copy2(EXE, target)
        return target

    res["dist_files_before_drive"] = sorted(
        p.name for p in (ROOT / "dist").iterdir())
    step("exe_copy", prepare_exe)
    exe = res.get("exe_copy")
    cached_cancel_dirs = set(Path(tempfile.gettempdir()).glob("msdl-cancel-*"))

    def drive(key, arg_list, report_name, timeout_ms):
        if exe is None or not Path(exe).is_file():
            return {"error": "exe 副本不可用"}
        spawn = _spawn_no_handles(str(exe), arg_list, tmp, timeout_ms)
        res["processes"].append((key, spawn))
        report = _read_json(tmp / report_name) if report_name else None
        spawn["report"] = report
        spawn["crash_log_exists"] = (tmp / "启动错误.log").is_file()
        spawn["crash_log"] = (
            (tmp / "启动错误.log").read_text(encoding="utf-8", errors="replace")
            if spawn["crash_log_exists"] else "")
        return spawn

    progress_dest = tmp / "progress_dest"
    step("exe_self_check", lambda: drive(
        "self_check", ["--self-check"], "自检结果.json", 180000))
    step("exe_smoke_progress", lambda: drive(
        "smoke_progress",
        ["--smoke-progress", str(progress_dest)],
        "进度回调自检结果.json", 300000))
    step("exe_smoke_cancel", lambda: drive(
        "smoke_cancel", ["--smoke-cancel"], "取消自检结果.json", 180000))
    step("exe_version", lambda: drive(
        "version", ["--version"], "", 120000))

    res["progress_dest"] = progress_dest
    # 清理 --smoke-cancel 在系统 temp 下留下的空目录
    def cleanup_cancel_dirs():
        for p in set(Path(tempfile.gettempdir()).glob("msdl-cancel-*")) - cached_cancel_dirs:
            try:
                shutil.rmtree(p, ignore_errors=True)
            except Exception:
                pass
    atexit.register(cleanup_cancel_dirs)
    _HEAVY = res
    return res


def _heavy():
    data = _ensure_heavy()
    if data.get("errors"):
        # 单项异常不阻塞其余用例，仅记录
        pass
    return data


def _skip_if_no_heavy_toolchain(case):
    if not IS_WIN:
        case.skipTest("CreateProcessW 仅 Windows 可用")
    if not VENV_PYTHONW.is_file() or not EXE.is_file():
        case.skipTest("缺少 venv pythonw.exe 或 dist exe，跳过真实进程验证")


# ===========================================================================
# A 组：快照与静态
# ===========================================================================
class TestSnapshotAndStatic(unittest.TestCase):
    """核对磁盘实物、语法、依赖方向、bat 编码。"""

    # 2026-11-11 14:5x QA（严过关）独立回归基线，对应「文件清单勾选未生效」修复：
    # core / gui / entry.py / exe / README / 使用说明 全部变更并重新打包，
    # 新增 pack/verify_select.py 与 pack/repro_select_bug.py 两个交付脚本。
    # 实测值一律来自磁盘实物（md5sum / wc -l / stat），不是抄工程师的声明；
    # 交付文档若再次更新，请同步刷新此表（这正是快照一致性检查的目的）。
    EXPECTED_MD5 = {
        "ms_downloader_core.py": "06092430e1f8a1372ae0821f792f66d5",
        "gui_app.py": "b3c4131338105f69943b14bf7733bed3",
        "pack/entry.py": "294071d8709e4d61646fb0299f257778",
        "dist/ModelScope下载器.exe": "8295e555c92cce8a5becdd25529c5e37",
        "README.md": "ea135822f2c270aacf4ba41b5e172c6a",
        "dist/使用说明.txt": "a2c225e705cdc1fb64673f6a6208df5c",
        "pack/verify_select.py": "efbc0c4a2f753a765826e9c7598aa69c",
        "pack/repro_select_bug.py": "3dddd3599d47ff94c85e55e6ec86280f",
    }
    EXPECTED_LINES = {
        "ms_downloader_core.py": 2033,
        "gui_app.py": 1037,
        "pack/entry.py": 880,
        "README.md": 259,
        "dist/使用说明.txt": 160,
        "pack/verify_select.py": 252,
        "pack/repro_select_bug.py": 178,
    }

    @staticmethod
    def _md5(path):
        h = hashlib.md5()
        with open(path, "rb") as fp:
            for chunk in iter(lambda: fp.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def test_a01_md5_and_line_counts(self):
        for rel, expected in self.EXPECTED_MD5.items():
            with self.subTest(file=rel):
                path = ROOT / rel
                self.assertTrue(path.is_file(), "文件不存在：{}".format(rel))
                self.assertEqual(self._md5(path), expected, "md5 不一致：{}".format(rel))
        for rel, expected in self.EXPECTED_LINES.items():
            with self.subTest(file=rel, kind="lines"):
                text = (ROOT / rel).read_text(encoding="utf-8")
                self.assertEqual(len(text.splitlines()), expected,
                                 "行数不一致：{}".format(rel))

    def test_a02_dist_contains_only_two_files(self):
        names = sorted(p.name for p in (ROOT / "dist").iterdir())
        self.assertEqual(names, sorted(["ModelScope下载器.exe", "使用说明.txt"]),
                         "dist/ 出现多余文件：{}".format(names))

    def test_a03_exe_size_is_reasonable(self):
        size = EXE.stat().st_size
        self.assertEqual(size, 34288975)
        self.assertTrue(20 * 1024 * 1024 < size < 60 * 1024 * 1024)

    def test_a04_all_python_and_spec_compile(self):
        # 显式枚举交付物目录，避免遍历 .venv / build 下成千上万第三方文件
        targets = (sorted(ROOT.glob("*.py"))
                   + sorted((ROOT / "pack").glob("*.py"))
                   + sorted((ROOT / "tests").glob("*.py"))
                   + [ROOT / "pack" / "downloader.spec"])
        self.assertGreater(len(targets), 8)
        tmp = Path(tempfile.mkdtemp(prefix="qa-pycompile-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        cfile = tmp / "out.pyc"
        for path in targets:
            with self.subTest(file=str(path.relative_to(ROOT))):
                py_compile.compile(str(path), cfile=str(cfile), doraise=True)

    def test_a05_core_has_no_top_level_tkinter_or_modelscope(self):
        tree = ast.parse((ROOT / "ms_downloader_core.py").read_text(encoding="utf-8"))
        top_modules = set()
        for node in tree.body:  # 只看顶层，函数内的惰性导入不算
            if isinstance(node, ast.Import):
                top_modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_modules.add(node.module.split(".")[0])
        self.assertNotIn("tkinter", top_modules)
        self.assertNotIn("modelscope", top_modules)
        self.assertNotIn("modelscope_hub", top_modules)

    def test_a06_gui_referenced_core_members_all_exist(self):
        tree = ast.parse((ROOT / "gui_app.py").read_text(encoding="utf-8"))
        used = {n.attr for n in ast.walk(tree)
                if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id == "core"}
        self.assertGreater(len(used), 10, "未解析到 core.* 引用")
        missing = sorted(name for name in used if not hasattr(core, name))
        self.assertEqual(missing, [],
                         "gui_app 引用了不存在的 core 成员：{}".format(missing))

    def test_a07_bat_files_are_ascii_crlf(self):
        for name in ("启动.bat", "安装依赖.bat"):
            with self.subTest(file=name):
                raw = (ROOT / name).read_bytes()
                self.assertTrue(all(b < 128 for b in raw),
                                "{} 含非 ASCII 字节".format(name))
                self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"),
                                 "{} 存在裸 LF，非纯 CRLF".format(name))
                self.assertGreater(raw.count(b"\r\n"), 10)

    def test_a08_readme_referenced_paths_exist(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        refs = sorted(set(re.findall(
            r"`((?:pack|dist|tests|docs)/[^`\s]+)`", text)))
        self.assertGreater(len(refs), 5)
        missing = [r for r in refs if not (ROOT / r).exists()]
        self.assertEqual(missing, [],
                         "README 引用了不存在的文件：{}".format(missing))

    def test_a09_precise_root_cause_is_objectwrapper_getattr(self):
        """精确触发点复核：utils.py:123 __getattr__，而非 std.py:1010 默认值求值。"""
        data = json.loads(
            (ROOT / "pack" / "repro_bug_a_before.json").read_text(encoding="utf-8"))
        tb = data["traceback"]
        self.assertIn('utils.py", line 123', tb)
        self.assertIn("__getattr__", tb)
        self.assertIn('_download.py", line 1303', tb)
        self.assertNotIn('std.py", line 1010', tb,
                         "证据显示走到了 std.py:1010，与根因修正不符")

    def test_a10_sdk_callback_base_does_not_create_tqdm(self):
        """核心回调继承 ProgressCallback（只赋两个属性），tqdm 来自 SDK 自建总条。"""
        try:
            import inspect
            import modelscope_hub
            from modelscope.hub.callback import ProgressCallback
        except Exception as exc:  # 3.13 便携版无 SDK
            self.skipTest("当前解释器无 modelscope SDK：{}".format(exc))
        src = inspect.getsource(ProgressCallback)
        self.assertNotIn("tqdm", src,
                         "ProgressCallback 基类不应创建 tqdm（那是 TqdmCallback）")
        self.assertIn("self.filename", src)
        self.assertIn("self.file_size", src)
        self.assertEqual([c.__name__ for c in ProgressCallback.__mro__],
                         ["ProgressCallback", "object"])
        download_py = Path(modelscope_hub.__file__).parent / "_download.py"
        line1303 = download_py.read_text(encoding="utf-8").splitlines()[1302]
        self.assertTrue(line1303.strip().startswith("with tqdm("),
                        "_download.py:1303 不是无条件的 tqdm 构造：{}".format(line1303))

    def test_a11_exact_smoke_params_still_reproduce(self):
        """「大文件/多线程、apply_environment 差异」猜测已被实测排除。"""
        data = json.loads((ROOT / "pack" / "repro_smoke_exact_result.json")
                          .read_text(encoding="utf-8"))
        self.assertIn("REPRODUCED", data["verdict"])
        self.assertIn("NoneType", data["exception"])
        self.assertEqual(data["downloaded_files"], [])
        self.assertTrue(any("README.md" in line for line in data["logs"]))
        self.assertTrue(any("已启用 SDK 进度回调" in line
                            for line in data["logs"]))
        self.assertIn('utils.py", line 123', data["traceback"])


# ===========================================================================
# B1 组：两种子进程启动方式的标准流差异（复盘结论独立实测）
# ===========================================================================
class TestStdioDifference(unittest.TestCase):
    """「旧 --smoke-download 为何漏测」必须自己跑出来，不采信转述。"""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_heavy_toolchain(cls)
        cls.data = _heavy()

    def test_b01_popen_devnull_gives_six_real_streams(self):
        """Popen(stdout/stderr=DEVNULL) 给子进程有效句柄，六流皆 TextIOWrapper。"""
        res = self.data["probe_devnull"]
        self.assertIsNotNone(res, self.data["errors"].get("probe_devnull"))
        self.assertEqual(res["returncode"], 0)
        payload = res["data"]
        self.assertIsNotNone(payload, "DEVNULL 探针未写出结果")
        streams = payload["streams"]
        for name in ("stdout", "stderr", "stdin",
                     "__stdout__", "__stderr__", "__stdin__"):
            with self.subTest(stream=name):
                self.assertNotEqual(streams[name], "NoneType",
                                    "DEVNULL 启动下 {} 竟为 None".format(name))
        self.assertEqual(streams["stderr"], "TextIOWrapper",
                         "旧验证环境下 sys.stderr 应是可写 TextIOWrapper")

    def test_b02_no_handles_gives_six_none_streams(self):
        """CreateProcessW 无标准句柄（等价双击）→ 六流全 NoneType。"""
        res = self.data["probe_nohandles"]
        self.assertIsNotNone(res, self.data["errors"].get("probe_nohandles"))
        self.assertFalse(res["timed_out"], "无句柄探针超时")
        payload = res["data"]
        self.assertIsNotNone(payload, "无句柄探针未写出结果")
        streams = payload["streams"]
        for name in ("stdout", "stderr", "stdin",
                     "__stdout__", "__stderr__", "__stdin__"):
            with self.subTest(stream=name):
                self.assertEqual(streams[name], "NoneType",
                                 "无句柄启动下 {} 应为 NoneType，实为 {}".format(
                                     name, streams[name]))

    def test_b03_the_two_launch_modes_really_differ(self):
        """复盘结论：DEVNULL 有句柄 / 无句柄 None —— 差异确实存在且可复现。"""
        a = self.data["probe_devnull"]["data"]["streams"]
        b = self.data["probe_nohandles"]["data"]["streams"]
        self.assertNotEqual(a, b, "两种启动方式的标准流状态竟然相同，复盘结论不成立")
        self.assertEqual(set(b.values()), {"NoneType"})


# ===========================================================================
# B2 组：stdio guard 的副作用边界
# ===========================================================================
class TestStdioGuardSideEffects(unittest.TestCase):
    """源码模式必须是纯 no-op；guard 后文件日志兜底仍可用。"""

    @staticmethod
    def _load_entry():
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "qa_entry_guard_test", str(ROOT / "pack" / "entry.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_b04_source_mode_guard_is_noop_and_preserves_sys_identity(self):
        names = ("stdout", "stderr", "stdin",
                 "__stdout__", "__stderr__", "__stdin__")
        before = {n: id(getattr(sys, n, None)) for n in names}
        if any(getattr(sys, n, None) is None for n in names):
            self.skipTest("当前解释器自身标准流不完整，无法验证源码模式 no-op")
        module = self._load_entry()
        after_import = {n: id(getattr(sys, n, None)) for n in names}
        self.assertFalse(module._STDIO_GUARD_INSTALLED,
                         "源码模式（有真实控制台）不应安装兜底")
        second = module._install_stdio_guard()
        after_call = {n: id(getattr(sys, n, None)) for n in names}
        self.assertFalse(second, "源码模式 _install_stdio_guard() 必须 return False")
        self.assertEqual(before, after_import,
                         "import entry.py 时改动了 sys 标准流属性")
        self.assertEqual(before, after_call,
                         "显式调用 guard 后改动了 sys 标准流属性")

    def test_b05_guard_streams_are_fully_functional_in_no_handle_process(self):
        """真实无句柄进程里装 guard 后，流的 write/flush/isatty/fileno/encoding 可用。"""
        _skip_if_no_heavy_toolchain(self)
        data = _heavy()
        res = data["ab_guard"]
        self.assertIsNotNone(res, data["errors"].get("ab_guard"))
        payload = res["data"]
        self.assertIsNotNone(payload, "A/B guard 侧未写出结果")
        self.assertEqual(set(payload["streams_before"].values()), {"NoneType"},
                         "A/B guard 侧起点不是无标准句柄环境，对照无效")
        self.assertTrue(payload.get("guard_installed"))
        for name in ("stdout", "stderr"):
            with self.subTest(stream=name):
                iface = payload["iface"][name]
                self.assertNotEqual(iface["type"], "NoneType")
                for method in ("write", "flush", "isatty", "fileno"):
                    self.assertTrue(iface[method],
                                    "guard 流的 {}.{} 不可用".format(name, method))
                self.assertTrue(iface["encoding"], "encoding 应非空")

    def test_b06_file_based_error_log_still_works_when_stdio_is_devnull(self):
        """guard 后把 sys.stderr 指向 devnull，traceback 落盘兜底不依赖控制台。"""
        tmp = Path(tempfile.mkdtemp(prefix="qa-errorlog-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        backup = {n: getattr(sys, n, None)
                  for n in ("stdout", "stderr", "__stdout__", "__stderr__")}
        null_out = open(os.devnull, "w", encoding="utf-8", errors="ignore")
        self.addCleanup(null_out.close)
        try:
            sys.stdout = sys.__stdout__ = null_out
            sys.stderr = sys.__stderr__ = null_out
            target = tmp / "启动错误.log"
            try:
                raise RuntimeError("QA 注入的兜底验证异常")
            except Exception:
                target.write_text(traceback.format_exc(), encoding="utf-8")
            self.assertTrue(target.is_file())
            self.assertIn("RuntimeError: QA", target.read_text(encoding="utf-8"))
            self.assertIn("Traceback", target.read_text(encoding="utf-8"))
        finally:
            for name, value in backup.items():
                setattr(sys, name, value)


# ===========================================================================
# B3 组：A/B 对照 —— 无 guard 复现 / 有 guard 通过（同一调用链）
# ===========================================================================
class TestABControl(unittest.TestCase):
    """不复用 pack/repro_bug_a.py，自己用无句柄进程跑真实 SDK。"""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_heavy_toolchain(cls)
        cls.data = _heavy()

    def test_b07_without_guard_reproduces_none_write_attributeerror(self):
        res = self.data["ab_noguard"]
        self.assertIsNotNone(res, self.data["errors"].get("ab_noguard"))
        self.assertFalse(res["timed_out"])
        payload = res["data"]
        self.assertIsNotNone(payload, "A/B 无 guard 侧未写出结果")
        self.assertEqual(set(payload["streams_before"].values()), {"NoneType"},
                         "对照起点必须真的是六个流全 None")
        self.assertIsNotNone(payload["exception"], "无 guard 竟然没有抛异常")
        self.assertIn("'NoneType' object has no attribute 'write'",
                      payload["traceback"],
                      "未复现 tqdm 的 AttributeError，实际 traceback：{}".format(
                          payload["traceback"][:600]))
        self.assertIn("tqdm", payload["traceback"])
        self.assertIn("_download.py", payload["traceback"])
        # 精确触发点：ObjectWrapper.__getattr__ -> getattr(None, 'write')，
        # 不是 std.py:1010 的 getattr(file, 'write', sys.stderr.write) 默认值
        # 求值分支（本次链路根本没走到那几处）。
        self.assertIn('utils.py", line 123', payload["traceback"],
                      "未命中 tqdm/utils.py:123 ObjectWrapper.__getattr__")
        self.assertIn("__getattr__", payload["traceback"])
        self.assertIn('_download.py", line 1303', payload["traceback"])
        self.assertNotIn('std.py", line 1010', payload["traceback"],
                         "traceback 意外走到了 std.py:1010 默认值求值分支")
        self.assertEqual(payload["downloaded"], [], "崩溃前不应有文件落盘")

    def test_b08_with_guard_same_chain_downloads_readme(self):
        res = self.data["ab_guard"]
        self.assertIsNotNone(res, self.data["errors"].get("ab_guard"))
        self.assertFalse(res["timed_out"])
        payload = res["data"]
        self.assertIsNotNone(payload, "A/B 有 guard 侧未写出结果")
        self.assertIsNone(payload["exception"],
                          "装了 guard 仍抛异常：{}".format(payload.get("exception")))
        self.assertEqual(payload["traceback"], "")
        self.assertTrue(payload["downloaded"], "README.md 未落盘")
        self.assertGreater(payload["downloaded"][0]["size"], 0)
        self.assertTrue(any("已启用 SDK 进度回调" in line
                            for line in payload["logs"]),
                        "日志未显示 SDK 进度回调已启用：{}".format(payload["logs"]))
        self.assertFalse(any("Traceback" in line or "AttributeError" in line
                             for line in payload["logs"]))

    def test_b09_ab_guard_does_not_down_700mb_or_touch_onedrive(self):
        """A/B 两侧只允许下 README.md，且目标目录在系统临时目录。"""
        data = self.data
        for key in ("ab_noguard", "ab_guard"):
            payload = data[key]["data"]
            target = payload.get("target_dir", "")
            self.assertIn(tempfile.gettempdir().replace("\\", "/").lower(),
                          target.replace("\\", "/").lower(),
                          "{} 目标目录不在系统临时目录：{}".format(key, target))
            self.assertNotIn("OneDrive", target)
            for item in payload.get("downloaded", []):
                self.assertEqual(item["name"], "README.md")
                self.assertLess(item["size"], 64 * 1024,
                                "下载了超出几 KB 的文件，疑似权重")


# ===========================================================================
# B4 组：无标准句柄驱动新 exe
# ===========================================================================
class TestExeNoHandles(unittest.TestCase):
    """exe 一律复制到隔离临时目录后驱动，dist/ 零污染。"""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_heavy_toolchain(cls)
        cls.data = _heavy()

    def _report(self, key):
        res = self.data[key]
        self.assertIsNotNone(res, self.data["errors"].get(key))
        self.assertFalse(res["timed_out"], "{} 子进程超时".format(key))
        self.assertEqual(res["error"], "", res.get("error"))
        return res

    @staticmethod
    def _check(report, name_fragment):
        for item in report["checks"]:
            if name_fragment in item["name"]:
                return item
        return None

    def test_b10_self_check_reports_stdio_guard_pass(self):
        res = self._report("exe_self_check")
        report = res["report"]
        self.assertIsNotNone(report, "无句柄下 --self-check 未写出 JSON 报告")
        self.assertTrue(report["ok"], "自检存在失败项：{}".format(report["checks"]))
        self.assertEqual(len(report["checks"]), 8, "自检项应为 8 项")
        item = self._check(report, "windowed 标准流兜底")
        self.assertIsNotNone(item, "缺少 windowed 标准流兜底检查项")
        self.assertTrue(item["ok"], item["detail"])
        self.assertIn("兜底已安装=True", item["detail"], item["detail"])
        self.assertIn("六个流均可用=True", item["detail"], item["detail"])
        self.assertFalse(res["crash_log_exists"],
                         "自检产生了启动错误.log：{}".format(res["crash_log"]))

    def test_b11_smoke_progress_enables_sdk_callback_and_downloads(self):
        res = self._report("exe_smoke_progress")
        report = res["report"]
        self.assertIsNotNone(report, "无句柄下 --smoke-progress 未写出报告")
        self.assertTrue(report["ok"], "进度回调自检失败：{}".format(report["checks"]))
        cb = self._check(report, "SDK 进度回调确实已启用")
        self.assertIsNotNone(cb)
        self.assertTrue(cb["ok"], cb["detail"])
        dl = self._check(report, "真实下载落到指定文件夹且无 AttributeError")
        self.assertIsNotNone(dl)
        self.assertTrue(dl["ok"], dl["detail"])
        all_details = "\n".join(str(c["detail"]) for c in report["checks"])
        self.assertNotIn("AttributeError", all_details)
        self.assertNotIn("Traceback", all_details)
        self.assertNotIn("NoneType", all_details)
        dest = self.data["progress_dest"]
        hits = [p for p in dest.rglob("README.md") if p.is_file()] if dest.is_dir() else []
        self.assertTrue(hits, "目标目录未找到 README.md：{}".format(dest))
        self.assertGreater(hits[0].stat().st_size, 0)
        self.assertLess(hits[0].stat().st_size, 64 * 1024)
        self.assertFalse(res["crash_log_exists"], res["crash_log"])

    def test_b12_smoke_cancel_interrupts_sdk_and_resets_gui(self):
        res = self._report("exe_smoke_cancel")
        report = res["report"]
        self.assertIsNotNone(report, "无句柄下 --smoke-cancel 未写出报告")
        self.assertTrue(report["ok"], "取消自检失败：{}".format(report["checks"]))
        injected = self._check(report, "进度回调类已注入 SDK")
        self.assertIsNotNone(injected)
        self.assertTrue(injected["ok"], injected["detail"])
        interrupted = self._check(report, "取消信号真实中断 SDK 下载循环")
        self.assertIsNotNone(interrupted)
        self.assertTrue(interrupted["ok"], interrupted["detail"])
        self.assertIn("DownloadCancelled", interrupted["detail"])
        self.assertIn("第 3/10 块", interrupted["detail"])
        stopped = self._check(report, "取消被识别为「已停止」而非「任务失败」")
        self.assertIsNotNone(stopped)
        self.assertTrue(stopped["ok"], stopped["detail"])
        self.assertNotIn("任务失败", stopped["detail"])
        gui = self._check(report, "GUI 停止按钮状态机可完整复位")
        self.assertIsNotNone(gui)
        self.assertTrue(gui["ok"], gui["detail"])
        self.assertIn("cancel_set=True", gui["detail"])
        self.assertIn("任务结束后复位=True", gui["detail"])
        all_details = "\n".join(str(c["detail"]) for c in report["checks"])
        self.assertNotIn("Traceback", all_details)
        self.assertFalse(res["crash_log_exists"], res["crash_log"])

    def test_b13_version_print_does_not_crash_without_console(self):
        res = self._report("exe_version")
        self.assertEqual(res["returncode"], 0,
                         "windowed 下 --version 的 print 崩溃了：{}".format(
                             res.get("crash_log")))
        self.assertFalse(res["crash_log_exists"],
                         "产生了启动错误.log：{}".format(res.get("crash_log")))

    def test_b14_dist_untouched_by_all_exe_drives(self):
        """所有自检报告都写在 exe 副本所在临时目录，dist 只有两个交付文件。"""
        names = sorted(p.name for p in (ROOT / "dist").iterdir())
        self.assertEqual(names, self.data["dist_files_before_drive"])
        self.assertEqual(names, sorted(["ModelScope下载器.exe", "使用说明.txt"]))
        for forbidden in ("自检结果.txt", "自检结果.json",
                          "进度回调自检结果.txt", "进度回调自检结果.json",
                          "取消自检结果.txt", "取消自检结果.json",
                          "启动错误.log", "下载自检结果.json"):
            self.assertFalse((ROOT / "dist" / forbidden).exists(),
                             "dist/ 被污染：{}".format(forbidden))


# ===========================================================================
# C 组：Bug B 错误路径与误判边界（纯 mock，不联网）
# ===========================================================================
class TestCancelLogic(unittest.TestCase):
    """打错误路径，验证转译边界与 heartbeat 语义。"""

    def setUp(self):
        self.tmp_obj = tempfile.TemporaryDirectory(prefix="qa-cancel-")
        self.tmp = Path(self.tmp_obj.name)
        self.addCleanup(self.tmp_obj.cleanup)
        self.sdk_backup = dict(core._SDK_CACHE)
        core._SDK_CACHE.clear()
        core._SDK_CACHE.update(
            checked=True, available=False, func=None, version="", error="")

        def restore():
            core._SDK_CACHE.clear()
            core._SDK_CACHE.update(self.sdk_backup)
        self.addCleanup(restore)

    @staticmethod
    def _fake_callback_patch():
        """注入可控的假 ProgressCallback 基类，使回调构造不依赖真实 SDK。

        本机 venv 装了 modelscope 1.40.2，其真实 ProgressCallback 可能是抽象
        基类 / 需要强制参数；不注入会导致 ``_try_build_sdk_progress_callback``
        静默返回 None，取消注入点就消失了，测试会退化成平凡路径。
        """
        import importlib
        import types

        class FakeProgressCallback:
            def __init__(self, *args, **kwargs):
                pass

        fake_module = types.ModuleType("qa_fake_callback")
        fake_module.ProgressCallback = FakeProgressCallback
        real_import = importlib.import_module

        def fake_import(name, *args, **kwargs):
            if name == "modelscope.hub.callback":
                return fake_module
            return real_import(name, *args, **kwargs)

        return mock.patch.object(core.importlib, "import_module",
                                 side_effect=fake_import)

    @classmethod
    def _build_callbacks(cls, cancel_event, logs):
        """构造 SDK 进度回调类：注入假 ProgressCallback 基类，不依赖真实 SDK。"""
        def fake_sdk(model_id, progress_callbacks=None, **kwargs):
            return "D:/fake"

        shared_ref = [0.0, 0]
        with cls._fake_callback_patch():
            callbacks = core._try_build_sdk_progress_callback(
                fake_sdk, shared_ref, logs.append, cancel_event=cancel_event)
        return callbacks, shared_ref

    def test_c01_heartbeat_silent_when_cancel_not_set(self):
        event = threading.Event()
        logs = []
        callbacks, shared = self._build_callbacks(event, logs)
        self.assertIsNotNone(callbacks, "未能构造 SDK 进度回调类")
        reporter = callbacks[0]("README.md", 4917)
        for method in ("update", "__call__", "on_progress", "progress", "end"):
            with self.subTest(method=method):
                getattr(reporter, method)(512)
        reporter.some_unknown_sdk_method(1, 2)  # 走 __getattr__ 降级
        self.assertGreater(shared[0], 0, "heartbeat 未记录时间戳")

    def test_c02_heartbeat_raises_downloadcancelled_when_set(self):
        event = threading.Event()
        event.set()
        logs = []
        callbacks, _ = self._build_callbacks(event, logs)
        reporter = callbacks[0]("README.md", 4917)
        with self.assertRaises(core.DownloadCancelled) as ctx:
            reporter.update(512)
        self.assertIn("停止", str(ctx.exception))

    def test_c03_cancel_set_after_callback_instantiation_is_still_seen(self):
        """重点：取消事件在回调实例化之后才置位，也必须被感知。"""
        event = threading.Event()
        logs = []
        callbacks, _ = self._build_callbacks(event, logs)
        reporter = callbacks[0]("README.md", 4917)
        reporter.update(512)   # 实例化时未取消
        reporter.update(512)
        event.set()            # 之后才取消
        with self.assertRaises(core.DownloadCancelled):
            reporter.update(512)

    def test_c04_no_cancel_event_means_heartbeat_never_raises(self):
        logs = []
        callbacks, _ = self._build_callbacks(None, logs)
        reporter = callbacks[0]("README.md", 4917)
        for _ in range(20):
            reporter.update(512)

    def test_c05_real_network_error_is_not_mistranslated(self):
        """误判边界：非取消的真实错误必须是 DownloadError，不能变成「已取消」。"""
        def broken_sdk(**kwargs):
            raise RuntimeError("connection reset by peer")

        core._SDK_CACHE.clear()
        core._SDK_CACHE.update(
            checked=True, available=True, func=broken_sdk,
            version="fake", error="")
        event = threading.Event()   # 明确未置位
        logs = []
        with mock.patch.object(core, "get_config_path",
                               return_value=self.tmp / "cfg" / "config.json"):
            with self.assertRaises(core.DownloadError) as ctx:
                core.download_model(
                    "Qwen/x", self.tmp / "target", cancel_event=event,
                    log_cb=logs.append, apply_env=False)
        self.assertNotIsInstance(ctx.exception, core.DownloadCancelled)
        self.assertIn("connection reset", str(ctx.exception))
        self.assertFalse(any("已响应停止请求" in line or "下载已取消" in line
                             for line in logs),
                         "真实错误被误转译为取消：{}".format(logs))

    def test_c06_wrapped_sdk_failure_is_translated_only_when_cancel_set(self):
        """SDK 把回调异常收集成 FileDownloadError，取消已置位才转译。"""
        event = threading.Event()
        logs = []

        def fake_sdk(progress_callbacks=None, **kwargs):
            callbacks = [cls("README.md", 4917)
                         for cls in (progress_callbacks or [])]
            failure = None
            try:
                for block in range(1, 11):
                    if block == 3:
                        event.set()
                    for cb in callbacks:
                        cb.update(512)
            except Exception as exc:
                failure = exc
            if failure is not None:
                raise RuntimeError(
                    "1 of 1 file(s) failed to download（模拟 FileDownloadError）"
                ) from failure
            return "D:/fake"

        core._SDK_CACHE.clear()
        core._SDK_CACHE.update(
            checked=True, available=True, func=fake_sdk,
            version="fake", error="")
        with mock.patch.object(core, "get_config_path",
                               return_value=self.tmp / "cfg" / "config.json"), \
                self._fake_callback_patch():
            with self.assertRaises(core.DownloadCancelled) as ctx:
                core.download_model(
                    "Qwen/x", self.tmp / "target", cancel_event=event,
                    log_cb=logs.append, progress_cb=lambda *a: None,
                    apply_env=False)
        self.assertIn("下载已取消", str(ctx.exception))
        self.assertTrue(any("已响应停止请求" in line for line in logs),
                        "缺少停止响应日志：{}".format(logs))
        self.assertFalse(any("任务失败" in line for line in logs),
                         "取消被误报成任务失败：{}".format(logs))

    def test_c07_cancel_already_set_creates_no_target_directory(self):
        """既有回归项：取消已置位时启动 → DownloadCancelled 且不 mkdir。"""
        event = threading.Event()
        event.set()
        target = self.tmp / "must_not_exist"
        logs = []
        with mock.patch.object(core, "get_config_path",
                               return_value=self.tmp / "cfg" / "config.json"):
            with self.assertRaises(core.DownloadCancelled):
                core.download_model(
                    "Qwen/x", target, cancel_event=event,
                    log_cb=logs.append, apply_env=False)
        self.assertFalse(target.exists(),
                         "已取消却创建了目标目录：{}".format(target))

    def test_c08_cancel_exception_message_says_cancelled_not_failed(self):
        """异常消息必须写「下载已取消」，而不是「任务失败 / 下载失败」。"""
        event = threading.Event()
        event.set()
        logs = []
        with mock.patch.object(core, "get_config_path",
                               return_value=self.tmp / "cfg" / "config.json"):
            with self.assertRaises(core.DownloadCancelled) as ctx:
                core.download_model(
                    "Qwen/x", self.tmp / "t2", cancel_event=event,
                    log_cb=logs.append, apply_env=False)
        text = str(ctx.exception)
        self.assertNotIn("任务失败", text)
        self.assertNotIn("下载失败", text)
        self.assertTrue("取消" in text or "停止" in text, text)


# ===========================================================================
# C2 组：GUI 状态机三条路径 + 停止反馈即时可见
# ===========================================================================
@unittest.skipUnless(HAS_TK, "需要带 tkinter 的解释器（venv / 系统 3.14.6）")
class TestGuiStopPaths(unittest.TestCase):
    """真跑 _download_worker 的三条路径，验证 finally 复位。"""

    def setUp(self):
        tmp_obj = tempfile.TemporaryDirectory(prefix="qa-gui-")
        self.tmp = Path(tmp_obj.name)
        self.addCleanup(tmp_obj.cleanup)
        config_path = self.tmp / "cfg" / "config.json"
        sdk_backup = dict(core._SDK_CACHE)
        core._SDK_CACHE.clear()
        core._SDK_CACHE.update(
            checked=True, available=False, func=None, version="", error="")

        def restore():
            core._SDK_CACHE.clear()
            core._SDK_CACHE.update(sdk_backup)
        self.addCleanup(restore)

        patchers = [
            mock.patch.object(core, "get_config_path", return_value=config_path),
            mock.patch.object(gui_app.messagebox, "showinfo"),
            mock.patch.object(gui_app.messagebox, "showerror"),
            mock.patch.object(gui_app.messagebox, "showwarning"),
            mock.patch.object(gui_app.filedialog, "askdirectory", return_value=""),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("无法创建 Tk 根窗口：{}".format(exc))
        self.root.withdraw()
        self.addCleanup(self._safe_destroy)
        self.app = gui_app.DownloaderApp(self.root)
        # 等后台 SDK 探测线程收尾，避免它随后投递日志干扰「停止反馈」断言
        for worker in threading.enumerate():
            if worker.name == "ms-sdk-probe":
                worker.join(timeout=2)
        self.app._drain_ui_queue()
        self.root.update_idletasks()

    def _safe_destroy(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def _run_worker(self, patch_kwargs):
        config = core.AppConfig(model_id="Qwen/x",
                                target_dir=str(self.tmp / "out"))
        self.app._set_busy(True)
        self.app._cancel_event.clear()
        with mock.patch.object(core, "download_model", **patch_kwargs):
            self.app._download_worker(config, [], [], 0)
        # 模拟主线程 40ms 定时泵：把 finally 投递的 _ui 任务排空
        self.app._drain_ui_queue()
        self.root.update_idletasks()
        return {
            "busy": self.app._busy,
            "download": str(self.app.btn_download.cget("state")),
            "stop": str(self.app.btn_stop.cget("state")),
            "status": self.app.var_status.get(),
        }

    def test_c09_success_path_resets_state_machine(self):
        state = self._run_worker({"return_value": "D:/done"})
        self.assertFalse(state["busy"], "正常结束后 _busy 仍为 True")
        self.assertEqual(state["download"], "normal")
        self.assertEqual(state["stop"], "disabled")
        self.assertIn("下载完成", state["status"])

    def test_c10_cancel_path_resets_state_machine(self):
        state = self._run_worker({
            "side_effect": core.DownloadCancelled("下载已取消")})
        self.assertFalse(state["busy"], "取消后 _busy 仍为 True（Bug B 复发）")
        self.assertEqual(state["download"], "normal")
        self.assertEqual(state["stop"], "disabled")
        self.assertIn("已停止", state["status"])

    def test_c11_failure_path_resets_state_machine(self):
        state = self._run_worker({
            "side_effect": core.DownloadError("网络中断")})
        self.assertFalse(state["busy"], "失败后 _busy 仍为 True")
        self.assertEqual(state["download"], "normal")
        self.assertEqual(state["stop"], "disabled")
        self.assertIn("任务失败", state["status"])

    def test_c12_on_stop_log_is_visible_without_driving_event_loop(self):
        """核心可感知性：不调用 root.update，日志也必须立刻可见。"""
        self.app._set_busy(True)
        self.app._drain_ui_queue()
        self.app.log_text.configure(state="normal")
        self.app.log_text.delete("1.0", "end")
        self.app.log_text.configure(state="disabled")
        self.root.update_idletasks()

        self.app._on_stop()  # 内部末尾同步 _drain_ui_queue()
        # 故意不驱动事件循环：若没有同步排空，这里应看不到任何停止日志
        text = self.app.log_text.get("1.0", "end")
        self.assertIn("已请求停止", text,
                      "_on_stop 的日志未即时可见（仍像「点了没反应」）")
        self.assertIn("SDK 进度回调", text)
        self.assertTrue(self.app._cancel_event.is_set())
        self.assertIn("停止", self.app.var_status.get())
        # 与 test_g05 契约一致：停止按钮保持 disabled
        self.assertEqual(str(self.app.btn_stop.cget("state")), "disabled")
        self.app._set_busy(False)  # 模拟 worker finally
        self.app._drain_ui_queue()
        self.root.update_idletasks()
        self.assertEqual(str(self.app.btn_download.cget("state")), "normal")
        self.assertFalse(self.app._busy)


# ===========================================================================
# E 组：交付一致性与残留进程
# ===========================================================================
class TestDeliveryConsistency(unittest.TestCase):

    def test_e01_docs_describe_all_four_switches(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        manual = (ROOT / "dist" / "使用说明.txt").read_text(encoding="utf-8")
        for switch in ("--self-check", "--smoke-download",
                       "--smoke-progress", "--smoke-cancel", "--smoke-select"):
            with self.subTest(switch=switch):
                self.assertIn(switch, readme, "README 未说明 {}".format(switch))
                self.assertIn(switch, manual, "使用说明未说明 {}".format(switch))
        for keyword in ("取消", "停止", "windowed", "标准流", "勾选", "下载范围"):
            with self.subTest(keyword=keyword):
                self.assertIn(keyword, manual)

    def test_e02_entry_main_exposes_all_four_switches(self):
        text = (ROOT / "pack" / "entry.py").read_text(encoding="utf-8")
        for switch in ("--self-check", "--smoke-download",
                       "--smoke-progress", "--smoke-cancel", "--smoke-select"):
            self.assertIn('"{}"'.format(switch), text)
        self.assertIn("_install_stdio_guard()", text)
        # guard 必须是模块级、早于任何第三方导入
        idx_guard = text.index("_install_stdio_guard()\n", text.index("def _install_stdio_guard"))
        idx_sdk_import = text.index("import modelscope")
        self.assertLess(idx_guard, idx_sdk_import,
                        "stdio guard 的模块级调用未早于 modelscope 导入")

    def test_e02b_selection_delivery_scripts_are_present(self):
        """「勾选即范围」的两份交付脚本必须真的在磁盘上、且被文档索引。"""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for rel in ("pack/verify_select.py", "pack/repro_select_bug.py"):
            with self.subTest(file=rel):
                self.assertTrue((ROOT / rel).is_file(), "缺少交付脚本 {}".format(rel))
                self.assertIn(rel, readme, "README 未索引 {}".format(rel))
        for rel in ("pack/repro_select_bug_before.json",
                    "pack/repro_select_bug_after.json"):
            with self.subTest(file=rel):
                self.assertTrue((ROOT / rel).is_file(), "缺少 A/B 证据 {}".format(rel))

    def test_e03_no_residual_exe_process(self):
        """只判「本次验证启动的 exe 进程」有无残留，绝不误伤用户手动实例。

        历史缺陷（ROUTE: QA 已修）：旧实现断言全局进程表里不存在任何
        ``ModelScope*下载器.exe``，无法区分「用户自己双击打开的实例」与
        「验证残留」——用户开着程序做别的事时该用例必然误红，而为了让红转绿
        的唯一捷径就是去杀用户进程，方向完全错误。

        现实现是**双重判据**，任一命中才算残留：
          1. **PID 归属**：本模块通过 ``CreateProcessW`` 启动过并且记录下来的
             PID（``SPAWNED``）仍在运行；
          2. **路径归属**：进程的可执行文件路径落在系统临时目录或本仓库内
             （覆盖 PyInstaller onefile 派生出的、我们没有拿到 PID 的子进程）。
        反之：
          * 路径落在别处（如 ``D:\\Program Files\\自建软件\\``）→ 判定为用户
            手动实例 → 只记录，不判红、不判跳过、绝不动它；
          * 打不开句柄（权限不足）→ 无法归属 → 只记录，不妄下结论。
        另注：本用例在类执行序里跑得早，exe 驱动的残留由 ``tearDownModule``
        在整个模块跑完后再复核一次，避免"检查早于残留产生"的漏网。
        """
        if not IS_WIN:
            self.skipTest("仅 Windows 检查进程")
        mine, foreign, unknown = _classify_exe_processes()
        self.assertEqual(
            mine, [],
            "本次验证启动的 exe 进程残留未退出：{}".format(mine))
        if foreign or unknown:
            # 用户在用的实例：只记录，不判红、不动它（判跳过会让「已验证」失真，
            # 所以这里选择通过 + 备注，由 QA 报告如实呈现）。
            EXE_PROCESS_NOTES.append(
                "非验证来源的实例（已忽略，未做任何操作）：{} / 无法读取路径的 PID {}".format(
                    foreign, unknown))

    def test_e04_dist_directory_has_no_self_check_leftovers(self):
        """自检报告必须写在 exe 所在目录，dist/ 只能有 exe + 使用说明。"""
        names = sorted(p.name for p in (ROOT / "dist").iterdir())
        for leftover in names:
            with self.subTest(name=leftover):
                self.assertNotIn("自检结果", leftover,
                                 "dist/ 出现了自检产物：{}".format(leftover))
                self.assertFalse(leftover.endswith(".log"),
                                 "dist/ 出现了日志：{}".format(leftover))

    def test_e05_residual_judge_really_tells_user_instance_apart(self):
        """用注入的进程表证明判据能区分「用户实例」与「验证残留」。

        这条用例是 ``test_e03`` 的**逻辑自证**：不依赖机器上真的有没有进程，
        因此无论用户是否开着程序，都能证明判据本身是对的。
        """
        always_alive = lambda _pid: True  # noqa: E731 - 注入用
        temp_copy = str(Path(tempfile.gettempdir()) / "qa-fake" / "ModelScope下载器.exe")
        user_copy = r"D:\Program Files\自建软件\ModelScope 下载器.exe"
        repo_copy = str(ROOT / "dist" / "ModelScope下载器.exe")

        # 1) 只有用户实例（哪怕文件名带空格、路径在 D:）→ 不算残留
        mine, foreign, unknown = _classify_exe_processes(
            listing=[(4101, user_copy)], alive=always_alive, spawned=[])
        self.assertEqual(mine, [], "用户手动实例绝不能判成验证残留")
        self.assertEqual([pid for pid, _ in foreign], [4101])
        self.assertEqual(unknown, [])

        # 2) 临时目录里的副本 → 判为残留（等价于本次验证启动的 exe 没退出）
        mine, foreign, _ = _classify_exe_processes(
            listing=[(4102, temp_copy)], alive=always_alive, spawned=[])
        self.assertEqual([pid for pid, _ in mine], [4102])
        self.assertEqual(foreign, [])

        # 3) 仓库内（dist/ 被直接驱动）→ 也判为残留
        mine, _, _ = _classify_exe_processes(
            listing=[(4103, repo_copy)], alive=always_alive, spawned=[])
        self.assertEqual([pid for pid, _ in mine], [4103])

        # 4) 已退出的进程（alive=False）→ 一律不判残留
        mine, foreign, _ = _classify_exe_processes(
            listing=[(4104, temp_copy)], alive=lambda _pid: False, spawned=[])
        self.assertEqual((mine, foreign), ([], []))

        # 5) 拿不到路径（权限不足）→ 只能归为 unknown，不能猜着判红
        mine, _, unknown = _classify_exe_processes(
            listing=[(4105, None)], alive=always_alive, spawned=[])
        self.assertEqual((mine, unknown), ([], [4105]))

        # 6) 跨盘安全：D: 与 C: 之间不会误判为「在树下」
        self.assertFalse(_is_under(Path(user_copy), Path("C:/Users")))
        self.assertTrue(_is_under(Path(user_copy), Path(r"D:\Program Files")))


def _is_under(path: Path, parent: Path) -> bool:
    """``path`` 是否位于 ``parent`` 目录树下（大小写不敏感，跨盘安全）。"""
    try:
        path.relative_to(parent.resolve())
        return True
    except ValueError:
        return False


#: 非验证来源（用户手动打开）的 exe 实例记录，供 QA 报告如实呈现。
EXE_PROCESS_NOTES: list = []

#: 本模块通过 CreateProcessW 启动过、并记录下来的 (pid, 可执行文件路径)。
SPAWNED: list = []


def _record_spawn(pid, application) -> None:
    """登记本次验证亲手启动的进程，作为「残留」的 PID 判据。"""
    if pid:
        SPAWNED.append((int(pid), str(application)))


def _pid_alive(pid, timeout_ms=2500) -> bool:
    """``pid`` 是否仍在运行（先给它 ``timeout_ms`` 的退出宽限期）。"""
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = k32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = ctypes.c_void_p
    wait = k32.WaitForSingleObject
    wait.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    wait.restype = wintypes.DWORD
    close = k32.CloseHandle
    close.argtypes = [ctypes.c_void_p]

    SYNCHRONIZE = 0x00100000
    handle = open_process(SYNCHRONIZE, False, int(pid))
    if not handle:
        return False  # 打不开基本等于已退出（或无权访问，此时也不该判红）
    try:
        return wait(handle, timeout_ms) != 0  # 非 WAIT_OBJECT_0 → 仍存活
    finally:
        close(handle)


def _classify_exe_processes(settle_ms=2500, listing=None, alive=None, spawned=None):
    """把当前 exe 进程分成「验证残留 / 用户实例 / 无法归属」三类。

    Args:
        settle_ms: 判定前给进程的退出宽限期（毫秒）。
        listing: 便于单测注入的进程列表；``None`` 时真实枚举。
        alive: 便于单测注入的「是否存活」判定；``None`` 时按 PID 真实探测。
        spawned: 便于单测注入的「本次启动过的 PID」；``None`` 时用 ``SPAWNED``。

    Returns:
        ``(mine, foreign, unknown)``；``mine`` 为空表示没有残留。
        进程枚举不可用时返回 ``(None, None, None)``。
    """
    listing = _list_exe_processes() if listing is None else listing
    if listing is None:
        return None, None, None
    alive = alive or (lambda pid: _pid_alive(pid, settle_ms))
    spawned = SPAWNED if spawned is None else spawned
    tmp_root = Path(tempfile.gettempdir())
    mine, foreign, unknown = [], [], []
    for pid, image in listing:
        if not image:
            unknown.append(pid)
            continue
        resolved = Path(image)
        if _is_under(resolved, tmp_root) or _is_under(resolved, ROOT):
            if alive(pid):
                mine.append((pid, str(resolved)))
        else:
            foreign.append((pid, str(resolved)))
    # PID 判据：自己启动过的进程即便路径判定不出来，活着也算残留。
    known = {item[0] for item in mine}
    for pid, application in spawned:
        if pid in known:
            continue
        if alive(pid):
            mine.append((pid, "{}（本模块 CreateProcessW 启动）".format(application)))
    return mine, foreign, unknown


def tearDownModule():
    """test_bugfix 全部用例（含 4 次 exe 驱动）跑完后复核一次残留。

    为什么放在这里：``test_e03`` 在类执行序里靠前，跑它的时候 exe 驱动还没
    开始，无法发现"之后才产生"的残留（PyInstaller onefile 的 bootloader 与
    解压出的子进程尤其容易漏）。模块级收尾能覆盖整段执行过程。
    """
    if not IS_WIN or not EXE.is_file():
        return
    mine, foreign, unknown = _classify_exe_processes()
    if mine is None:
        return
    if foreign or unknown:
        EXE_PROCESS_NOTES.append(
            "tearDownModule 观察到非验证来源的实例（已忽略，未做任何操作）："
            "{} / 无法读取路径的 PID {}".format(foreign, unknown))
    if mine:
        raise AssertionError(
            "test_bugfix 的 exe 驱动留下了残留进程（未做任何清理动作，"
            "仅如实报红）：{}".format(mine))


def _list_exe_processes():
    """枚举进程并解析可执行文件路径，只返回名字含 ``ModelScope`` 的 exe。

    Returns:
        ``[(pid, image_path_or_None), ...]``；枚举本身不可用时返回 ``None``。
    """
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    enum = psapi.EnumProcesses
    enum.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                     ctypes.POINTER(wintypes.DWORD)]
    enum.restype = wintypes.BOOL
    open_process = k32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = ctypes.c_void_p
    query = k32.QueryFullProcessImageNameW
    query.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                      wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.BOOL
    close = k32.CloseHandle
    close.argtypes = [ctypes.c_void_p]

    capacity = 8192
    buffer = (wintypes.DWORD * capacity)()
    needed = wintypes.DWORD()
    if not enum(buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
        return None
    count = needed.value // ctypes.sizeof(wintypes.DWORD)
    result = []
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    for index in range(count):
        pid = buffer[index]
        if not pid:
            continue
        handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            path_buf = ctypes.create_unicode_buffer(2048)
            size = wintypes.DWORD(2048)
            image = (path_buf.value
                     if query(handle, 0, path_buf, ctypes.byref(size)) else "")
        finally:
            close(handle)
        name = Path(image).name if image else ""
        if "ModelScope" in name and name.lower().endswith(".exe"):
            result.append((pid, image or None))
    return result


if __name__ == "__main__":
    unittest.main(verbosity=2)
