# -*- coding: utf-8 -*-
"""ModelScope 下载器 —— 纯逻辑层独立验收测试（A~F 组）。

约束：
    * 仅使用标准库 ``unittest``；
    * 不安装 / 不导入真实 ``modelscope``，不发起任何真实网络请求；
    * REST 查询全部 mock ``core._http_get_json``，SDK 全部通过
      ``core._SDK_CACHE`` 注入或 ``sys.modules`` 假模块模拟；
    * 测试之间互相隔离（SDK 缓存、环境变量、临时目录均自动还原）。
"""
from __future__ import annotations

import ast
import importlib
import importlib.machinery
import json
import math
import os
import py_compile
import re
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ms_downloader_core as core  # noqa: E402

CORE_PATH = ROOT / "ms_downloader_core.py"
GUI_PATH = ROOT / "gui_app.py"
DELIVERABLES = (
    "ms_downloader_core.py", "gui_app.py", "启动.bat",
    "安装依赖.bat", "requirements.txt", "README.md",
)

_ENV_KEYS = (
    "MODELSCOPE_ENDPOINT", "MODELSCOPE_API_ENDPOINT",
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
    "ALL_PROXY", "all_proxy", "APPDATA",
)


class IsolatedCoreTestCase(unittest.TestCase):
    """隔离 SDK 缓存、环境变量并提供临时目录的基类。"""

    def setUp(self) -> None:
        sdk_backup = dict(core._SDK_CACHE)
        env_backup = {key: os.environ.get(key) for key in _ENV_KEYS}
        tmp_obj = tempfile.TemporaryDirectory(prefix="msqa_")
        self.tmp = Path(tmp_obj.name)

        def restore() -> None:
            core._SDK_CACHE.clear()
            core._SDK_CACHE.update(sdk_backup)
            for key, value in env_backup.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            tmp_obj.cleanup()

        self.addCleanup(restore)

    def use_fake_sdk(self, func, version: str = "9.9.9-qa") -> None:
        """把 ``_SDK_CACHE`` 伪装成「SDK 已加载且可用」。"""
        core._SDK_CACHE.clear()
        core._SDK_CACHE.update(
            checked=True, available=True, func=func, version=version, error=""
        )

    def use_no_sdk(self) -> None:
        """把 ``_SDK_CACHE`` 伪装成「SDK 不可用」。"""
        core._SDK_CACHE.clear()
        core._SDK_CACHE.update(
            checked=True, available=False, func=None, version="", error="not installed"
        )

    def use_unchecked_sdk(self) -> None:
        """恢复为未探测状态，让 ``_load_sdk()`` 真实尝试导入（本机必然失败）。"""
        core._SDK_CACHE.clear()
        core._SDK_CACHE.update(
            checked=False, available=False, func=None, version="", error=""
        )


# ===========================================================================
# A 组：静态与结构
# ===========================================================================

class TestStaticStructure(unittest.TestCase):
    """A. 交付物完整性、语法、依赖方向与文档一致性。"""

    def test_a01_all_deliverables_exist(self) -> None:
        """六个交付文件必须真实存在且非空。"""
        for name in DELIVERABLES:
            with self.subTest(name=name):
                path = ROOT / name
                self.assertTrue(path.is_file(), "缺少交付文件：{0}".format(path))
                self.assertGreater(path.stat().st_size, 0)

    def test_a02_python_sources_compile(self) -> None:
        """两份 .py 源码既要能 compile，也要能 py_compile（含 __pycache__ 隔离）。"""
        for path in (CORE_PATH, GUI_PATH):
            with self.subTest(path=path.name):
                source = path.read_text(encoding="utf-8")
                compile(source, str(path), "exec")
                with tempfile.TemporaryDirectory(prefix="msqa_pyc_") as td:
                    cfile = str(Path(td) / (path.stem + ".pyc"))
                    py_compile.compile(str(path), cfile=cfile, doraise=True)
                    self.assertTrue(Path(cfile).is_file())

    def test_a03_core_has_no_tkinter_and_no_hard_modelscope_import(self) -> None:
        """core 顶层不得 import tkinter，也不得硬 import modelscope。"""
        tree = ast.parse(CORE_PATH.read_text(encoding="utf-8"))
        top_level: list = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                top_level.append(node.module or "")
        for name in top_level:
            self.assertFalse(name.split(".")[0] in ("tkinter", "modelscope"),
                             "core 顶层出现禁用导入：{0}".format(name))
        # 全文件都不应存在静态 import 语句（延迟导入必须用 importlib）
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], ("tkinter", "modelscope"),
                                     "core 出现硬 import：{0}".format(alias.name))
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0],
                                 ("tkinter", "modelscope"),
                                 "core 出现硬 from-import：{0}".format(node.module))

    def test_a04_no_circular_import_and_import_is_idempotent(self) -> None:
        """core 不得反向依赖 gui_app；重复导入应返回同一模块对象。"""
        tree = ast.parse(CORE_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name, "gui_app")
            elif isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "gui_app")
        gui_tree = ast.parse(GUI_PATH.read_text(encoding="utf-8"))
        gui_imports = [
            alias.name for node in ast.walk(gui_tree)
            if isinstance(node, ast.Import) for alias in node.names
        ]
        self.assertIn("ms_downloader_core", gui_imports,
                      "gui_app 应单向依赖 core")
        again = importlib.import_module("ms_downloader_core")
        self.assertIs(again, core)

    def test_a05_gui_referenced_core_members_all_exist(self) -> None:
        """用 AST 提取 gui_app.py 中所有 core.* 引用，逐个 getattr 校验。"""
        tree = ast.parse(GUI_PATH.read_text(encoding="utf-8"))
        names = {
            node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "core"
        }
        self.assertGreaterEqual(len(names), 15, "提取到的 core.* 成员过少")
        for name in sorted(names):
            with self.subTest(member=name):
                self.assertTrue(hasattr(core, name),
                                "gui_app.py 引用了不存在的 core.{0}".format(name))
        # core.__all__ 中声明的公开 API 也必须真实存在
        for name in core.__all__:
            with self.subTest(public_api=name):
                self.assertTrue(hasattr(core, name),
                                "__all__ 声明了不存在的 {0}".format(name))

    def test_a06_bat_files_are_pure_ascii_and_consistent(self) -> None:
        """两个 bat：纯 ASCII 无 BOM、chcp 65001、cd /d、解释器选择逻辑一致。"""
        texts = {}
        for name in ("启动.bat", "安装依赖.bat"):
            raw = (ROOT / name).read_bytes()
            with self.subTest(file=name):
                self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "存在 UTF-8 BOM")
                self.assertEqual([b for b in raw if b > 127], [],
                                 "bat 含非 ASCII 字节，cmd 下会中文乱码")
                self.assertEqual(raw.count(b"\r"), raw.count(b"\r\n"),
                                 "存在孤立 CR，换行格式不完整")
                text = raw.decode("ascii")
                texts[name] = text
                for fragment in ("@echo off", "chcp 65001", 'cd /d "%~dp0"', "setlocal"):
                    self.assertIn(fragment, text, "缺少 {0}".format(fragment))
                self.assertIn("where pythonw", text)
                self.assertIn("where python", text)
                self.assertIn("import tkinter", text)
                self.assertLess(text.index("where pythonw"),
                                text.index("'where python "),
                                "应优先尝试 pythonw")
                self.assertNotIn("py -3", text, "本机无 py -3，不应使用该命令")

        launcher, installer = texts["启动.bat"], texts["安装依赖.bat"]
        self.assertIn('"gui_app.py"', launcher, "启动脚本未指向真实入口")
        self.assertTrue((ROOT / "gui_app.py").is_file())
        self.assertIn("requirements.txt", installer)
        self.assertIn("pip install -r requirements.txt", installer)
        self.assertTrue((ROOT / "requirements.txt").is_file())
        # 两者都同时具备 pythonw / python / tkinter 校验，逻辑一致
        for fragment in ("where pythonw", "'where python ",
                         '"%%~dpWpython.exe" -c "import tkinter"',
                         '"%%P" -c "import tkinter"'):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, launcher)
                self.assertIn(fragment, installer)

    def test_a07_readme_requirements_match_real_files(self) -> None:
        """README / requirements 中提到的文件名与入口必须与实物一致。"""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        for name in ("启动.bat", "安装依赖.bat", "requirements.txt",
                     "gui_app.py", "ms_downloader_core.py"):
            with self.subTest(name=name):
                self.assertIn(name, readme, "README 未提及 {0}".format(name))
                self.assertTrue((ROOT / name).is_file())
        self.assertIn("main()", readme)
        self.assertIn("gui_app.py", readme)
        self.assertIn("modelscope", requirements)
        self.assertIn("requests", requirements)
        self.assertIn(core.INSTALL_COMMAND, readme)

    @unittest.skipUnless(os.name == "nt", "仅 Windows 需验证 cmd.exe 解析")
    def test_a08_launcher_dry_run_parses_lf_only_bat(self) -> None:
        """LF-only 的启动脚本在 cmd 下能正确解析并选中带 tkinter 的解释器。

        干跑：把真正启动 GUI 的两行替换为 echo，避免弹窗 / 挂死；其余
        （for /f、if defined、goto :EOF）保持原样。
        """
        text = (ROOT / "启动.bat").read_bytes().decode("ascii")
        text = text.replace('start "" "%PYWEXE%" "gui_app.py"',
                            'echo [DRYRUN] pythonw=%PYWEXE%')
        text = text.replace('"%PYEXE%" "gui_app.py"',
                            'echo [DRYRUN] python=%PYEXE%')
        text = text.replace("pause", "echo [DRYRUN] pause skipped")
        with tempfile.TemporaryDirectory(prefix="msqa_bat_") as td:
            probe = Path(td) / "launcher_dryrun.bat"
            probe.write_bytes(text.encode("ascii"))
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            try:
                proc = subprocess.run(
                    [comspec, "/c", str(probe)],
                    stdin=subprocess.DEVNULL, capture_output=True,
                    text=True, timeout=40,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                self.skipTest("cmd.exe 在当前沙箱不可用：{0}".format(exc))
            output = (proc.stdout or "") + (proc.stderr or "")
            if "No usable Python" in output:
                self.skipTest("当前 PATH 无带 tkinter 的解释器，属环境限制")
            self.assertEqual(proc.returncode, 0, output)
            self.assertIn("[OK] Using", output, output)


# ===========================================================================
# B 组：纯函数边界
# ===========================================================================

class TestNormalizeModelId(IsolatedCoreTestCase):
    """B1. 模型 ID 规范化的精确行为与「绝不裸抛异常」契约。"""

    VALID_CASES = (
        ("Qwen/Qwen2.5-7B-Instruct", "Qwen/Qwen2.5-7B-Instruct"),
        ("  Qwen/Qwen2.5-7B-Instruct  ", "Qwen/Qwen2.5-7B-Instruct"),
        ('"Qwen/Qwen2.5-7B-Instruct"', "Qwen/Qwen2.5-7B-Instruct"),
        ("https://www.modelscope.cn/models/Qwen/Qwen2.5-7B-Instruct",
         "Qwen/Qwen2.5-7B-Instruct"),
        ("https://www.modelscope.cn/models/Qwen/Qwen2.5-7B-Instruct/summary",
         "Qwen/Qwen2.5-7B-Instruct"),
        ("https://www.modelscope.cn/models/Qwen/Qwen2.5-7B-Instruct?revision=master",
         "Qwen/Qwen2.5-7B-Instruct"),
        ("http://modelscope.cn/model/qwen/Qwen2.5-7B-Instruct-GGUF",
         "qwen/Qwen2.5-7B-Instruct-GGUF"),
        ("Qwen/Qwen2.5-7B-Instruct.git", "Qwen/Qwen2.5-7B-Instruct"),
        ("QWEN/Qwen2.5", "QWEN/Qwen2.5"),
        ("Qwen/Qwen2.5/onnx/q4", "Qwen/Qwen2.5"),
        ("Qwen/Qwen2.5-7B-Instruct/", "Qwen/Qwen2.5-7B-Instruct"),
        ("Qwen\\Qwen2.5", "Qwen/Qwen2.5"),
        ("模型/名称", "模型/名称"),
    )

    INVALID_CASES = (
        None, "", "   ", "Qwen", "/", "../Qwen/xxx", "Qwen/../etc",
        "Qwen/na<me", "Qwen/na>me", "Qwen/na:me", 'Qwen/na"me',
        "Qwen/na|me", "Qwen/na*me", "Qwen/空格 名", 123, ["a", "b"],
    )

    ABNORMAL_CASES = INVALID_CASES + (
        "Qwen/na?me", "Qwen/na#me", "模型/名称",
        "a" * 5000 + "/B", "https://www.modelscope.cn/Qwen/Qwen2.5",
        "Qwen//name", "////", 0, 3.14, object(),
        "Qwen/na\x00me", "Qwen/na\tme", "Qwen/na\nme",
    )

    def test_b01_exact_valid_outputs(self) -> None:
        for raw, expected in self.VALID_CASES:
            with self.subTest(raw=raw):
                self.assertEqual(core.normalize_model_id(raw), expected)

    def test_b02_exact_invalid_inputs_raise_validation_error(self) -> None:
        for raw in self.INVALID_CASES:
            with self.subTest(raw=raw):
                with self.assertRaises(core.ValidationError):
                    core.normalize_model_id(raw)

    def test_b03_contract_never_raises_bare_exception(self) -> None:
        """异常输入只允许两种结果：规范化成功，或 ValidationError。"""
        for raw in self.ABNORMAL_CASES:
            with self.subTest(raw=str(raw)[:60]):
                try:
                    result = core.normalize_model_id(raw)
                except core.ValidationError:
                    continue
                except Exception as exc:  # noqa: BLE001
                    self.fail("normalize_model_id({0!r}) 抛出裸异常 {1}: {2}".format(
                        raw, type(exc).__name__, exc))
                # 规范化成功时，结果必须是干净的 owner/name 两段式
                self.assertIsInstance(result, str)
                self.assertTrue(result)
                parts = result.split("/")
                self.assertEqual(len(parts), 2, "结果不是两段式：{0!r}".format(result))
                self.assertTrue(all(parts))
                self.assertNotIn("..", result)
                self.assertFalse(any(ch in result for ch in ':*?"<>|'))
                self.assertFalse(any(ch.isspace() for ch in result))


class TestPatterns(IsolatedCoreTestCase):
    """B2. 过滤规则解析与合并。"""

    def test_b04_parse_patterns_exact(self) -> None:
        cases = (
            ("", []), (None, []), (",,, , , ", []),
            ("*.safetensors, *.gguf", ["*.safetensors", "*.gguf"]),
            ("onnx/*", ["onnx/*"]),
            ("  a , a ,b ; c\nd", ["a", "b", "c", "d"]),
            ("onnx\\*", ["onnx/*"]),
            ('"*.json"', ["*.json"]),
            (" ,*.safetensors,,*.gguf, ", ["*.safetensors", "*.gguf"]),
            (0, []), ([], []),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(core.parse_patterns(raw), expected)

    def test_b05_parse_patterns_contract(self) -> None:
        """任意输入都返回无空项、无重复、无反斜杠的字符串列表。"""
        for raw in ("", None, "a" * 5000, ";;;", "\n\n", 0, 3.14,
                    ["*.a"], {"a": 1}, "a;b,c\nd,a"):
            with self.subTest(raw=str(raw)[:40]):
                try:
                    result = core.parse_patterns(raw)
                except Exception as exc:  # noqa: BLE001
                    self.fail("parse_patterns 抛出裸异常 {0}: {1}".format(
                        type(exc).__name__, exc))
                self.assertIsInstance(result, list)
                self.assertEqual(len(result), len(set(result)), "存在重复项")
                for item in result:
                    self.assertIsInstance(item, str)
                    self.assertTrue(item.strip())
                    self.assertNotIn("\\", item)

    def test_b06_resolve_patterns_exact(self) -> None:
        self.assertEqual(core.resolve_patterns(), ([], []))
        allow, ignore = core.resolve_patterns(
            "*.json", "*.md", "onnx/q4", ["a.bin", "/b.bin", "a.bin"]
        )
        self.assertEqual(allow, ["*.json", "onnx/q4/*", "onnx/q4/**", "a.bin", "b.bin"])
        self.assertEqual(ignore, ["*.md"])
        self.assertEqual(core.resolve_patterns(None, None, None, None), ([], []))

    def test_b07_resolve_patterns_contract(self) -> None:
        for args in (
            (), ("a", "b", "c", None), (None, None, None, None),
            ("a", "b", "onnx\\q4", ["x/y.bin"]),
            ("", "", "///", []), (123, 456, 789, "not-a-list"),
        ):
            with self.subTest(args=str(args)[:50]):
                try:
                    allow, ignore = core.resolve_patterns(*args)
                except Exception as exc:  # noqa: BLE001
                    self.fail("resolve_patterns 抛出裸异常 {0}: {1}".format(
                        type(exc).__name__, exc))
                self.assertIsInstance(allow, list)
                self.assertIsInstance(ignore, list)
                self.assertTrue(all(isinstance(x, str) for x in allow + ignore))


class TestFormatSize(IsolatedCoreTestCase):
    """B3. 字节数格式化。"""

    def test_b08_exact_outputs(self) -> None:
        cases = (
            (0, "0 B"), (-1, "未知"), (1023, "1023 B"), (1024, "1.00 KB"),
            (1536, "1.50 KB"), (1024 ** 2, "1.00 MB"),
            (1024 ** 3, "1.00 GB"), (1024 ** 4, "1.00 TB"),
            (10 ** 18, "888.18 PB"), (None, "未知"), ("abc", "未知"),
            ("1024", "1.00 KB"), (-float("inf"), "未知"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(core.format_size(value), expected)

    def test_b09_illegal_inputs_never_raise(self) -> None:
        """非 int / 超大 / 无穷值都必须返回可读字符串，不得裸抛异常。"""
        bad = (None, "abc", object(), [], {}, complex(1, 2), -1,
               10 ** 30, float("inf"), -float("inf"), "  ", b"123")
        for value in bad:
            with self.subTest(value=str(value)[:40]):
                try:
                    result = core.format_size(value)
                except Exception as exc:  # noqa: BLE001
                    self.fail("format_size({0!r}) 抛出裸异常 {1}: {2}".format(
                        value, type(exc).__name__, exc))
                self.assertIsInstance(result, str)
                self.assertTrue(result)

    def test_b10_nan_is_an_illegal_value_should_return_unknown(self) -> None:
        """NaN 属于文档承诺的「非法值」，应返回「未知」而非抛 ValueError。

        依据 ms_downloader_core.py:309-331 的 docstring：
        ``None / 负数 / 非法值返回 "未知"``。
        """
        self.assertEqual(core.format_size(float("nan")), "未知")


class TestConfigPersistence(IsolatedCoreTestCase):
    """B4. 配置读写、损坏回退与原子写入。"""

    def test_b11_save_creates_missing_dirs_and_roundtrip_chinese_space(self) -> None:
        path = self.tmp / "配置 目录" / "子 目录" / "config.json"
        cfg = core.AppConfig(
            model_id="Qwen/Qwen2.5", target_dir="D:/我的 模型",
            revision="v1.2", endpoint="https://mirror.cn",
            proxy="http://127.0.0.1:7890", max_workers=8,
            subdir="onnx/q4", allow_patterns="*.safetensors",
            ignore_patterns="*.md", only_selected=True,
        )
        self.assertFalse(path.parent.exists())
        self.assertTrue(core.save_config(cfg, path))
        self.assertTrue(path.is_file())
        loaded = core.load_config(path)
        self.assertEqual(loaded, cfg)
        self.assertEqual(loaded.target_dir, "D:/我的 模型")
        self.assertFalse(path.with_name(path.name + ".tmp").exists(),
                         "原子写入后不应残留 .tmp")

    def test_b12_corrupted_or_wrong_type_json_falls_back_to_defaults(self) -> None:
        default = core.AppConfig()
        variants = (
            "{not json at all",
            "[1, 2, 3]",
            '"a string"',
            "null",
            "",
        )
        for index, content in enumerate(variants):
            path = self.tmp / "broken_{0}.json".format(index)
            path.write_text(content, encoding="utf-8")
            with self.subTest(content=content[:20]):
                self.assertEqual(core.load_config(path), default)

    def test_b13_illegal_field_values_are_sanitized(self) -> None:
        path = self.tmp / "weird.json"
        path.write_text(json.dumps({
            "model_id": None, "target_dir": 123, "max_workers": "abc",
            "max_workers_note": "ignored", "only_selected": "yes",
            "revision": "", "endpoint": ["a", "b"],
        }), encoding="utf-8")
        cfg = core.load_config(path)
        self.assertEqual(cfg.model_id, "")
        self.assertEqual(cfg.target_dir, "123")
        self.assertEqual(cfg.max_workers, core.DEFAULT_MAX_WORKERS)
        self.assertEqual(cfg.revision, core.DEFAULT_REVISION)
        self.assertTrue(cfg.only_selected)
        self.assertEqual(cfg.endpoint, "['a', 'b']")

        path.write_text(json.dumps({"max_workers": -5, "revision": "  "}),
                        encoding="utf-8")
        cfg2 = core.load_config(path)
        self.assertEqual(cfg2.max_workers, core.DEFAULT_MAX_WORKERS)
        self.assertEqual(cfg2.revision, core.DEFAULT_REVISION)

    def test_b14_missing_file_returns_defaults(self) -> None:
        self.assertEqual(core.load_config(self.tmp / "nope" / "config.json"),
                         core.AppConfig())

    def test_b15_atomic_replace_failure_keeps_original_file(self) -> None:
        path = self.tmp / "cfg" / "config.json"
        self.assertTrue(core.save_config(core.AppConfig(model_id="first"), path))
        original = path.read_bytes()
        with mock.patch.object(core.os, "replace", side_effect=OSError("replace failed")):
            self.assertFalse(core.save_config(core.AppConfig(model_id="second"), path))
        self.assertEqual(path.read_bytes(), original,
                         "原子写入失败时不得破坏已有配置")
        self.assertEqual(core.load_config(path).model_id, "first")

    def test_b16_get_config_path_uses_appdata_when_set(self) -> None:
        with mock.patch.dict(os.environ, {"APPDATA": str(self.tmp / "APPDATA")}):
            path = core.get_config_path()
        self.assertEqual(path.name, core.CONFIG_FILE_NAME)
        self.assertEqual(path.parent.name, core.CONFIG_DIR_NAME)
        self.assertEqual(path.parent.parent, self.tmp / "APPDATA")


class TestDataclasses(IsolatedCoreTestCase):
    """B5. RepoFile / ModelInfo 数据结构。"""

    def test_b17_repo_file_normalization(self) -> None:
        item = core.RepoFile(path="onnx\\model.onnx", size="123")
        self.assertEqual(item.path, "onnx/model.onnx")
        self.assertEqual(item.name, "model.onnx")
        self.assertEqual(item.size, 123)
        self.assertEqual(core.RepoFile("a.bin", -5).size, 0)
        self.assertEqual(core.RepoFile("a.bin", None).size, 0)
        self.assertEqual(core.RepoFile("a.bin", "456").size, 456)
        self.assertEqual(core.RepoFile("dir/b.bin", 1, name="custom").name, "custom")
        # 生产路径：非法 Size 由 _parse_file_entry -> _to_int 兜底为 0，
        # 不会把脏值传进 RepoFile 构造器。
        parsed = core._parse_file_entry({"Path": "x.bin", "Size": "not-a-number"})
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.size, 0)

    def test_b18_model_info_aggregates(self) -> None:
        info = core.ModelInfo(
            model_id="Qwen/x", revision="v1",
            files=[core.RepoFile("b.bin", 20), core.RepoFile("a.bin", 10)],
            source="REST",
        )
        self.assertEqual(info.total_size, 30)
        self.assertEqual(info.file_count, 2)
        self.assertEqual(info.paths, ["b.bin", "a.bin"])
        self.assertEqual(core.ModelInfo("Qwen/x").total_size, 0)


# ===========================================================================
# C 组：文件清单查询（全 mock _http_get_json）
# ===========================================================================

class TestRepoFileQuery(IsolatedCoreTestCase):
    """C. SDK HubApi / REST 双路径清单查询。"""

    NORMAL_PAYLOAD = {
        "Code": 200,
        "Data": {"Files": [
            {"Path": "config.json", "Size": 123, "Type": "blob"},
            {"Path": "model.safetensors", "Size": 1073741824,
             "Type": "blob", "IsLFS": True},
            {"Path": "onnx/model.onnx", "Size": 456, "Type": "blob"},
            {"Path": ".gitattributes", "Size": 88, "Type": "blob"},
            {"Path": "onnx", "Type": "tree"},
        ]},
    }

    def test_c01_rest_normal_response_filters_tree_keeps_gitattributes(self) -> None:
        calls = []

        def fake_http(url, proxy, timeout):
            calls.append((url, proxy, timeout))
            return self.NORMAL_PAYLOAD

        with mock.patch.object(core, "_http_get_json", side_effect=fake_http):
            info = core.fetch_repo_files("Qwen/Qwen2.5-7B-Instruct",
                                         revision="v1.0", prefer_sdk=False)
        self.assertEqual(info.source, "REST")
        self.assertEqual(info.model_id, "Qwen/Qwen2.5-7B-Instruct")
        self.assertEqual(info.revision, "v1.0")
        self.assertEqual(info.paths,
                         [".gitattributes", "config.json", "model.safetensors",
                          "onnx/model.onnx"])
        self.assertEqual(info.file_count, 4)
        self.assertEqual(info.total_size, 123 + 1073741824 + 456 + 88)
        self.assertTrue(any(f.is_lfs for f in info.files))
        self.assertEqual(len(calls), 1)
        self.assertIn("/repo/files?", calls[0][0])
        self.assertIn("Recursive=True", calls[0][0])
        self.assertIn("Revision=v1.0", calls[0][0])

    def test_c02_field_name_variants(self) -> None:
        """大小写、字符串 Size、size 别名、路径别名、Data 为 list 等。"""
        variants = (
            ({"code": 200, "data": {"files": [
                {"path": "b.bin", "size": "1024", "type": "blob"}]}},
             ["b.bin"], 1024),
            ({"Data": {"Files": [
                {"Path": "c.bin", "Length": 7},
                {"Path": "d.bin", "Bytes": 8},
                {"Path": "e.bin", "FileSize": 9}]}},
             ["c.bin", "d.bin", "e.bin"], 24),
            ({"Data": {"Files": [
                {"Name": "g.bin"}, {"FilePath": "h.bin"},
                {"Key": "i.bin"}, {"RelativePath": "j.bin"}]}},
             ["g.bin", "h.bin", "i.bin", "j.bin"], 0),
            ({"Data": [{"Path": "k.bin", "Size": 1}]}, ["k.bin"], 1),
            ([{"Path": "m.bin", "Size": 1}], ["m.bin"], 1),
            (["n.bin", "o.bin"], ["n.bin", "o.bin"], 0),
            ({"Data": {"FileList": [{"Path": "p.bin", "Size": 3}]}},
             ["p.bin"], 3),
            # 源码第 792 行声明支持的顶层 Files 变体
            ({"Code": 200, "Files": [
                {"Path": "q.bin", "Size": 5, "Type": "blob"}]},
             ["q.bin"], 5),
        )
        for index, (payload, paths, total) in enumerate(variants):
            with self.subTest(index=index):
                info = core._parse_file_entries(payload, "REST", "Qwen/x", "master")
                self.assertEqual(info.paths, paths)
                self.assertEqual(info.total_size, total)

    def test_c03_tree_entries_and_trailing_slash_are_dropped(self) -> None:
        payload = {"Data": {"Files": [
            {"Path": "onnx", "Type": "tree"},
            {"Path": "docs/", "Size": 0, "Type": "blob"},
            {"Path": "sub", "Type": "directory"},
            {"Path": "folder", "Type": "folder"},
            {"Path": "dir", "Type": "dir"},
            {"Path": ".gitattributes", "Size": 2, "Type": "blob"},
            {"Path": "real.bin", "Size": 11, "Type": "blob"},
        ]}}
        info = core._parse_file_entries(payload, "REST", "Qwen/x", "master")
        self.assertEqual(info.paths, [".gitattributes", "real.bin"])

    def test_c04_top_level_files_variant_end_to_end(self) -> None:
        """REST 返回顶层 Files 时，fetch_repo_files 必须能拿到文件清单。

        对应源码 ms_downloader_core.py:791-792 的兼容回退分支。
        """
        payload = {"Code": 200, "Files": [
            {"Path": "config.json", "Size": 10, "Type": "blob"},
            {"Path": "model.safetensors", "Size": 20, "Type": "blob"},
            {"Path": "onnx/model.onnx", "Size": 30, "Type": "blob"},
        ]}
        with mock.patch.object(core, "_http_get_json", return_value=payload):
            info = core.fetch_repo_files("Qwen/x", prefer_sdk=False)
        self.assertEqual(info.file_count, 3)
        self.assertEqual(info.paths,
                         ["config.json", "model.safetensors", "onnx/model.onnx"])

    def test_c05_code_error_tries_next_candidate_url(self) -> None:
        urls = []

        def fake_http(url, proxy, timeout):
            urls.append(url)
            if len(urls) == 1:
                return {"Code": 500, "Message": "internal error"}
            return {"Code": 200, "Data": {"Files": [
                {"Path": "ok.bin", "Size": 1, "Type": "blob"}]}}

        with mock.patch.object(core, "_http_get_json", side_effect=fake_http):
            info = core.fetch_repo_files("Qwen/x", prefer_sdk=False)
        self.assertEqual(len(urls), 2)
        self.assertIn("/repo/files?", urls[0])
        self.assertIn("/repo/tree?", urls[1])
        self.assertEqual(info.paths, ["ok.bin"])

    def test_c06_success_false_tries_next_candidate_url(self) -> None:
        urls = []

        def fake_http(url, proxy, timeout):
            urls.append(url)
            if len(urls) == 1:
                return {"Success": False, "Message": "denied"}
            return {"Data": {"Files": [{"Path": "ok.bin", "Size": 2}]}}

        with mock.patch.object(core, "_http_get_json", side_effect=fake_http):
            info = core.fetch_repo_files("Qwen/x", prefer_sdk=False)
        self.assertEqual(len(urls), 2)
        self.assertEqual(info.paths, ["ok.bin"])

    def test_c07_all_rest_urls_fail_raises_repo_query_error(self) -> None:
        with mock.patch.object(core, "_http_get_json",
                               side_effect=OSError("network down")):
            with self.assertRaises(core.RepoQueryError) as ctx:
                core.fetch_repo_files("Qwen/x", prefer_sdk=False)
        self.assertIn("network down", str(ctx.exception))

    def test_c08_sdk_and_rest_both_fail_error_mentions_both(self) -> None:
        with mock.patch.object(core, "sdk_is_installed", return_value=True), \
             mock.patch.object(core, "is_sdk_available", return_value=True), \
             mock.patch.object(core, "_fetch_via_sdk",
                               side_effect=RuntimeError("SDK boom")), \
             mock.patch.object(core, "_http_get_json",
                               side_effect=OSError("REST down")):
            with self.assertRaises(core.RepoQueryError) as ctx:
                core.fetch_repo_files("Qwen/x", prefer_sdk=True)
        message = str(ctx.exception)
        self.assertIn("SDK boom", message)
        self.assertIn("REST down", message)

    def test_c09_sdk_hubapi_path_via_fake_module(self) -> None:
        """注入假的 modelscope.hub.api 模块，验证 SDK 优先路径。"""
        fake_module = types.ModuleType("modelscope.hub.api")
        fake_module.__spec__ = importlib.machinery.ModuleSpec(
            "modelscope.hub.api", loader=None)
        calls = []

        class FakeHubApi:
            def __init__(self, *args, **kwargs):
                calls.append(("init", args, kwargs))

            def get_model_files(self, **kwargs):
                calls.append(("get_model_files", kwargs))
                return {"Data": {"Files": [
                    {"Path": "sdk.bin", "Size": 42, "Type": "blob"}]}}

        fake_module.HubApi = FakeHubApi
        real_import = core.importlib.import_module

        def fake_import(name, *args, **kwargs):
            if name == "modelscope.hub.api":
                return fake_module
            return real_import(name, *args, **kwargs)

        with mock.patch.object(core.importlib, "import_module", side_effect=fake_import), \
             mock.patch.object(core, "sdk_is_installed", return_value=True), \
             mock.patch.object(core, "is_sdk_available", return_value=True):
            info = core.fetch_repo_files("Qwen/x", prefer_sdk=True)
        self.assertEqual(info.source, "SDK")
        self.assertEqual(info.paths, ["sdk.bin"])
        self.assertEqual(info.total_size, 42)
        self.assertTrue(any(c[0] == "get_model_files" for c in calls))

    def test_c10_injected_sdk_cache_short_circuits_real_import(self) -> None:
        """注入 _SDK_CACHE 后，is_sdk_available 不得再真正 import modelscope。"""
        self.use_fake_sdk(lambda **kw: None, version="1.20.0")
        fake_info = core.ModelInfo(
            model_id="Qwen/x", files=[core.RepoFile("cached.bin", 7)],
            source="SDK")
        with mock.patch.object(core, "sdk_is_installed", return_value=True), \
             mock.patch.object(core.importlib, "import_module",
                               wraps=core.importlib.import_module) as import_spy, \
             mock.patch.object(core, "_fetch_via_sdk",
                               return_value=fake_info) as sdk_mock:
            info = core.fetch_repo_files("Qwen/x", prefer_sdk=True)
        self.assertIs(info, fake_info)
        sdk_mock.assert_called_once()
        import_spy.assert_not_called()

    def test_c11_validation_and_cancel_short_circuit_network(self) -> None:
        with self.assertRaises(core.ValidationError):
            core.fetch_repo_files("bad", prefer_sdk=False)
        event = threading.Event()
        event.set()
        with mock.patch.object(core, "_http_get_json") as http_mock:
            with self.assertRaises(core.DownloadCancelled):
                core.fetch_repo_files("Qwen/x", prefer_sdk=False,
                                      cancel_event=event)
        http_mock.assert_not_called()

    def test_c12_build_files_api_urls(self) -> None:
        urls = core._build_files_api_urls("Qwen/x", "v1.0", "")
        self.assertEqual(len(urls), 2)
        self.assertTrue(all("/api/v1/models/Qwen/x/repo/" in u for u in urls))
        self.assertTrue(all("Revision=v1.0" in u and "Recursive=True" in u
                            for u in urls))
        urls2 = core._build_files_api_urls("Qwen/x", "master",
                                           "https://mirror.cn/api/v1/")
        self.assertNotIn("/api/v1/api/v1", urls2[0])
        self.assertTrue(urls2[0].startswith("https://mirror.cn/api/v1/models/"))

    def test_c13_sdk_unavailable_falls_back_to_rest(self) -> None:
        """本机未装 SDK 时，prefer_sdk=True 也应直接走 REST。"""
        self.use_no_sdk()
        with mock.patch.object(core, "_http_get_json",
                               return_value=self.NORMAL_PAYLOAD) as http_mock:
            info = core.fetch_repo_files("Qwen/x", prefer_sdk=True)
        self.assertEqual(info.source, "REST")
        self.assertEqual(info.file_count, 4)
        http_mock.assert_called()


# ===========================================================================
# D 组：下载三条路径（fake SDK 注入 _SDK_CACHE）
# ===========================================================================

MODEL_FILES = {
    "config.json": b'{"arch": "qa"}',
    "model.safetensors": b"weights-" + b"w" * 4096,
    "onnx/model.onnx": b"onnx-payload",
    ".gitattributes": b"*.safetensors filter=lfs",
}


def _write_tree(root, files=MODEL_FILES):
    """在指定根目录写出模拟模型文件树，返回目录字符串。"""
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    for relative, data in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return str(path)


class _RecordingPoller:
    """替换真实进度轮询线程，避免测试产生后台守护线程。"""

    def __init__(self, *args, **kwargs):
        self.roots = kwargs.get("roots", [])
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class TestDownloadPaths(IsolatedCoreTestCase):
    """D. local_dir 直写 / TypeError 回退 / **kwargs 静默吞掉 local_dir 兜底。"""

    def _run_download(self, func, target, **kwargs):
        """统一注入 fake SDK 并调用 download_model（不联网、不真下载）。"""
        self.use_fake_sdk(func)
        logs, progress = [], []
        with mock.patch.object(
            core, "get_config_path",
            return_value=self.tmp / "config" / "config.json"), \
             mock.patch.object(core, "_try_build_sdk_progress_callback",
                               return_value=None), \
             mock.patch.object(core, "_ProgressPoller", _RecordingPoller):
            result = core.download_model(
                model_id="Qwen/Qwen2.5-7B-Instruct",
                target_dir=target,
                revision="master",
                allow_patterns=kwargs.pop("allow_patterns", ["*.safetensors"]),
                ignore_patterns=kwargs.pop("ignore_patterns", ["*.md"]),
                max_workers=4,
                total_bytes=1024,
                apply_env=False,
                progress_interval=0.2,
                log_cb=logs.append,
                progress_cb=lambda done, total, msg: progress.append(
                    (done, total, msg)),
                **kwargs,
            )
        return result, logs, progress

    def _assert_files_landed(self, target):
        """目标文件夹内应有完整模型文件且保留目录结构。"""
        for relative, data in MODEL_FILES.items():
            path = target / relative
            with self.subTest(relative=relative):
                self.assertTrue(path.is_file(), "目标文件夹缺少 {0}".format(relative))
                self.assertEqual(path.read_bytes(), data)
        self.assertTrue((target / "onnx").is_dir(), "子目录结构丢失")
        self.assertGreater(core.dir_size_bytes(target), 0)

    def test_d01_new_sdk_local_dir_writes_directly(self) -> None:
        calls = []

        def sdk_new(model_id, revision="master", local_dir=None, cache_dir=None,
                    allow_patterns=None, ignore_patterns=None, max_workers=None,
                    endpoint=None, progress_callbacks=None, repo_type=None,
                    token=None, user_agent=None):
            calls.append({
                "model_id": model_id, "local_dir": local_dir,
                "cache_dir": cache_dir, "allow_patterns": allow_patterns,
                "ignore_patterns": ignore_patterns,
            })
            return _write_tree(local_dir)

        target = self.tmp / "我的 模型 目录" / "Qwen 权重"
        result, logs, progress = self._run_download(sdk_new, target)

        self.assertEqual(Path(result).resolve(), target.resolve())
        self._assert_files_landed(target)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["local_dir"], str(target))
        self.assertIsNone(calls[0]["cache_dir"])
        self.assertEqual(calls[0]["allow_patterns"], ["*.safetensors"])
        self.assertEqual(calls[0]["ignore_patterns"], ["*.md"])
        self.assertTrue(any("local_dir" in line for line in logs))
        self.assertTrue(progress)
        self.assertEqual(progress[-1][2], "下载完成")

    def test_d02_runtime_typeerror_switches_to_cache_dir_fallback(self) -> None:
        calls = []

        def sdk_runtime_reject(model_id, revision="master", **kwargs):
            calls.append(dict(kwargs))
            if "local_dir" in kwargs:
                raise TypeError(
                    "snapshot_download() got an unexpected keyword argument "
                    "'local_dir'")
            return _write_tree(kwargs.get("cache_dir"))

        target = self.tmp / "回退 目标 文件夹" / "带 空格"
        result, logs, progress = self._run_download(sdk_runtime_reject, target)

        self.assertEqual(Path(result).resolve(), target.resolve())
        self._assert_files_landed(target)
        self.assertGreaterEqual(len(calls), 2)
        self.assertIn("local_dir", calls[0])
        self.assertNotIn("local_dir", calls[-1])
        self.assertIn("cache_dir", calls[-1])
        self.assertTrue(any("cache_dir" in line for line in logs))
        self.assertEqual(progress[-1][2], "下载完成")

    def test_d03_kwargs_sdk_silently_swallows_local_dir_is_recovered(self) -> None:
        """重点复验：带 **kwargs 的旧版静默吞掉 local_dir 时仍须落到目标文件夹。"""
        calls = []
        private_cache = self.tmp / "sdk_private_cache"

        def sdk_swallow(model_id, revision="master", **kwargs):
            calls.append(dict(kwargs))
            # 故意完全忽略 local_dir，下载到 SDK 自己的缓存目录
            return _write_tree(private_cache)

        target = self.tmp / "静默 吞掉" / "local_dir 目标"
        result, logs, progress = self._run_download(sdk_swallow, target)

        self.assertEqual(Path(result).resolve(), target.resolve())
        self._assert_files_landed(target)
        self.assertIn("local_dir", calls[-1], "测试前提：local_dir 确实传给了 SDK")
        self.assertTrue(any("local_dir 未生效" in line or "回退路径" in line
                            for line in logs),
                        "日志未体现运行时兜底：{0}".format(logs))
        self.assertNotEqual(private_cache.resolve(), target.resolve())
        self.assertEqual(progress[-1][2], "下载完成")

    def test_d04_sdk_returns_none_must_fail_loudly(self) -> None:
        """边界：SDK 返回 None 且未落盘时必须明确失败，不得谎报下载成功。

        最新版源码在 download_model 中把这种「静默成功」改为抛
        DownloadError 并给出升级 SDK 指引，符合 PRD「必须下载到该文件夹内」
        的硬要求。
        """

        def sdk_returns_none(model_id, revision="master", **kwargs):
            return None

        target = self.tmp / "空 返回 目录"
        with self.assertRaises(core.DownloadError) as ctx:
            self._run_download(sdk_returns_none, target)
        message = str(ctx.exception)
        self.assertIn(str(target), message)
        self.assertTrue("升级" in message or "检查" in message,
                        "错误信息应给出可操作的排查指引：{0}".format(message))
        files = ([p for p in target.rglob("*") if p.is_file()]
                 if target.exists() else [])
        self.assertEqual(files, [], "失败时不应留下伪装成已完成的模型文件")

    def test_d05_allow_ignore_patterns_reach_sdk(self) -> None:
        seen = {}

        def sdk_new(model_id, revision="master", local_dir=None,
                    allow_patterns=None, ignore_patterns=None, **kwargs):
            seen["allow"] = allow_patterns
            seen["ignore"] = ignore_patterns
            return _write_tree(local_dir)

        target = self.tmp / "规则 目录"
        self._run_download(
            sdk_new, target,
            allow_patterns=["*.safetensors", "*.json", "onnx/*"],
            ignore_patterns=["*.md", "*.png"],
        )
        self.assertEqual(seen["allow"], ["*.safetensors", "*.json", "onnx/*"])
        self.assertEqual(seen["ignore"], ["*.md", "*.png"])


# ===========================================================================
# E 组：断点续传 / 取消 / 进度
# ===========================================================================

class _FlipEvent:
    """前 N 次 is_set() 返回 False，之后返回 True，用于模拟复制中途取消。"""

    def __init__(self, set_after: int):
        self.count = 0
        self.limit = set_after

    def is_set(self) -> bool:
        self.count += 1
        return self.count > self.limit


class TestResumeCancelProgress(IsolatedCoreTestCase):
    """E. 跳过已存在 / 重新复制截断文件 / 元数据过滤 / 取消 / 进度。"""

    def _make_cache(self):
        cache = self.tmp / "cache"
        _write_tree(cache, {
            "config.json": b"config-data",
            "model.safetensors": b"W" * 2048,
            "onnx/model.onnx": b"onnx-data",
            ".gitattributes": b"lfs",
            "docs/readme.md": b"docs",
        })
        return cache

    def test_e01_second_sync_copies_zero_when_sizes_match(self) -> None:
        cache, target = self._make_cache(), self.tmp / "target"
        first = core.sync_cache_to_target(cache, target)
        self.assertEqual(first, 5)
        second = core.sync_cache_to_target(cache, target)
        self.assertEqual(second, 0, "第二轮应跳过全部已存在且大小一致的文件")
        self.assertEqual(core.dir_size_bytes(target), core.dir_size_bytes(cache))

    def test_e02_truncated_target_file_is_re_copied(self) -> None:
        cache, target = self._make_cache(), self.tmp / "target"
        self.assertEqual(core.sync_cache_to_target(cache, target), 5)
        truncated = target / "model.safetensors"
        truncated.write_bytes(b"short")
        again = core.sync_cache_to_target(cache, target)
        self.assertGreaterEqual(again, 1, "被截断的文件应重新复制")
        self.assertEqual(truncated.read_bytes(), b"W" * 2048)
        self.assertEqual(core.sync_cache_to_target(cache, target), 0)

    def test_e03_metadata_dirs_and_temp_files_are_skipped(self) -> None:
        cache = self.tmp / "cache"
        _write_tree(cache, {
            "model.safetensors": b"weights",
            ".gitattributes": b"lfs",
            "onnx/model.onnx": b"onnx",
            ".cache/meta.json": b"meta",
            ".git/config": b"git",
            "blobs/aaa": b"blob",
            "locks/l": b"lock",
            ".locks/x": b"lock",
            "a.lock": b"lock",
            "b.part": b"part",
            "c.tmp": b"tmp",
            "d._____temp": b"temp",
        })
        target = self.tmp / "target"
        copied = core.sync_cache_to_target(cache, target)
        self.assertEqual(copied, 3)
        landed = sorted(str(p.relative_to(target)).replace("\\", "/")
                        for p in target.rglob("*") if p.is_file())
        self.assertEqual(landed, [".gitattributes", "model.safetensors",
                                 "onnx/model.onnx"])
        self.assertFalse((target / ".git").exists())
        self.assertFalse((target / "blobs").exists())
        self.assertFalse(list(target.rglob("*.part")))

    def test_e04_cancel_before_sync_writes_no_files(self) -> None:
        cache = self._make_cache()
        target = self.tmp / "target"
        event = threading.Event()
        event.set()
        with self.assertRaises(core.DownloadCancelled):
            core.sync_cache_to_target(cache, target, cancel_event=event)
        files = [p for p in target.rglob("*") if p.is_file()] if target.exists() else []
        self.assertEqual(files, [], "取消后目标文件夹不应出现任何文件")

    def test_e05_cancel_mid_copy_leaves_no_part_file(self) -> None:
        cache = self.tmp / "cache"
        big = cache / "big.bin"
        big.parent.mkdir(parents=True, exist_ok=True)
        big.write_bytes(b"x" * (4 * 1024 * 1024))
        target = self.tmp / "target"
        event = _FlipEvent(set_after=2)
        with self.assertRaises(core.DownloadCancelled):
            core.sync_cache_to_target(cache, target, cancel_event=event)
        self.assertEqual(list(target.rglob("*.part")), [],
                         "取消后不得残留 .part 半截文件")
        self.assertFalse((target / "big.bin").exists(),
                         "取消后不得留下被误判为已完成的半截文件")

    def test_e06_copy_file_with_cancel_cleans_temp_on_cancel(self) -> None:
        cache = self.tmp / "cache"
        cache.mkdir()
        src = cache / "src.bin"
        src.write_bytes(b"y" * (3 * 1024 * 1024))
        dst = self.tmp / "target" / "dst.bin"
        dst.parent.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(core.DownloadCancelled):
            core._copy_file_with_cancel(src, dst, _FlipEvent(set_after=1))
        self.assertFalse(dst.exists())
        self.assertFalse(dst.with_name(dst.name + ".part").exists())

    def test_e07_download_model_cancel_before_start(self) -> None:
        self.use_unchecked_sdk()
        target = self.tmp / "从未创建" / "target"
        event = threading.Event()
        event.set()
        with mock.patch.object(core, "get_config_path",
                               return_value=self.tmp / "config" / "config.json"):
            with self.assertRaises(core.DownloadCancelled):
                core.download_model("Qwen/x", target, cancel_event=event,
                                    apply_env=False)
        files = [p for p in target.rglob("*") if p.is_file()] if target.exists() else []
        self.assertEqual(files, [], "取消后不得产生任何文件写入")

    def test_e08_dir_size_bytes(self) -> None:
        root = self.tmp / "sized"
        (root / "sub").mkdir(parents=True)
        (root / "a.bin").write_bytes(b"12345")
        (root / "sub" / "b.bin").write_bytes(b"1234567")
        self.assertEqual(core.dir_size_bytes(root), 12)
        self.assertEqual(core.dir_size_bytes(self.tmp / "missing"), 0)
        self.assertEqual(core.dir_size_bytes(root / "a.bin"), 0)

    def test_e09_progress_poller_reports_bytes(self) -> None:
        root = self.tmp / "poll"
        root.mkdir()
        (root / "a.bin").write_bytes(b"x" * 100)
        received = threading.Event()
        records = []

        def reporter(done, total, message):
            records.append((done, total, message))
            received.set()

        poller = core._ProgressPoller([root], 1000, None, reporter, interval=0.2)
        poller.start()
        try:
            self.assertTrue(received.wait(3.0), "轮询线程未上报进度")
            self.assertEqual(records[0][0], 100)
            self.assertEqual(records[0][1], 1000)
        finally:
            poller.stop()

    def test_e10_progress_poller_interval_floor_and_cancel(self) -> None:
        noop = lambda *_args: None  # noqa: E731
        self.assertEqual(
            core._ProgressPoller([], 0, None, noop, interval=0.01).interval, 0.2)
        root = self.tmp / "poll2"
        root.mkdir()
        (root / "a.bin").write_bytes(b"123")
        records = []
        event = threading.Event()
        event.set()
        poller = core._ProgressPoller(
            [root], 100, event,
            lambda d, t, m: records.append((d, t, m)), interval=0.2)
        poller.start()
        time.sleep(0.5)
        poller.stop()
        self.assertEqual(records, [], "取消事件置位时不应继续上报进度")


# ===========================================================================
# F 组：依赖缺失与异常分层
# ===========================================================================

class TestDependencyAndExceptions(IsolatedCoreTestCase):
    """F. 异常继承、SDK 缺失、新旧参数名映射与重试。"""

    def test_f01_exception_hierarchy(self) -> None:
        self.assertTrue(issubclass(core.DownloaderError, Exception))
        for cls in (core.ValidationError, core.DependencyError,
                    core.RepoQueryError, core.DownloadError,
                    core.DownloadCancelled):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(issubclass(cls, core.DownloaderError))
                self.assertTrue(issubclass(cls, Exception))
        self.assertFalse(issubclass(core.ValidationError, core.DependencyError))

    def test_f02_missing_sdk_raises_dependency_error_not_module_not_found(self) -> None:
        """SDK 真实导入失败时必须包装成 DependencyError（与本机是否装 SDK 无关）。

        旧实现用 ``use_unchecked_sdk()`` 让 ``_load_sdk()`` 去真实 import，并硬
        依赖「本机未装 modelscope」这一环境前提。在装了 SDK 的 venv 里它会真的
        发起网络请求并以 DownloadError 结束（既慢又假失败）。这里显式模拟导入
        失败，精确覆盖「导入失败 -> DependencyError」这一被测契约。
        """
        self.use_unchecked_sdk()
        real_import = core.importlib.import_module

        def fail_import(name, *args, **kwargs):
            if name.startswith("modelscope"):
                raise ImportError("QA: 模拟 modelscope 导入失败")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(core.importlib, "import_module",
                               side_effect=fail_import), \
                mock.patch.object(core, "get_config_path",
                                  return_value=self.tmp / "config" / "config.json"):
            with self.assertRaises(core.DependencyError) as ctx:
                core.download_model("Qwen/x", self.tmp / "target",
                                    apply_env=False)
        self.assertIn(core.INSTALL_COMMAND, str(ctx.exception))
        self.assertIn("pip install -r requirements.txt", str(ctx.exception))

    def test_f03_cached_unavailable_sdk_raises_dependency_error(self) -> None:
        self.use_no_sdk()
        with mock.patch.object(core, "get_config_path",
                               return_value=self.tmp / "config" / "config.json"):
            with self.assertRaises(core.DependencyError):
                core.download_model("Qwen/x", self.tmp / "target",
                                    apply_env=False)

    def test_f04_build_download_kwargs_local_vs_cache(self) -> None:
        kwargs = core.build_download_kwargs(
            "Qwen/x", target_dir="D:/我的 模型",
            allow_patterns=["*.safetensors"], ignore_patterns=["*.md"],
            max_workers=8, endpoint="https://mirror.cn/",
        )
        self.assertEqual(kwargs["model_id"], "Qwen/x")
        self.assertEqual(kwargs["revision"], core.DEFAULT_REVISION)
        self.assertEqual(kwargs["local_dir"], "D:/我的 模型")
        self.assertNotIn("cache_dir", kwargs)
        self.assertEqual(kwargs["allow_patterns"], ["*.safetensors"])
        self.assertEqual(kwargs["allow_file_pattern"], ["*.safetensors"])
        self.assertEqual(kwargs["ignore_patterns"], ["*.md"])
        self.assertEqual(kwargs["ignore_file_pattern"], ["*.md"])
        self.assertEqual(kwargs["max_workers"], 8)
        self.assertEqual(kwargs["endpoint"], "https://mirror.cn")

        cached = core.build_download_kwargs(
            "Qwen/x", target_dir="D:/t", cache_dir="D:/c",
            supports_local_dir=False, max_workers=0)
        self.assertIn("cache_dir", cached)
        self.assertNotIn("local_dir", cached)
        self.assertNotIn("max_workers", cached)
        self.assertEqual(cached["cache_dir"], "D:/c")

        with self.assertRaises(core.ValidationError):
            core.build_download_kwargs("bad")

    def test_f05_filter_kwargs_new_old_name_mapping(self) -> None:
        def new_style(model_id, local_dir=None, allow_patterns=None,
                      ignore_patterns=None, **kwargs):
            pass

        def old_style(model_id, cache_dir=None, allow_file_pattern=None,
                      ignore_file_pattern=None, **kwargs):
            pass

        def strict(model_id):
            pass

        def both(model_id, allow_patterns=None, allow_file_pattern=None):
            pass

        base = {
            "model_id": "a", "local_dir": "D:/t",
            "allow_patterns": ["*.a"], "allow_file_pattern": ["*.a"],
            "ignore_patterns": ["*.b"], "ignore_file_pattern": ["*.b"],
            "max_workers": 4, "endpoint": "e",
        }
        new = core._filter_kwargs(new_style, dict(base))
        self.assertIn("allow_patterns", new)
        self.assertNotIn("allow_file_pattern", new)
        self.assertIn("ignore_patterns", new)
        self.assertNotIn("ignore_file_pattern", new)

        # 带 **kwargs 且显式声明旧名的旧版 SDK：必须改用旧名，避免规则被吞掉
        old = core._filter_kwargs(old_style, dict(base))
        self.assertIn("allow_file_pattern", old)
        self.assertNotIn("allow_patterns", old)
        self.assertIn("ignore_file_pattern", old)
        self.assertNotIn("ignore_patterns", old)

        self.assertEqual(core._filter_kwargs(strict, dict(base)), {"model_id": "a"})
        both_result = core._filter_kwargs(both, dict(base))
        self.assertIn("allow_patterns", both_result)
        self.assertNotIn("allow_file_pattern", both_result)

    def test_f06_probe_local_dir_support_variants(self) -> None:
        def with_local(model_id, local_dir=None):
            pass

        def without_local(model_id, cache_dir=None):
            pass

        def old_kwargs(model_id, cache_dir=None, **kwargs):
            pass

        def pure_kwargs(model_id, **kwargs):
            pass

        self.assertTrue(core.probe_local_dir_support(with_local))
        self.assertFalse(core.probe_local_dir_support(without_local))
        self.assertFalse(core.probe_local_dir_support(old_kwargs),
                         "带 **kwargs 且声明 cache_dir 的旧版必须判定为不支持")
        self.assertTrue(core.probe_local_dir_support(pure_kwargs),
                        "纯 **kwargs 代理层应乐观返回 True 交给运行时兜底")
        self.use_no_sdk()
        self.assertFalse(core.probe_local_dir_support())

    def test_f07_call_snapshot_download_strips_optional_kwargs_one_by_one(self) -> None:
        calls = []

        def picky(**kwargs):
            calls.append(dict(kwargs))
            for key in ("progress_callbacks", "endpoint", "max_workers"):
                if key in kwargs:
                    raise TypeError(
                        "picky() got an unexpected keyword argument '{0}'".format(key))
            return "OK"

        logs = []
        kwargs = {
            "model_id": "Qwen/x", "allow_patterns": ["*.a"],
            "progress_callbacks": [object()], "endpoint": "e", "max_workers": 4,
        }
        self.assertEqual(core._call_snapshot_download(picky, kwargs, logs.append), "OK")
        self.assertEqual(len(calls), 4, "应逐个剔除 3 个可选参数后第 4 次成功")
        self.assertEqual(set(calls[-1]), {"model_id", "allow_patterns"})
        self.assertEqual(len(logs), 3)
        self.assertTrue(all("已自动移除后重试" in line for line in logs))

    def test_f08_local_dir_typeerror_is_not_stripped(self) -> None:
        """local_dir 的 TypeError 必须向上抛，交给 download_model 切换策略。"""
        def reject(**kwargs):
            raise TypeError(
                "snapshot_download() got an unexpected keyword argument 'local_dir'")

        with self.assertRaises(TypeError) as ctx:
            core._call_snapshot_download(
                reject, {"local_dir": "D:/t", "max_workers": 4}, lambda _m: None)
        self.assertIn("local_dir", str(ctx.exception))

    def test_f09_sdk_helpers_when_not_installed(self) -> None:
        """SDK 不可用时四个辅助函数的返回值（与本机是否安装 SDK 解耦）。

        旧实现直接断言 ``sdk_is_installed()`` 为 False 并注明「本机实测未安装
        modelscope」，是硬编码环境假设：该函数用 ``importlib.util.find_spec``，
        在装了 modelscope 的 venv 里必然返回 True（源码行为正确，是用例错了）。
        这里显式 mock ``find_spec`` 覆盖两个分支，并显式模拟导入失败来覆盖
        其余三个 helper，保证在任意解释器下都测到同一个契约。
        """
        with mock.patch.object(core.importlib.util, "find_spec",
                               return_value=None):
            self.assertFalse(core.sdk_is_installed())
        with mock.patch.object(core.importlib.util, "find_spec",
                               return_value=object()):
            self.assertTrue(core.sdk_is_installed())

        self.use_unchecked_sdk()
        real_import = core.importlib.import_module

        def fail_import(name, *args, **kwargs):
            if name.startswith("modelscope"):
                raise ImportError("QA: 模拟 modelscope 导入失败")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(core.importlib, "import_module",
                               side_effect=fail_import):
            self.assertFalse(core.is_sdk_available())
            self.assertIsNone(core.get_snapshot_download())
            self.assertEqual(core.sdk_version(), "")
            self.assertTrue(core._SDK_CACHE["checked"])

    def test_f10_apply_environment(self) -> None:
        applied = core.apply_environment("https://mirror.cn/",
                                         "http://127.0.0.1:7890")
        self.assertEqual(applied, {"endpoint": "https://mirror.cn",
                                   "proxy": "http://127.0.0.1:7890"})
        self.assertEqual(os.environ["MODELSCOPE_ENDPOINT"], "https://mirror.cn")
        self.assertEqual(os.environ["MODELSCOPE_API_ENDPOINT"], "https://mirror.cn")
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                    "ALL_PROXY", "all_proxy"):
            self.assertEqual(os.environ[key], "http://127.0.0.1:7890")
        self.assertEqual(core.apply_environment(), {}, "空输入不应写入环境变量")

    def test_f11_log_and_progress_wrappers_never_raise(self) -> None:
        def bad_callback(*_args):
            raise RuntimeError("UI closed")

        logger = core._make_logger(bad_callback)
        reporter = core._make_progress(bad_callback)
        logger("消息")
        reporter(1, 2, "进度")
        core._make_logger(None)("x")
        core._make_progress(None)(1, 2, "x")

    def test_f12_strip_unexpected_kwarg_regex(self) -> None:
        kwargs = {"max_workers": 4, "endpoint": "e"}
        exc = TypeError("f() got an unexpected keyword argument 'max_workers'")
        self.assertEqual(
            core._strip_unexpected_kwarg(exc, kwargs, ("max_workers", "endpoint")),
            "max_workers")
        self.assertNotIn("max_workers", kwargs)
        self.assertIsNone(
            core._strip_unexpected_kwarg(
                TypeError("f() got an unexpected keyword argument 'local_dir'"),
                {"local_dir": "x"}, ("max_workers",)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
