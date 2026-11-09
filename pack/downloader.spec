# -*- mode: python ; coding: utf-8 -*-
"""ModelScope 下载器 · 单文件 exe 构建配置。

关键点：modelscope 1.40 完整包有 2866 个 .py / 49 MB，其中 trainers、
pipelines、models 等是训练推理模块，本工具完全用不到。下载实现其实在
独立的 ``modelscope_hub`` 包里。因此这里只精准收集下载链路涉及的 4 个子包，
把 exe 体积和启动时间压下来。
"""
import os

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

SPEC_DIR = os.path.abspath(SPECPATH)
ROOT = os.path.dirname(SPEC_DIR)
block_cipher = None

hiddenimports = []
datas = []
binaries = []

# 下载链路真实涉及的 4 个子包（合计约 150 个模块 / 5 MB）
for pkg in (
    "modelscope_hub",        # 1.38+ 真正的下载实现
    "modelscope.hub",        # snapshot_download shim + HubApi
    "modelscope.utils",      # logger / import_utils / 常量
    "modelscope.fileio",     # 文件读写工具
):
    hiddenimports += collect_submodules(pkg)
    datas += collect_data_files(pkg)

# core 里用 importlib.import_module 动态导入，静态分析看不到，必须显式列出
hiddenimports += [
    "modelscope",
    "modelscope.hub.api",
    "modelscope.hub.snapshot_download",
    "modelscope.hub.file_download",
    "modelscope.hub.callback",
    "modelscope.hub.utils.utils",
    "modelscope.utils.logger",
    "modelscope.utils.import_utils",
    "modelscope_hub",
    "modelscope_hub.compat.snapshot_download",
    "certifi",
    "gui_app",
    "ms_downloader_core",
    "tkinter",
    "tkinter.ttk",
    "tkinter.constants",
    "tkinter.font",
    "tkinter.messagebox",
    "tkinter.filedialog",
    "tkinter.scrolledtext",
]

# HTTPS 根证书 + 本机扩展模块（cffi / charset_normalizer 的 .pyd）
datas += collect_data_files("certifi")
for pkg in ("cffi", "charset_normalizer", "cryptography"):
    try:
        binaries += collect_dynamic_libs(pkg)
    except Exception:
        pass

a = Analysis(
    [os.path.join(SPEC_DIR, "entry.py")],
    pathex=[ROOT, SPEC_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PyQt5", "PyQt6", "PySide2", "PySide6",
        "matplotlib", "pytest", "IPython", "notebook",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ModelScope下载器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,           # 双击不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(SPEC_DIR, "app.ico"),
)
