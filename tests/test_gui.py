# -*- coding: utf-8 -*-
"""ModelScope 下载器 —— tkinter GUI 冒烟测试（G 组）。

约束：
    * 只在带 tkinter 的解释器上执行（本机为系统 Python 3.14.6 / Tk 8.6）；
    * ``root.withdraw()`` 不弹窗，``root.after`` 注册兜底 destroy 防止挂死；
    * 所有 messagebox / filedialog 均被 mock，不弹任何对话框；
    * patch ``core.download_model`` / ``core.fetch_repo_files``，
      **不启动任何真实下载线程、不联网**；
    * 配置路径重定向到临时目录，不污染用户 %APPDATA%。
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import tkinter as tk  # noqa: F401
    HAS_TK = True
except Exception:  # 便携版 Python 3.13 无 tkinter
    HAS_TK = False
    tk = None

import ms_downloader_core as core  # noqa: E402

if HAS_TK:
    import gui_app  # noqa: E402
else:
    gui_app = None


@unittest.skipUnless(HAS_TK, "需要带 tkinter 的解释器（本机为 Python 3.14.6）")
class TestGuiSmoke(unittest.TestCase):
    """G. 主窗口构造、依赖缺失提示、按钮状态机、进度条、Treeview。"""

    def setUp(self) -> None:
        tmp_obj = tempfile.TemporaryDirectory(prefix="msqa_gui_")
        self.tmp = Path(tmp_obj.name)
        config_path = self.tmp / "APPDATA" / "ModelScopeDownloader" / "config.json"

        # 隔离 SDK 缓存：让后台探测线程立即返回「不可用」，不真正 import
        sdk_backup = dict(core._SDK_CACHE)
        core._SDK_CACHE.clear()
        core._SDK_CACHE.update(
            checked=True, available=False, func=None, version="", error="")

        patchers = [
            mock.patch.object(core, "get_config_path", return_value=config_path),
            mock.patch.object(core, "download_model",
                              side_effect=AssertionError("不应启动真实下载")),
            mock.patch.object(core, "fetch_repo_files",
                              side_effect=AssertionError("不应发起真实查询")),
            mock.patch.object(gui_app.messagebox, "showwarning"),
            mock.patch.object(gui_app.messagebox, "showerror"),
            mock.patch.object(gui_app.messagebox, "showinfo"),
            mock.patch.object(gui_app.messagebox, "askyesno", return_value=True),
            mock.patch.object(gui_app.filedialog, "askdirectory", return_value=""),
            # G 组测的是「SDK 未安装」场景。core.sdk_is_installed() 走
            # importlib.util.find_spec，并不读 _SDK_CACHE，因此上面只清缓存
            # 不足以隔离；在装了 modelscope 的 venv 里会误判为已安装，
            # 导致 g04/g12（以及首帧安装指引）失败。显式固定为 False，
            # 让本组用例在任何解释器下都精确落在被测前提上。
            # 追加在列表末尾，保持 started[1]/[3]/[4] 的既有索引语义。
            mock.patch.object(core, "sdk_is_installed", return_value=False),
        ]
        started = []
        for patcher in patchers:
            started.append(patcher.start())
            self.addCleanup(patcher.stop)
        self.download_mock = started[1]
        self.warn_mock = started[3]
        self.error_mock = started[4]

        def restore():
            core._SDK_CACHE.clear()
            core._SDK_CACHE.update(sdk_backup)
            tmp_obj.cleanup()

        self.addCleanup(restore)

        try:
            self.root = tk.Tk()
        except Exception as exc:  # 无显示服务等情况
            self.skipTest("无法创建 Tk 根窗口：{0}".format(exc))
        self.root.withdraw()
        # main() 才会设置标题，测试里显式补齐以贴近真实窗口
        self.root.title(gui_app.APP_TITLE)
        # 兜底定时销毁，避免任何意外把测试挂死
        try:
            self._destroy_job = self.root.after(5000, self.root.destroy)
        except tk.TclError:
            self._destroy_job = None
        self.addCleanup(self._safe_destroy)
        self.app = gui_app.DownloaderApp(self.root)
        # 只驱动一次事件循环就记录「启动第一帧」日志：用于验证 SDK 缺失时的
        # 安装指引由 _refresh_sdk_status 同步投递，不依赖后台探测线程。
        try:
            self.root.update()
        except tk.TclError:
            pass
        self._first_frame_log = self._log_text()
        self._pump()

    def _safe_destroy(self) -> None:
        """取消所有 pending after 回调并在 Tk 存活期间释放 tk 变量。"""
        app = getattr(self, "app", None)
        root = getattr(self, "root", None)
        if root is None:
            return
        for job in (
            getattr(self, "_destroy_job", None),
            getattr(app, "_timer_job", None),
            getattr(app, "_ui_pump_job", None),
        ):
            if job:
                try:
                    root.after_cancel(job)
                except Exception:
                    pass
        if app is not None:
            for name in ("_drain_ui_queue", "_stop_elapsed_timer"):
                try:
                    getattr(app, name)()
                except Exception:
                    pass
            try:
                app.progress_bar.stop()
            except Exception:
                pass
            # 让 StringVar 等在 Tk 解释器仍存活时被回收，避免 __del__ 报
            # "main thread is not in main loop" 之类的无害噪音。
            for attr in list(vars(app)):
                try:
                    if isinstance(getattr(app, attr), tk.Variable):
                        delattr(app, attr)
                except Exception:
                    pass
        try:
            root.update_idletasks()
        except Exception:
            pass
        self.app = None
        try:
            root.destroy()
        except Exception:
            pass

    def _pump(self, times: int = 6) -> None:
        """只驱动事件循环，不调用任何内部 drain 方法（避免假阳性）。

        主线程投递的任务由 ``_ui`` 的 ``after(0, _drain_ui_queue)`` 在
        ``root.update()`` 中执行；后台线程投递的任务必须由 40ms 定时泵在
        真实 ``mainloop()`` 中排空，见 test_g11。
        """
        for _ in range(times):
            try:
                self.root.update()
            except tk.TclError:
                break

    def _join_probe_thread(self) -> None:
        for thread in threading.enumerate():
            if thread.name == "ms-sdk-probe" and thread.is_alive():
                thread.join(2.0)

    def _log_text(self) -> str:
        try:
            return self.app.log_text.get("1.0", "end")
        except tk.TclError:
            return ""

    def test_g01_main_window_and_critical_widgets(self) -> None:
        """主窗口可构造，关键控件齐全。"""
        self.assertEqual(self.root.winfo_class(), "Tk")
        self.assertEqual(self.root.title(), gui_app.APP_TITLE)
        for name in (
            "btn_preview", "btn_download", "btn_stop", "tree",
            "progress_bar", "log_text", "sdk_label",
            "var_model", "var_dir", "var_status", "var_sdk",
            "var_progress", "var_summary", "var_elapsed",
            "var_revision", "var_endpoint", "var_proxy",
            "var_workers", "var_subdir", "var_allow", "var_ignore",
            "var_only_selected",
        ):
            with self.subTest(widget=name):
                self.assertTrue(hasattr(self.app, name), "缺少控件 {0}".format(name))
        self.assertEqual(self.app.root, self.root)
        self.assertIsInstance(self.app.config, core.AppConfig)
        self.assertFalse(self.app._busy)
        self.assertIn("程序已启动", self._log_text())

    def test_g02_sdk_missing_status_is_readable(self) -> None:
        """SDK 未安装时状态栏给出可读提示（不是 traceback / 空白）。"""
        self._join_probe_thread()
        self._pump()
        status = self.app.var_sdk.get()
        self.assertTrue(status.strip())
        self.assertTrue("未安装" in status or "不可用" in status, status)
        self.assertIn(core.INSTALL_COMMAND, status)
        self.assertIn("pip install -r requirements.txt", status)
        self.assertNotIn("Traceback", status)

    def test_g03_install_guide_log_contains_pip_command(self) -> None:
        """安装指引日志含 pip install -r requirements.txt，且弹窗不崩。"""
        self.app._show_install_guide()
        self._pump()
        text = self._log_text()
        self.assertIn(core.INSTALL_COMMAND, text)
        self.assertIn("modelscope", text)
        self.warn_mock.assert_called_once()
        args, _kwargs = self.warn_mock.call_args
        self.assertIn(core.INSTALL_COMMAND, str(args))
        self.assertIn("安装依赖.bat", str(args))

    def test_g04_click_download_without_sdk_no_traceback_no_thread(self) -> None:
        """SDK 缺失时点击【开始下载】：仅弹指引，不抛异常、不启动下载线程。"""
        before = {t.name for t in threading.enumerate()}
        try:
            self.app.btn_download.invoke()
        except Exception as exc:  # noqa: BLE001
            self.fail("点击【开始下载】抛出异常：{0}: {1}".format(
                type(exc).__name__, exc))
        self._pump()
        self.warn_mock.assert_called()
        self.download_mock.assert_not_called()
        self.assertIsNone(self.app._worker, "不应创建下载线程")
        after = {t.name for t in threading.enumerate()}
        self.assertNotIn("ms-download", after - before)
        self.assertNotIn("Traceback", self._log_text())
        self.assertIn(core.INSTALL_COMMAND, self._log_text())

    def test_g05_button_state_machine(self) -> None:
        """按钮状态机：空闲 / 忙碌 / 停止 / 恢复。"""
        self.assertEqual(str(self.app.btn_download.cget("state")), "normal")
        self.assertEqual(str(self.app.btn_preview.cget("state")), "normal")
        self.assertEqual(str(self.app.btn_stop.cget("state")), "disabled")

        self.app._cancel_event.set()
        self.app._set_busy(True)
        self._pump()
        self.assertTrue(self.app._busy)
        self.assertFalse(self.app._cancel_event.is_set(),
                         "新任务开始时必须清除上次的取消事件")
        self.assertEqual(str(self.app.btn_download.cget("state")), "disabled")
        self.assertEqual(str(self.app.btn_preview.cget("state")), "disabled")
        self.assertEqual(str(self.app.btn_stop.cget("state")), "normal")

        self.app._on_stop()
        self._pump()
        self.assertTrue(self.app._cancel_event.is_set())
        self.assertIn("停止", self.app.var_status.get())
        self.assertEqual(str(self.app.btn_stop.cget("state")), "disabled")

        self.app._set_busy(False)
        self._pump()
        self.assertFalse(self.app._busy)
        self.assertEqual(str(self.app.btn_download.cget("state")), "normal")
        self.assertEqual(str(self.app.btn_stop.cget("state")), "disabled")

    def test_g06_progress_bar_determinate_indeterminate_switch(self) -> None:
        """复验修复 #2：unknown→indeterminate、50%→value≈50、停止→determinate。

        Tk 8.6 下 ``ttk.Progressbar.cget("mode")`` 返回 ``_tkinter.Tcl_Obj``
        而非 str，直接与字符串比较恒为 False；源码必须经 ``_progress_mode()``
        显式转换。本用例同时校验该辅助方法的返回类型与转换正确性。
        """
        raw_mode = self.app.progress_bar.cget("mode")
        mode = self.app._progress_mode()
        self.assertIsInstance(mode, str, "_progress_mode() 必须返回 str")
        self.assertIn(mode, ("determinate", "indeterminate"))
        self.assertEqual(str(raw_mode), mode,
                         "_progress_mode() 应是 cget 值的字符串化")

        self.app._reset_progress(0)
        self._pump()
        self.assertEqual(self.app._progress_mode(), "indeterminate")

        self.app._reset_progress(1000)
        self._pump()
        self.assertEqual(self.app._progress_mode(), "determinate")
        self.assertEqual(float(self.app.progress_bar.cget("value")), 0.0)

        self.app._on_progress(500, 1000, "")
        self._pump()
        self.assertEqual(self.app._progress_mode(), "determinate")
        self.assertAlmostEqual(float(self.app.progress_bar.cget("value")), 50.0,
                               delta=0.5)
        self.assertIn("50.0%", self.app.var_progress.get())

        self.app._on_progress(10, 0, "")
        self._pump()
        self.assertEqual(self.app._progress_mode(), "indeterminate")

        self.app._stop_progress_animation()
        self._pump()
        self.assertEqual(
            (self.app._progress_mode(), self.app.var_progress.get()),
            ("determinate", "任务结束"),
            "任务结束后必须停止 indeterminate 动画并切回 determinate",
        )

    def test_g07_treeview_select_all_none_invert(self) -> None:
        """Treeview 全选 / 全不选 / 反选 / 单选与汇总信息。"""
        info = core.ModelInfo(
            model_id="Qwen/Qwen2.5", revision="master", source="REST",
            files=[core.RepoFile("a.bin", 10),
                   core.RepoFile("b.bin", 20),
                   core.RepoFile("onnx/c.bin", 30, is_lfs=True)],
        )
        self.app._show_model_info(info)
        self._pump()
        self.assertEqual(len(self.app.tree.get_children()), 3)
        self.assertEqual(len(self.app._selected_paths()), 3, "默认应全部勾选")
        self.assertIn("共 3 个文件", self.app.var_summary.get())
        self.assertIn("合计", self.app.var_summary.get())
        self.assertIn("公开 REST 接口", self.app.var_summary.get())

        self.app._select_all(False)
        self._pump()
        self.assertEqual(self.app._selected_paths(), [])

        self.app._select_all(True)
        self._pump()
        self.assertEqual(sorted(self.app._selected_paths()),
                         ["a.bin", "b.bin", "onnx/c.bin"])

        self.app._invert_selection()
        self._pump()
        self.assertEqual(self.app._selected_paths(), [])

        self.app._invert_selection()
        self._pump()
        self.assertEqual(len(self.app._selected_paths()), 3)

        self.app._toggle_row("a.bin")
        self._pump()
        self.assertEqual(sorted(self.app._selected_paths()),
                         ["b.bin", "onnx/c.bin"])

    def test_g08_prepare_validates_inputs(self) -> None:
        """空模型名弹错并返回 None；合法输入返回规范化配置。"""
        self.app.var_model.set("")
        self.error_mock.reset_mock()
        self.assertIsNone(self.app._prepare())
        self.error_mock.assert_called()

        target = self.tmp / "下载 目录" / "带 空格"
        self.app.var_model.set("  Qwen/Qwen2.5-7B-Instruct  ")
        self.app.var_dir.set(str(target))
        config = self.app._prepare()
        self.assertIsInstance(config, core.AppConfig)
        self.assertEqual(config.model_id, "Qwen/Qwen2.5-7B-Instruct")
        self.assertTrue(target.is_dir(), "目标文件夹应自动创建")
        self.assertEqual(Path(config.target_dir).resolve(), target.resolve())
        self.assertTrue((self.tmp / "APPDATA" / "ModelScopeDownloader" /
                         "config.json").is_file(), "配置应写入临时目录而非真实 APPDATA")

    def test_g09_safe_int_and_collect_config(self) -> None:
        """并发数非法输入回退默认值，配置收集字段完整。"""
        self.assertEqual(gui_app.DownloaderApp._safe_int("abc", 4), 4)
        self.assertEqual(gui_app.DownloaderApp._safe_int("-3", 4), 4)
        self.assertEqual(gui_app.DownloaderApp._safe_int("8", 4), 8)
        self.app.var_model.set("Qwen/x")
        self.app.var_dir.set(str(self.tmp / "out"))
        self.app.var_workers.set("not-a-number")
        config = self.app._collect_config()
        self.assertEqual(config.max_workers, core.DEFAULT_MAX_WORKERS)
        self.assertEqual(config.model_id, "Qwen/x")
        self.assertEqual(config.revision, core.DEFAULT_REVISION)

    def test_g10_main_entrypoint_and_module_structure(self) -> None:
        """main() 存在，且 __main__ 分支正确调用（不真正进入 mainloop）。"""
        self.assertTrue(callable(gui_app.main))
        tree = ast.parse((ROOT / "gui_app.py").read_text(encoding="utf-8"))
        has_main_guard = any(
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and any(isinstance(x, ast.Name) and x.id == "__name__"
                    for x in ast.walk(node.test))
            for node in tree.body
        )
        self.assertTrue(has_main_guard, "缺少 if __name__ == '__main__' 入口")
        self.assertTrue(hasattr(gui_app.DownloaderApp, "_download_worker"))

    def test_g11_ui_queue_delivers_background_thread_updates(self) -> None:
        """复验修复 #1：后台线程只 put 队列，主线程真实 mainloop 排空。

        严格性：本用例**不调用** ``_drain_ui_queue`` / ``_pump`` 等任何内部
        快捷方法，只驱动真实 ``root.mainloop()``。若 40ms 定时泵失效，
        下列断言就会失败，从而排除「显式 drain 造成假阳性」。
        """
        marker = "后台线程日志-QA-MARKER"
        status_marker = "后台线程状态-QA"
        errors = []

        def worker():
            try:
                self.app._log(marker)               # 后台线程：只 put 队列
                self.app._on_progress(25, 100, "")  # 后台线程：只 put 队列
                self.app._ui(lambda: self.app.var_status.set(status_marker))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=worker, name="qa-ui-worker", daemon=True)
        thread.start()
        thread.join(2.0)
        self.assertFalse(thread.is_alive(), "后台投递线程异常挂起")
        self.assertEqual(errors, [], "后台线程调用 _ui 不应抛 RuntimeError")

        # 驱动事件循环之前，控件状态不应已变化（证明不是同步写入的假阳性）
        self.assertNotIn(marker, self._log_text())
        self.assertNotEqual(self.app.var_status.get(), status_marker)

        # 只驱动真实 mainloop，让 40ms 定时泵自然排空队列
        self.root.after(300, self.root.quit)
        self.root.mainloop()

        self.assertIn(marker, self._log_text(), "定时泵未把后台日志投递到 Text")
        self.assertEqual(self.app.var_status.get(), status_marker,
                         "定时泵未把后台 UI 任务投递到状态栏")
        self.assertIn("25.0%", self.app.var_progress.get(),
                      "定时泵未把后台进度投递到进度条")

    def test_g12_startup_first_frame_has_install_guide(self) -> None:
        """复验修复 #3：构造窗口后第一帧日志即含安装指引，不依赖后台线程。"""
        first = self._first_frame_log
        self.assertIn(core.INSTALL_COMMAND, first,
                      "启动第一帧日志缺少 pip install -r requirements.txt")
        self.assertIn("未检测到 modelscope SDK", first)
        self.assertIn("安装依赖.bat", first)
        self.assertIn("程序已启动", first)

        status = self.app.var_sdk.get()
        self.assertIn(core.INSTALL_COMMAND, status)
        self.assertTrue("未安装" in status or "不可用" in status, status)
        self.assertFalse(
            any(t.name == "ms-sdk-probe" for t in threading.enumerate()),
            "SDK 未安装时不应启动后台探测线程，安装指引必须来自同步路径",
        )

        # 点击【开始下载】：不抛 traceback、不启动下载线程
        self.warn_mock.reset_mock()
        try:
            self.app.btn_download.invoke()
        except Exception as exc:  # noqa: BLE001
            self.fail("点击【开始下载】抛出异常：{0}: {1}".format(
                type(exc).__name__, exc))
        self._pump()
        self.warn_mock.assert_called()
        self.download_mock.assert_not_called()
        self.assertIsNone(self.app._worker, "不应创建下载线程")
        self.assertNotIn("Traceback", self._log_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
