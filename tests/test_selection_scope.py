# -*- coding: utf-8 -*-
"""QA 独立回归：文件清单「勾选即下载范围」（ROUTE 判定用）。

本文件由 QA（严过关）为「用户只勾 1 个文件，下载器却把整个模型的 11 个文件
全部下回来」这个 Bug 的修复独立编写，**不调用工程师的 ``pack/verify_select.py``
/ ``pack/repro_select_bug.py``**，也不复用其断言，全部用例自建设计：

分组：
    S 范围裁决纯逻辑      TestScopePlanner          （无网络、无 Tk）
    M 与真实 SDK 匹配器一致 TestSdkMatcherAgreement  （离线调用 modelscope_hub）
    C 边界与错误路径      TestScopeEdgeCases        （纯逻辑 + Tk）
    D GUI 交互            TestGuiSelectionInteraction / TestGuiStartDownload
    R 真实下载（只下小文件）TestRealSelectedDownload
    X exe 端到端          TestExeSmokeSelect

约束：
    * 真实下载只允许 ``README.md``(4917B) / ``.gitattributes``(1519B) 级别的小文件，
      目标目录一律 ``tempfile.TemporaryDirectory``，绝不下载权重（model.safetensors
      988MB），绝不写入用户 OneDrive / %APPDATA% / ``D:\\Program Files\\自建软件\\``；
    * 驱动 exe 时先把 exe **复制到临时目录**再运行，``dist/`` 零污染；
    * 子进程用 CreateProcessW(bInheritHandles=False) 启动并由父进程同步等待，
      等价「资源管理器双击」（onefile + windowed 从 shell 启动会提前交还控制权）。
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ms_downloader_core as core  # noqa: E402

IS_WIN = os.name == "nt"
EXE = ROOT / "dist" / "ModelScope下载器.exe"

try:
    import tkinter as tk  # noqa: F401
    HAS_TK = True
except Exception:  # 便携版 Python 3.13 无 tkinter
    HAS_TK = False
    tk = None

if HAS_TK:
    import gui_app  # noqa: E402
else:
    gui_app = None

try:  # 真实 SDK 匹配器（离线可调用，不需要联网）
    from modelscope_hub._download import _matches_patterns  # noqa: E402
    from modelscope_hub.utils.patterns import normalize_patterns  # noqa: E402
    HAS_SDK_MATCHER = True
except Exception:  # noqa: BLE001 - 没装 SDK 时跳过该组
    HAS_SDK_MATCHER = False
    _matches_patterns = None
    normalize_patterns = None


# ---------------------------------------------------------------------------
# 固定被测模型（真实清单，2026-11-11 用 core.fetch_repo_files 复核过）
# ---------------------------------------------------------------------------
MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"
REVISION = "master"
SMALL_FILE = "README.md"
SMALL_BYTES = 4917

#: 真实清单的 11 个文件：(path, size)
REAL_FILES = (
    (".gitattributes", 1519),
    ("LICENSE", 11343),
    ("README.md", 4917),
    ("config.json", 659),
    ("configuration.json", 2),
    ("generation_config.json", 242),
    ("merges.txt", 1671839),
    ("model.safetensors", 988097824),
    ("tokenizer.json", 7031645),
    ("tokenizer_config.json", 7305),
    ("vocab.json", 2776833),
)
REAL_TOTAL = sum(size for _, size in REAL_FILES)
WEIGHT_FILE = "model.safetensors"


def _listing(files=REAL_FILES, model_id=MODEL_ID, revision=REVISION):
    """按真实清单构造 ``ModelInfo``（假清单，不联网）。"""
    return core.ModelInfo(
        model_id=model_id,
        revision=revision,
        files=[core.RepoFile(path=path, size=size) for path, size in files],
        source="SDK",
    )


def _paths(files=REAL_FILES):
    return [path for path, _ in files]


# ---------------------------------------------------------------------------
# 无标准句柄子进程启动器（独立实现，不复用工程师脚本）
# ---------------------------------------------------------------------------

#: 本模块亲手启动过的 (pid, 可执行文件路径)，用于「只判自己启动的进程」。
_SPAWNED: list = []


def _spawn_no_handles(application, args, cwd, timeout_ms=300000):
    """CreateProcessW：bInheritHandles=False + 不设 STARTF_USESTDHANDLES。

    等价于资源管理器双击：windowed 进程拿不到任何标准句柄。父进程用
    ``WaitForSingleObject`` 同步等待；超时则 TerminateProcess 收尾并如实返回。
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

    def _quote(text):
        return '"{}"'.format(str(text).replace('"', '\\"'))

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
    WAIT_TIMEOUT = 0x102
    _SPAWNED.append((int(pi.dwProcessId), str(application)))  # 残留判据：PID 归属
    timed_out = wait(pi.hProcess, timeout_ms) == WAIT_TIMEOUT
    if timed_out:
        terminate(pi.hProcess, 1)
        wait(pi.hProcess, 5000)
    code = wintypes.DWORD()
    getcode(pi.hProcess, ctypes.byref(code))
    close(pi.hThread)
    close(pi.hProcess)
    return {"returncode": code.value, "timed_out": timed_out, "error": ""}


def _is_under(path, parent) -> bool:
    """``path`` 是否位于 ``parent`` 目录树下（跨盘安全）。"""
    try:
        Path(path).relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def _pid_alive(pid, timeout_ms=2500) -> bool:
    """``pid`` 是否仍在运行（先给它 ``timeout_ms`` 退出的宽限期）。"""
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
        return False
    try:
        return wait(handle, timeout_ms) != 0
    finally:
        close(handle)


def _list_model_exe_processes():
    """枚举名字含 ``ModelScope`` 的 exe 进程及其可执行路径。"""
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

    buffer = (wintypes.DWORD * 8192)()
    needed = wintypes.DWORD()
    if not enum(buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
        return []
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    result = []
    for index in range(needed.value // ctypes.sizeof(wintypes.DWORD)):
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
            result.append((int(pid), image))
    return result


def _residual_exe_processes():
    """返回「本次验证留下的」exe 残留；用户手动实例只记录、不返回。"""
    tmp_root = Path(tempfile.gettempdir())
    mine, foreign = [], []
    for pid, image in _list_model_exe_processes():
        if image and (_is_under(image, tmp_root) or _is_under(image, ROOT)):
            if _pid_alive(pid):
                mine.append((pid, image))
        else:
            foreign.append((pid, image or "（无法读取路径）"))
    known = {item[0] for item in mine}
    for pid, application in _SPAWNED:
        if pid not in known and _pid_alive(pid):
            mine.append((pid, application))
    if foreign:
        _NOTES.append("非验证来源的实例（已忽略，未做任何操作）：{}".format(foreign))
    return mine


#: 非验证来源实例的记录，供报告如实呈现。
_NOTES: list = []


def tearDownModule():
    """整个模块（含 exe 端到端驱动）跑完后复核一次残留。"""
    if not IS_WIN or not EXE.is_file():
        return
    mine = _residual_exe_processes()
    if mine:
        raise AssertionError(
            "test_selection_scope 的 exe 驱动留下了残留进程（未做任何清理动作，"
            "仅如实报红）：{}".format(mine))


# ===========================================================================
# S 组：范围裁决纯逻辑（不联网、不起 Tk）
# ===========================================================================
class TestScopePlanner(unittest.TestCase):
    """``plan_download_scope`` 的优先级规则逐条核对（工程师设计规则 1~9）。"""

    def test_s01_single_selection_becomes_exact_allow_pattern(self):
        """只勾 1 个 → allow_patterns 严格等于该路径，绝不是空列表。"""
        scope = core.plan_download_scope(
            listing=_listing(), selected_paths=[SMALL_FILE],
            model_id=MODEL_ID, revision=REVISION)
        self.assertEqual(scope.mode, core.SCOPE_SELECTED)
        self.assertEqual(scope.allow_patterns, [SMALL_FILE],
                         "只勾 1 个文件时 allow_patterns 必须精确等于该文件，"
                         "空列表会被 SDK 当成「不过滤=下载全部」")
        self.assertNotEqual(scope.allow_patterns, [],
                            "空 allow_patterns 正是本次 Bug 的成因")
        self.assertEqual(scope.total_bytes, SMALL_BYTES)
        self.assertEqual((scope.selected_count, scope.total_count), (1, 11))
        self.assertFalse(scope.is_blocked)

    def test_s02_select_all_falls_back_to_legacy_behaviour(self):
        """全选 → 退回旧行为：allow 为空（下载全部）、分母 = 整模型总大小。"""
        scope = core.plan_download_scope(
            listing=_listing(), selected_paths=_paths(),
            model_id=MODEL_ID, revision=REVISION)
        self.assertEqual(scope.mode, core.SCOPE_ALL)
        self.assertEqual(scope.allow_patterns, [])
        self.assertEqual(scope.total_bytes, REAL_TOTAL)
        self.assertEqual((scope.selected_count, scope.total_count), (11, 11))

    def test_s03_select_all_keeps_user_rules(self):
        """全选时手填 allow / ignore / subdir 照常生效（老习惯不受影响）。"""
        scope = core.plan_download_scope(
            allow_raw="*.safetensors", ignore_raw="*.md", subdir="onnx",
            listing=_listing(), selected_paths=_paths(),
            model_id=MODEL_ID, revision=REVISION)
        self.assertIn("*.safetensors", scope.allow_patterns)
        self.assertIn("onnx/*", scope.allow_patterns)
        self.assertEqual(scope.ignore_patterns, ["*.md"])
        self.assertTrue(any("照常生效" in note for note in scope.notes),
                        "全选退回旧行为时日志应有说明：{}".format(scope.notes))

    def test_s04_no_listing_downloads_everything_and_says_so(self):
        """未获取清单 → 下载全部，但日志必须明确写出「未获取清单」。"""
        scope = core.plan_download_scope(listing=None, selected_paths=[])
        self.assertEqual(scope.mode, core.SCOPE_ALL)
        self.assertEqual(scope.allow_patterns, [])
        self.assertEqual(scope.total_bytes, 0)
        text = "\n".join(scope.notes)
        self.assertIn("未获取清单", text)
        self.assertIn("本次下载全部文件", text)

    def test_s05_stale_listing_from_other_model_is_ignored(self):
        """清单属于别的 model_id → 视为未获取清单，不得套用旧勾选。"""
        stale = _listing(model_id="other/model-x")
        scope = core.plan_download_scope(
            listing=stale, selected_paths=[SMALL_FILE],
            model_id=MODEL_ID, revision=REVISION)
        self.assertEqual(scope.mode, core.SCOPE_ALL)
        self.assertEqual(scope.allow_patterns, [],
                         "过期清单的勾选不能变成新模型的过滤条件")
        self.assertEqual(scope.total_bytes, 0)
        self.assertIn("其它模型", "\n".join(scope.notes))

    def test_s06_stale_listing_from_other_revision_is_ignored(self):
        """清单 revision 与输入不一致 → 同样视为未获取清单。"""
        stale = _listing(revision="v1.0")
        scope = core.plan_download_scope(
            listing=stale, selected_paths=[SMALL_FILE],
            model_id=MODEL_ID, revision=REVISION)
        self.assertEqual(scope.mode, core.SCOPE_ALL)
        self.assertEqual(scope.allow_patterns, [])
        self.assertIn("其它模型", "\n".join(scope.notes))

    def test_s07_zero_selection_is_blocked_not_silently_all(self):
        """0 勾选 → SCOPE_EMPTY + is_blocked，绝不静默下载整模型。"""
        scope = core.plan_download_scope(
            listing=_listing(), selected_paths=[],
            model_id=MODEL_ID, revision=REVISION)
        self.assertTrue(scope.is_blocked)
        self.assertEqual(scope.mode, core.SCOPE_EMPTY)
        self.assertEqual(scope.allow_patterns, [])
        self.assertEqual(scope.total_bytes, 0)
        self.assertIn("一个都没勾选", "\n".join(scope.notes))
        self.assertIn("已中止", scope.describe())

    def test_s08_unknown_selected_paths_are_dropped(self):
        """勾选里混入清单外的路径 → 丢弃，不污染 allow_patterns。"""
        scope = core.plan_download_scope(
            listing=_listing(),
            selected_paths=[SMALL_FILE, "../../etc/passwd", "not-in-listing.bin"],
            model_id=MODEL_ID, revision=REVISION)
        self.assertEqual(scope.allow_patterns, [SMALL_FILE])

    def test_s09_selection_beats_hand_typed_allow_rules(self):
        """勾选优先：部分勾选时手填 include / subdir 被忽略且写日志。"""
        scope = core.plan_download_scope(
            allow_raw="*.safetensors", subdir="onnx",
            listing=_listing(), selected_paths=[SMALL_FILE],
            model_id=MODEL_ID, revision=REVISION)
        self.assertEqual(scope.allow_patterns, [SMALL_FILE],
                         "勾选优先时 allow_patterns 只能由勾选生成")
        text = "\n".join(scope.notes)
        self.assertIn("勾选优先", text)
        self.assertIn("*.safetensors", text)

    def test_s10_ignore_rules_always_win_and_are_logged(self):
        """排除规则永远优先：勾了也传 ignore，并提示该规则仍生效。"""
        scope = core.plan_download_scope(
            ignore_raw="*.md", listing=_listing(),
            selected_paths=[SMALL_FILE, WEIGHT_FILE],
            model_id=MODEL_ID, revision=REVISION)
        self.assertEqual(scope.ignore_patterns, ["*.md"])
        self.assertIn(SMALL_FILE, scope.allow_patterns)
        self.assertIn("排除规则仍然生效", "\n".join(scope.notes))
        # 工程师自述「分母含被排除文件」：如实核对当前实现（可能不满 100%）
        self.assertEqual(
            scope.total_bytes, SMALL_BYTES + 988097824,
            "当前实现的分母含被排除文件；若行为变更需同步更新 README 已知限制")

    def test_s11_progress_denominator_switches_between_modes(self):
        """进度分母：勾选时为勾选字节和；全选时为整模型总大小。"""
        files = (("README.md", 4917), ("weight.bin", 12_000_000))
        listing = _listing(files=files)
        one = core.plan_download_scope(
            listing=listing, selected_paths=["README.md"],
            model_id=MODEL_ID, revision=REVISION)
        self.assertEqual(one.total_bytes, 4917)
        both = core.plan_download_scope(
            listing=listing, selected_paths=["README.md", "weight.bin"],
            model_id=MODEL_ID, revision=REVISION)
        self.assertEqual(both.total_bytes, 12_004_917)

    def test_s12_describe_reports_n_of_m(self):
        """describe() 必须给出「本次下载 N/M 个文件」。"""
        scope = core.plan_download_scope(
            listing=_listing(), selected_paths=[SMALL_FILE],
            model_id=MODEL_ID, revision=REVISION)
        self.assertIn("本次下载 1/11 个文件", scope.describe())

    def test_s13_scope_order_follows_listing_not_click_order(self):
        """allow_patterns 顺序按清单顺序，避免抖动影响快照。"""
        scope = core.plan_download_scope(
            listing=_listing(), selected_paths=["vocab.json", "README.md"],
            model_id=MODEL_ID, revision=REVISION)
        self.assertEqual(scope.allow_patterns, ["README.md", "vocab.json"])


# ===========================================================================
# C 组（前半）：转义与文件名边界
# ===========================================================================
class TestGlobEscaping(unittest.TestCase):
    """通配符转义：勾选哪个就只能匹配哪个（规则 6）。"""

    CASES = (
        ("a[b].txt", "a[[]b[]].txt"),
        ("a*b.txt", "a[*]b.txt"),
        ("a?b.txt", "a[?]b.txt"),
        ("model[1].onnx", "model[[]1[]].onnx"),
    )

    def test_c01_escape_map_is_exact(self):
        for raw, expected in self.CASES:
            with self.subTest(path=raw):
                self.assertEqual(core.escape_glob_pattern(raw), expected)

    def test_c02_escape_normalizes_separators_and_prefix(self):
        self.assertEqual(core.escape_glob_pattern("onnx\\model.onnx"),
                         "onnx/model.onnx")
        self.assertEqual(core.escape_glob_pattern("./onnx/model.onnx"),
                         "onnx/model.onnx")
        self.assertEqual(core.escape_glob_pattern("/a/b.txt"), "a/b.txt")
        self.assertEqual(core.escape_glob_pattern(""), "")
        self.assertEqual(core.escape_glob_pattern(None), "")

    def test_c03_selection_to_patterns_dedupes_and_keeps_order(self):
        result = core.selection_to_allow_patterns(
            [" b.txt", "a.txt", "./b.txt", "a.txt", ""])
        self.assertEqual(result, ["b.txt", "a.txt"])

    def test_c04_non_sequence_is_treated_as_empty(self):
        for value in (None, "README.md", 123):
            with self.subTest(value=value):
                self.assertEqual(core.selection_to_allow_patterns(value), [])


class TestScopeEdgeCases(unittest.TestCase):
    """边界场景：逗号文件名、多级子目录、清单为空。"""

    def test_c05_comma_filename_is_reported_not_silently_lost(self):
        """英文逗号：SDK 会按逗号切分规则 → 必须有明确日志告警。"""
        files = (("a,b.txt", 10), ("README.md", 4917))
        scope = core.plan_download_scope(
            listing=_listing(files=files), selected_paths=["a,b.txt"],
            model_id=MODEL_ID, revision=REVISION)
        self.assertEqual(scope.allow_patterns, ["a,b.txt"])
        text = "\n".join(scope.notes)
        self.assertIn("英文逗号", text,
                      "逗号文件名无法精确表达，必须在日志里告警")
        self.assertIn("a,b.txt", text)

    def test_c06_nested_subdir_selection_is_expressed_exactly(self):
        """多级子目录：勾 onnx/model.onnx 只表达这一条，不含目录通配。"""
        files = (("onnx/model.onnx", 100), ("onnx/other.onnx", 200),
                 ("README.md", 4917))
        scope = core.plan_download_scope(
            listing=_listing(files=files), selected_paths=["onnx/model.onnx"],
            model_id=MODEL_ID, revision=REVISION)
        self.assertEqual(scope.allow_patterns, ["onnx/model.onnx"])
        self.assertEqual(scope.total_bytes, 100)
        self.assertNotIn("onnx/*", scope.allow_patterns)

    def test_c07_listing_without_files_counts_as_no_listing(self):
        """清单对象存在但 files 为空 → 等价未获取清单（下载全部 + 提示）。"""
        empty = core.ModelInfo(model_id=MODEL_ID, revision=REVISION, files=[])
        scope = core.plan_download_scope(
            listing=empty, selected_paths=[], model_id=MODEL_ID,
            revision=REVISION)
        self.assertEqual(scope.mode, core.SCOPE_ALL)
        self.assertFalse(scope.is_blocked)

    def test_c08_empty_revision_is_normalized_to_default(self):
        """revision 传空串等价 master，不应误判清单过期。"""
        scope = core.plan_download_scope(
            listing=_listing(), selected_paths=[SMALL_FILE],
            model_id=MODEL_ID, revision="")
        self.assertEqual(scope.mode, core.SCOPE_SELECTED)
        self.assertEqual(scope.allow_patterns, [SMALL_FILE])


# ===========================================================================
# M 组：与真实 SDK 匹配器的一致性（离线，不需要网络）
# ===========================================================================
@unittest.skipUnless(HAS_SDK_MATCHER, "需要 modelscope_hub 才能核对真实匹配语义")
class TestSdkMatcherAgreement(unittest.TestCase):
    """用 SDK 自己的 ``normalize_patterns`` + ``_matches_patterns`` 复核转义。

    这比「断言字符串长得对」强一档：直接把程序生成的正则交给 SDK 判断，
    看它到底会下哪些文件。
    """

    SIBLINGS = ("ab.txt", "aXb.txt", "a[b].txt", "a*b.txt", "a?b.txt",
                "model1.onnx", "model[1].onnx", "onnx/model.onnx", "README.md")

    def _selected(self, path):
        patterns = normalize_patterns(core.selection_to_allow_patterns([path]))
        return [item for item in self.SIBLINGS if _matches_patterns(item, patterns)]

    def test_m01_each_escaped_name_hits_only_itself(self):
        for path in ("a[b].txt", "a*b.txt", "a?b.txt", "model[1].onnx"):
            with self.subTest(path=path):
                self.assertEqual(self._selected(path), [path],
                                 "转义后必须只命中自己、不误伤兄弟文件")

    def test_m02_nested_path_hits_only_itself(self):
        self.assertEqual(self._selected("onnx/model.onnx"), ["onnx/model.onnx"])

    def test_m03_comma_filename_really_cannot_be_expressed(self):
        """确认工程师「逗号无法精确表达」的结论属实（不是拍脑袋）。"""
        patterns = normalize_patterns(core.selection_to_allow_patterns(["a,b.txt"]))
        self.assertEqual(patterns, ["a", "b.txt"],
                         "SDK 确实按逗号切分；文档与日志告警基于此事实")
        self.assertEqual(
            [item for item in ("a", "b.txt", "a,b.txt")
             if _matches_patterns(item, patterns)],
            ["a", "b.txt"])

    def test_m04_empty_allow_list_means_no_filtering(self):
        """空 allow_patterns 被 SDK 归一化成 None = 不过滤 = 下载全部。

        这是本次 Bug 的直接成因，必须有测试盯住，防止回归。
        """
        self.assertIsNone(normalize_patterns([]))
        self.assertIsNone(normalize_patterns(None))
        all_paths = _paths()
        patterns = normalize_patterns([])
        filtered = [p for p in all_paths if _matches_patterns(p, patterns)]
        self.assertEqual(filtered, [],
                         "_matches_patterns 在 patterns 为空时恒 False，"
                         "即上游「不过滤」→ 全部下载")

    def test_m05_exact_selection_filters_down_to_one_of_eleven(self):
        """11 个真实文件里只勾 1 个 → 过滤后只剩 1 个。"""
        patterns = normalize_patterns(core.selection_to_allow_patterns([SMALL_FILE]))
        kept = [p for p in _paths() if _matches_patterns(p, patterns)]
        self.assertEqual(kept, [SMALL_FILE])


# ===========================================================================
# Tk GUI 夹具
# ===========================================================================
@unittest.skipUnless(HAS_TK, "需要带 tkinter 的解释器（本机 pack/.venv Python 3.14.6）")
class _GuiCase(unittest.TestCase):
    """真实 Tk 主窗口 + 真实 Treeview，窗口放在屏幕外不干扰用户。"""

    FILES = REAL_FILES

    def setUp(self):
        self._tmp_obj = tempfile.TemporaryDirectory(prefix="qa-scope-gui-")
        self.tmp = Path(self._tmp_obj.name)
        self.config_path = self.tmp / "APPDATA" / "ModelScopeDownloader" / "config.json"
        self.dest = self.tmp / "out"

        self.captured = []
        self.popups = []

        def fake_download(**kwargs):
            self.captured.append(dict(kwargs))
            return str(kwargs.get("target_dir", ""))

        def record(kind):
            def _inner(*args, **kwargs):
                self.popups.append((kind, args, kwargs))
                return "ok"
            return _inner

        patchers = [
            mock.patch.object(core, "get_config_path", return_value=self.config_path),
            mock.patch.object(core, "download_model", side_effect=fake_download),
            mock.patch.object(gui_app.messagebox, "showinfo", record("showinfo")),
            mock.patch.object(gui_app.messagebox, "showerror", record("showerror")),
            mock.patch.object(gui_app.messagebox, "showwarning", record("showwarning")),
            mock.patch.object(gui_app.messagebox, "askyesno",
                              lambda *a, **k: True),
            mock.patch.object(gui_app.filedialog, "askdirectory", return_value=""),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.root = tk.Tk()
        self.root.geometry("1180x820+4000+3000")  # 屏幕外，避免遮挡用户桌面
        try:  # 兜底定时销毁，任何意外都不会把测试挂死
            self._destroy_job = self.root.after(60000, self.root.destroy)
        except tk.TclError:
            self._destroy_job = None
        self.root.update()
        self.app = gui_app.DownloaderApp(self.root)
        self.root.update()
        self._load_listing()

    def tearDown(self):
        """在 Tk 解释器仍存活时释放 tk 变量，避免收尾噪音 / 跨线程 abort。"""
        app = getattr(self, "app", None)
        root = getattr(self, "root", None)
        if root is not None:
            for job in (getattr(self, "_destroy_job", None),
                        getattr(app, "_timer_job", None),
                        getattr(app, "_ui_pump_job", None)):
                if job:
                    try:
                        root.after_cancel(job)
                    except Exception:  # noqa: BLE001
                        pass
            if app is not None:
                for name in ("_drain_ui_queue", "_stop_elapsed_timer"):
                    try:
                        getattr(app, name)()
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    app.progress_bar.stop()
                except Exception:  # noqa: BLE001
                    pass
                # StringVar/BooleanVar 必须在解释器销毁前回收，否则 __del__
                # 会落到已销毁的 Tcl 解释器上（"main thread is not in main loop"）。
                for attr in list(vars(app)):
                    try:
                        if isinstance(getattr(app, attr), tk.Variable):
                            delattr(app, attr)
                    except Exception:  # noqa: BLE001
                        pass
            try:
                root.update_idletasks()
            except Exception:  # noqa: BLE001
                pass
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass
        self.app = None
        self.root = None
        self._tmp_obj.cleanup()

    # -- helpers ---------------------------------------------------------
    def _load_listing(self, info=None):
        self.app.var_model.set(MODEL_ID)
        self.app.var_revision.set(REVISION)
        self.app.var_dir.set(str(self.dest))
        self.app._show_model_info(info or _listing(files=self.FILES))
        self._pump()

    def _pump(self, seconds=0.0):
        """驱动事件循环（含 root.after 泵）并排空界面队列。"""
        deadline = time.monotonic() + seconds
        while True:
            self.root.update()
            self.app._drain_ui_queue()
            if time.monotonic() >= deadline:
                return

    def _wait_worker(self, seconds=30.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.root.update()
            self.app._drain_ui_queue()
            worker = self.app._worker
            if worker is None or not worker.is_alive():
                self._pump()
                return True
            time.sleep(0.02)
        return False

    def _log_text(self):
        self._pump()
        return self.app.log_text.get("1.0", "end")

    def _values(self, iid):
        return list(self.app.tree.item(iid, "values"))

    def _click(self, iid, column="#2"):
        """在真实坐标上派发一次 <Button-1>（不是直接调私有方法）。"""
        x, y, width, height = self.app.tree.bbox(iid, column)
        self.app.tree.event_generate(
            "<Button-1>", x=x + width // 2, y=y + height // 2)
        self._pump()


# ===========================================================================
# D 组：GUI 勾选交互
# ===========================================================================
@unittest.skipUnless(HAS_TK, "需要带 tkinter 的解释器")
class TestGuiSelectionInteraction(_GuiCase):
    """单击任意列都要翻转 ☑/☐，且「下载范围」提示实时跟随。"""

    def test_d01_click_file_path_column_toggles_check(self):
        """Bug 复现关键：单击「文件路径」列必须真的改变勾选状态。"""
        iid = SMALL_FILE
        self.assertEqual(self._values(iid)[0], gui_app.CHECKED)
        self._click(iid, "#2")
        self.assertEqual(self._values(iid)[0], gui_app.UNCHECKED,
                         "单击「文件路径」列后必须是 ☐；旧版只是行高亮 → 用户误以为已选")
        self._click(iid, "#2")
        self.assertEqual(self._values(iid)[0], gui_app.CHECKED)

    def test_d02_click_any_column_toggles(self):
        for column in ("#1", "#2", "#3", "#4"):
            with self.subTest(column=column):
                iid = "LICENSE"
                before = self._values(iid)[0]
                self._click(iid, column)
                self.assertNotEqual(self._values(iid)[0], before,
                                    "{} 列单击未翻转勾选".format(column))

    def test_d03_selection_column_click_leaves_no_row_highlight(self):
        """「选择」列返回 break → 不产生行高亮，杜绝「高亮≠勾选」的误解。"""
        current = self.app.tree.selection()
        if current:
            self.app.tree.selection_remove(*current)
        self._click(SMALL_FILE, "#1")
        self.assertEqual(tuple(self.app.tree.selection()), ())

    def test_d04_click_on_heading_toggles_nothing(self):
        apps_before = self.app._selected_paths()
        self.app.tree.event_generate("<Button-1>", x=40, y=6)
        self._pump()
        self.assertEqual(self.app._selected_paths(), apps_before)

    def test_d05_scope_hint_tracks_selection(self):
        total = len(self.FILES)
        self.app._select_all(False)
        self._pump()
        self.assertIn("未勾选任何文件", self.app.var_scope.get())
        self.assertIn("拦下", self.app.var_scope.get())

        self._click(SMALL_FILE, "#2")  # 用真实单击，而不是内部方法
        scope = self.app.var_scope.get()
        self.assertIn("1/{0}".format(total), scope)
        self.assertIn(core.format_size(SMALL_BYTES), scope)

        self.app._select_all(True)
        self._pump()
        scope = self.app.var_scope.get()
        self.assertIn("全部 {0} 个文件".format(total), scope)
        self.assertIn(core.format_size(REAL_TOTAL), scope)

    def test_d06_invert_and_select_none_are_consistent_with_planner(self):
        self.app._select_all(False)
        self.app._toggle_row(SMALL_FILE)
        self._pump()
        self.app._invert_selection()
        self._pump()
        checked = self.app._selected_paths()
        self.assertEqual(len(checked), len(self.FILES) - 1)
        self.assertNotIn(SMALL_FILE, checked)

        scope = core.plan_download_scope(
            listing=self.app._model_info, selected_paths=checked,
            model_id=MODEL_ID, revision=REVISION)
        self.assertEqual(scope.selected_count, len(checked))
        self.assertIn("勾选的 {0}/{1}".format(len(checked), len(self.FILES)),
                      self.app.var_scope.get())

        self.app._select_all(False)
        self._pump()
        self.assertEqual(self.app._selected_paths(), [])


# ===========================================================================
# D 组（续）：点【开始下载】的分支路由
# ===========================================================================
@unittest.skipUnless(HAS_TK, "需要带 tkinter 的解释器")
class TestGuiStartDownload(_GuiCase):
    """界面把勾选结果正确交给 SDK，且 0 勾选被硬拦。"""

    def test_d07_zero_selection_blocks_with_dialog_and_no_thread(self):
        """0 勾选 → 弹窗 + 不启动线程 + 不调用下载 + 目标目录零写入。"""
        self.app._select_all(False)
        self._pump()
        self.app.btn_download.invoke()  # 不抛 traceback 即为通过
        self._pump(0.2)

        self.assertIsNone(self.app._worker, "0 勾选时绝不能启动下载线程")
        self.assertEqual(self.captured, [], "0 勾选时绝不能调用 core.download_model")
        titles = [args[0] for kind, args, _ in self.popups if kind == "showinfo"]
        self.assertIn("请先勾选文件", titles, "必须弹窗告知用户先勾选")
        blocked_dirs = list(self.dest.rglob("*")) if self.dest.exists() else []
        self.assertEqual(blocked_dirs, [], "被拦下时不得创建任何文件")

    def test_d08_single_selection_passes_exact_allow_pattern(self):
        """只勾 README.md → SDK 收到 ['README.md']、分母 4917、日志写 1/11。"""
        self.app._select_all(False)
        self.app._toggle_row(SMALL_FILE)
        self._pump()
        self.app.btn_download.invoke()
        self.assertTrue(self._wait_worker(), "下载线程未在超时内结束")

        self.assertEqual(len(self.captured), 1)
        kwargs = self.captured[0]
        self.assertEqual(kwargs.get("allow_patterns"), [SMALL_FILE],
                         "只勾 1 个文件时 SDK 的 allow_patterns 必须精确等于该文件")
        self.assertEqual(kwargs.get("total_bytes"), SMALL_BYTES)
        text = self._log_text()
        self.assertIn("本次下载 1/11 个文件", text)
        self.assertIn(core.format_size(SMALL_BYTES), text)

    def test_d09_select_all_passes_empty_allow_and_model_total(self):
        """全选 → allow 为空（旧行为）、分母 = 整模型总大小、日志写 11/11。"""
        self.app._select_all(True)
        self._pump()
        self.app.btn_download.invoke()
        self.assertTrue(self._wait_worker())
        kwargs = self.captured[0]
        self.assertEqual(kwargs.get("allow_patterns"), [])
        self.assertEqual(kwargs.get("total_bytes"), REAL_TOTAL)
        self.assertIn("本次下载 11/11 个文件", self._log_text())

    def test_d10_missing_listing_downloads_all_with_explicit_log(self):
        """未查清单 → allow 为空 + 日志明确写「未获取清单」。"""
        self.app._model_info = None
        for iid in self.app.tree.get_children():
            self.app.tree.delete(iid)
        self._pump()
        self.app.btn_download.invoke()
        self.assertTrue(self._wait_worker())
        kwargs = self.captured[0]
        self.assertEqual(kwargs.get("allow_patterns"), [])
        self.assertEqual(kwargs.get("total_bytes"), 0)
        text = self._log_text()
        self.assertIn("未获取清单", text)
        self.assertIn("本次下载全部文件", text)

    def test_d11_stale_listing_does_not_leak_old_selection(self):
        """改了模型名但没重查清单 → 不得沿用旧清单的勾选。"""
        self.app._select_all(False)
        self.app._toggle_row(SMALL_FILE)
        self.app.var_model.set("Qwen/Qwen2.5-1.5B-Instruct")
        self._pump()
        self.app.btn_download.invoke()
        self.assertTrue(self._wait_worker())
        self.assertEqual(self.captured[0].get("allow_patterns"), [],
                         "旧清单属于别的模型，必须按「未获取清单」处理")
        self.assertIn("其它模型", self._log_text())

    def test_d12_hand_typed_allow_is_overridden_by_selection(self):
        """勾 1 个 + 手填 *.safetensors → 仍只下勾选的那个，且日志说明。"""
        self.app.var_allow.set("*.safetensors")
        self.app._select_all(False)
        self.app._toggle_row(SMALL_FILE)
        self._pump()
        self.app.btn_download.invoke()
        self.assertTrue(self._wait_worker())
        self.assertEqual(self.captured[0].get("allow_patterns"), [SMALL_FILE])
        self.assertIn("勾选优先", self._log_text())


# ===========================================================================
# C 组（后端）：配置迁移
# ===========================================================================
@unittest.skipUnless(HAS_TK, "需要带 tkinter 的解释器")
class TestLegacyConfigMigration(unittest.TestCase):
    """旧 config.json 里 ``only_selected: false`` 不得再让勾选失效、也不得崩。"""

    def setUp(self):
        self._tmp_obj = tempfile.TemporaryDirectory(prefix="qa-scope-cfg-")
        self.tmp = Path(self._tmp_obj.name)
        self.config_path = self.tmp / "config.json"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._tmp_obj.cleanup)

    def _write_legacy(self):
        self.config_path.write_text(json.dumps({
            "model_id": MODEL_ID,
            "target_dir": str(self.tmp / "dl"),
            "revision": REVISION,
            "only_selected": False,
        }, ensure_ascii=False), encoding="utf-8")

    def test_c09_legacy_false_loads_without_crash(self):
        self._write_legacy()
        config = core.load_config(self.config_path)
        self.assertEqual(config.model_id, MODEL_ID)

    def test_c10_gui_forces_only_selected_true_and_migrates_file(self):
        self._write_legacy()
        with mock.patch.object(core, "get_config_path", return_value=self.config_path), \
                mock.patch.object(gui_app.messagebox, "showinfo"), \
                mock.patch.object(gui_app.messagebox, "showerror"):
            root = tk.Tk()
            root.geometry("1180x820+4000+3000")
            root.update()
            try:
                app = gui_app.DownloaderApp(root)
                root.update()
                self.assertTrue(app.var_only_selected.get(),
                                "旧配置 only_selected=false 必须被忽略（恒为 True）")
                config = app._collect_config()
                self.assertTrue(config.only_selected)
                saved = json.loads(self.config_path.read_text(encoding="utf-8"))
                self.assertTrue(saved.get("only_selected", False),
                                "下次保存时旧配置应被自动迁移为 true")
            finally:
                for attr in list(vars(app)):
                    try:
                        if isinstance(getattr(app, attr), tk.Variable):
                            delattr(app, attr)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    root.update_idletasks()
                except Exception:  # noqa: BLE001
                    pass
                root.destroy()


# ===========================================================================
# R 组：真实下载（只下小文件，绝不拉权重）
# ===========================================================================
_REAL = {}


def _ensure_real():
    """真实下载只跑一次：单文件（用户场景）+ 双文件（多选）。"""
    if _REAL:
        return _REAL
    tmp_obj = tempfile.TemporaryDirectory(prefix="qa-scope-real-")
    tmp = Path(tmp_obj.name)
    _REAL["tmp_obj"] = tmp_obj
    _REAL["tmp"] = tmp
    _REAL["result"] = {}
    _REAL["errors"] = {}

    def run(key, selected, dirname):
        dest = tmp / dirname
        logs = []
        totals = []
        try:
            core.download_model(
                model_id=MODEL_ID, target_dir=str(dest), revision=REVISION,
                allow_patterns=core.selection_to_allow_patterns(selected),
                ignore_patterns=[],
                total_bytes=sum(
                    size for path, size in REAL_FILES if path in selected),
                log_cb=logs.append,
                progress_cb=lambda done, total, message="": totals.append(total),
            )
            files = sorted(
                (p.relative_to(dest).as_posix(), p.stat().st_size)
                for p in dest.rglob("*") if p.is_file())
            _REAL["result"][key] = {
                "files": files, "logs": logs,
                "totals": sorted({value for value in totals if value}),
            }
        except Exception as exc:  # noqa: BLE001 - 记录后由用例断言失败
            _REAL["errors"][key] = "{}: {}".format(type(exc).__name__, exc)

    run("single", [SMALL_FILE], "single")
    run("pair", [SMALL_FILE, ".gitattributes"], "pair")
    return _REAL


def _skip_if_no_network_toolchain(case):
    """兼容保留：真实下载组改用 setUpClass 判定依赖。"""
    if not core.sdk_is_installed():
        case.skipTest("当前解释器未安装 modelscope，跳过真实下载验证")


class TestRealSelectedDownload(unittest.TestCase):
    """真实 SDK + 真实网络：勾 1 个就只落 1 个文件（只跑一次，缓存共享）。"""

    @classmethod
    def setUpClass(cls):
        if not core.sdk_is_installed():
            raise unittest.SkipTest("当前解释器未安装 modelscope，跳过真实下载验证")
        cls.data = _ensure_real()
        if cls.data["errors"]:
            raise AssertionError("真实下载准备失败：{}".format(cls.data["errors"]))

    @classmethod
    def tearDownClass(cls):
        data = _REAL
        if data.get("tmp_obj") is not None:
            data["tmp_obj"].cleanup()
        data.clear()

    def test_r01_single_selection_downloads_only_that_file(self):
        got = self.data["result"]["single"]
        self.assertEqual(
            got["files"], [(SMALL_FILE, SMALL_BYTES)],
            "只勾 README.md 时目标目录必须只有该文件（用户报告的 Bug 现场）")
        others = [path for path, _ in got["files"] if path != SMALL_FILE]
        self.assertEqual(others, [], "不得多下载其它 10 个文件：{}".format(others))
        self.assertNotIn(WEIGHT_FILE, [p for p, _ in got["files"]])

    def test_r02_single_selection_denominator_is_selected_bytes(self):
        got = self.data["result"]["single"]
        self.assertEqual(got["totals"], [SMALL_BYTES],
                         "进度分母应为勾选文件字节数，而不是整模型 999,604,128")

    def test_r03_two_files_download_exactly_two(self):
        got = self.data["result"]["pair"]
        self.assertEqual(
            got["files"],
            [(".gitattributes", 1519), (SMALL_FILE, 4917)],
            "勾 2 个文件时只应落这 2 个（校验多选与目录结构）")


# ===========================================================================
# X 组：exe 端到端（无标准句柄，等价双击）
# ===========================================================================
_EXE = {}


def _ensure_exe():
    """把 exe 复制到临时目录后驱动 --smoke-select，dist/ 零污染。"""
    if _EXE:
        return _EXE
    tmp_obj = tempfile.TemporaryDirectory(prefix="qa-scope-exe-")
    tmp = Path(tmp_obj.name)
    _EXE["tmp_obj"] = tmp_obj
    _EXE["tmp"] = tmp
    _EXE["dist_before"] = sorted(p.name for p in (ROOT / "dist").iterdir())
    exe = tmp / EXE.name
    shutil.copy2(EXE, exe)
    dest = tmp / "out"
    spawn = _spawn_no_handles(str(exe), ["--smoke-select", str(dest)], tmp, 420000)
    _EXE["spawn"] = spawn
    report = tmp / "勾选下载自检结果.json"
    _EXE["report"] = (json.loads(report.read_text(encoding="utf-8"))
                      if report.is_file() else None)
    _EXE["report_text"] = (
        (tmp / "勾选下载自检结果.txt").read_text(encoding="utf-8", errors="replace")
        if (tmp / "勾选下载自检结果.txt").is_file() else "")
    _EXE["out_files"] = sorted(
        (p.relative_to(dest).as_posix(), p.stat().st_size)
        for p in dest.rglob("*") if p.is_file()) if dest.is_dir() else []
    _EXE["dest"] = dest
    _EXE["dist_after"] = sorted(p.name for p in (ROOT / "dist").iterdir())
    return _EXE


@unittest.skipUnless(IS_WIN, "CreateProcessW 仅 Windows 可用")
@unittest.skipUnless(EXE.is_file(), "缺少 dist/ModelScope下载器.exe")
class TestExeSmokeSelect(unittest.TestCase):
    """无标准句柄驱动 exe 的 ``--smoke-select``（真实 GUI + 1 次真实下载）。"""

    @classmethod
    def setUpClass(cls):
        cls.data = _ensure_exe()

    @classmethod
    def tearDownClass(cls):
        if cls.data.get("tmp_obj") is not None:
            cls.data["tmp_obj"].cleanup()

    def test_x01_exe_exits_zero(self):
        spawn = self.data["spawn"]
        self.assertFalse(spawn.get("timed_out"), "exe 自检超时未结束")
        self.assertEqual(spawn.get("error"), "")
        self.assertEqual(spawn.get("returncode"), 0,
                         "exe --smoke-select 退出码非 0\n{}".format(
                             self.data.get("report_text", "")[:4000]))

    def test_x02_all_checks_pass(self):
        report = self.data.get("report")
        self.assertIsNotNone(report, "未生成 勾选下载自检结果.json")
        failed = [item["name"] for item in report["checks"] if not item["ok"]]
        self.assertEqual(failed, [], "以下自检项失败：{}".format(failed))
        self.assertTrue(report["ok"])
        self.assertGreaterEqual(len(report["checks"]), 9,
                                "自检项数量少于 9 项")

    def test_x03_no_standard_handles_still_download(self):
        """无控制台句柄（等价双击）下仍能跑完真实下载，且只落 1 个文件。"""
        detail = "\n".join(
            item["detail"] for item in self.data["report"]["checks"]
            if "标准流" in item["name"])
        self.assertIn("stdout=", detail)
        self.assertEqual(self.data["out_files"],
                         [(SMALL_FILE, SMALL_BYTES)],
                         "exe 只勾 1 个文件时目标目录必须只有 README.md")

    def test_x04_log_reports_one_of_eleven(self):
        text = self.data.get("report_text", "")
        self.assertIn("本次下载 1/11 个文件", text)

    def test_x05_dist_not_polluted_by_drive(self):
        self.assertEqual(self.data["dist_before"], self.data["dist_after"])
        self.assertEqual(
            sorted(p.name for p in self.data["tmp"].iterdir()
                   if p.suffix in (".json", ".txt")),
            sorted(["勾选下载自检结果.json", "勾选下载自检结果.txt"]),
            "自检产物应写在 exe 副本所在目录（临时目录），而不是 dist/")


# ===========================================================================
# U 组：用户真实场景复核（LiquidAI/LFM3-1.7B-GGUF，17 个文件 / 14.9 GB）
# ===========================================================================
#: 用户 config.json 里实际在用的模型：想只下 1 个 .gguf，结果把 14.9 GB 全下了。
USER_MODEL = "LiquidAI/LFM3-1.7B-GGUF"
USER_WANT = "LFM3-1.7B-Q4_K_M.gguf"
USER_WANT_BYTES = 1099627808
USER_TINY = "leap/Q4_0.json"      # 子目录里的小文件，用于真实下载（1800 B）
USER_TINY_BYTES = 1800

_USER = {}


def _ensure_user_scenario():
    """拉取用户真实模型的清单，并真实下载其中最小的一个子目录文件。"""
    if _USER:
        return _USER
    tmp_obj = tempfile.TemporaryDirectory(prefix="qa-scope-user-")
    _USER["tmp_obj"] = tmp_obj
    _USER["error"] = ""
    _USER["info"] = None
    _USER["files"] = []
    _USER["download"] = None
    try:
        info = core.fetch_repo_files(USER_MODEL, REVISION)
    except Exception as exc:  # noqa: BLE001 - 无网络时由用例 skip
        _USER["error"] = "{}: {}".format(type(exc).__name__, exc)
        return _USER
    _USER["info"] = info
    _USER["files"] = [(item.path, item.size) for item in info.files]
    dest = Path(tmp_obj.name) / "nested"
    try:
        core.download_model(
            model_id=USER_MODEL, target_dir=str(dest), revision=REVISION,
            allow_patterns=core.selection_to_allow_patterns([USER_TINY]),
            ignore_patterns=[], total_bytes=USER_TINY_BYTES,
            log_cb=lambda _m: None)
        _USER["download"] = sorted(
            (p.relative_to(dest).as_posix(), p.stat().st_size)
            for p in dest.rglob("*") if p.is_file())
    except Exception as exc:  # noqa: BLE001
        _USER["error"] = "{}: {}".format(type(exc).__name__, exc)
    return _USER


@unittest.skipUnless(HAS_TK or HAS_SDK_MATCHER, "需要 modelscope 才能复核真实仓库")
class TestUserRealScenario(unittest.TestCase):
    """用户报告现场的同款仓库：勾 1 个 .gguf 就只能落 1 个。"""

    @classmethod
    def setUpClass(cls):
        if not core.sdk_is_installed():
            raise unittest.SkipTest("当前解释器未安装 modelscope")
        cls.data = _ensure_user_scenario()
        if cls.data["error"]:
            raise unittest.SkipTest("真实仓库不可用：{}".format(cls.data["error"]))

    @classmethod
    def tearDownClass(cls):
        data = _USER
        if data.get("tmp_obj") is not None:
            data["tmp_obj"].cleanup()
        data.clear()

    def test_u01_user_repo_listing_has_17_files(self):
        self.assertEqual(self.data["info"].file_count, len(self.data["files"]))
        self.assertGreaterEqual(len(self.data["files"]), 10,
                                "用户仓库应包含多个 .gguf 量化")

    def test_u02_selecting_one_gguf_expresses_exactly_one_pattern(self):
        scope = core.plan_download_scope(
            listing=self.data["info"], selected_paths=[USER_WANT],
            model_id=USER_MODEL, revision=REVISION)
        self.assertEqual(scope.allow_patterns, [USER_WANT])
        self.assertEqual(scope.total_bytes, USER_WANT_BYTES)
        self.assertEqual(
            (scope.selected_count, scope.total_count),
            (1, len(self.data["files"])))
        self.assertIn("本次下载 1/{0} 个文件".format(len(self.data["files"])),
                      scope.describe())

    @unittest.skipUnless(HAS_SDK_MATCHER, "需要 modelscope_hub 核对真实匹配")
    def test_u03_sdk_keeps_only_the_selected_gguf(self):
        """把勾选结果交给 SDK 真实匹配器：17 个文件里只剩 1 个。"""
        patterns = normalize_patterns(
            core.selection_to_allow_patterns([USER_WANT]))
        kept = [path for path, _ in self.data["files"]
                if _matches_patterns(path, patterns)]
        self.assertEqual(kept, [USER_WANT],
                         "其它 8 个 .gguf 量化必须被过滤掉")

    def test_u04_nested_small_file_really_downloads_alone(self):
        """真实下载子目录里的 leap/Q4_0.json：只落 1 个文件，目录结构保留。"""
        self.assertEqual(self.data["download"], [(USER_TINY, USER_TINY_BYTES)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
