# -*- coding: utf-8 -*-
"""一键构建 ModelScope 下载器单文件 exe。

用法（在项目根目录执行）：
    pack/.venv/Scripts/python.exe pack/build.py

会生成 dist/ModelScope下载器.exe。图标由本脚本用纯标准库绘制，无额外依赖。
"""
from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VENV_PY = HERE / ".venv" / "Scripts" / "python.exe"
ICO = HERE / "app.ico"
SPEC = HERE / "downloader.spec"
DIST = ROOT / "dist"
EXE_NAME = "ModelScope下载器.exe"


def make_icon() -> None:
    """用纯标准库画一个 32x32 / 32bpp 的 ICO：蓝底 + 白色下载箭头。"""
    size = 32
    pixels = bytearray()
    for y in range(size - 1, -1, -1):  # BMP 像素自下而上
        for x in range(size):
            dx, dy = x - 15.5, y - 15.5
            if dx * dx + dy * dy > 15.2 ** 2:
                pixels += bytes((0, 0, 0, 0))  # 圆外透明
                continue
            t = y / 31.0
            r, g, b = int(45 + 15 * t), int(120 - 10 * t), int(215 - 25 * t)
            shaft = 13 <= x <= 18 and 6 <= y <= 19
            triangle = False
            if 18 <= y <= 25:
                half = max(1, int((25 - y) * 1.7))
                triangle = 15 - half <= x <= 15 + half
            baseline = 26 <= y <= 28 and 9 <= x <= 22
            if shaft or triangle or baseline:
                r = g = b = 255
            pixels += bytes((b, g, r, 255))  # BGRA

    and_mask = bytes(size * 4)  # 32x32 1bpp，全 0（透明度由 alpha 通道决定）
    bmp_header = struct.pack(
        "<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(pixels), 0, 0, 0, 0
    )
    image = bmp_header + bytes(pixels) + and_mask
    ico = (
        struct.pack("<HHH", 0, 1, 1)
        + struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(image), 22)
        + image
    )
    ICO.write_bytes(ico)
    print("[build] 图标已生成：{} ({} 字节)".format(ICO, len(ico)), flush=True)


def main() -> int:
    if not VENV_PY.is_file():
        print("[build] 缺少隔离环境 {}，请先建 venv 并安装依赖".format(VENV_PY))
        return 2
    DIST.mkdir(parents=True, exist_ok=True)
    make_icon()

    cmd = [
        str(VENV_PY), "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--distpath", str(DIST),
        "--workpath", str(HERE / "build"),
        str(SPEC),
    ]
    print("[build] 开始构建：{}".format(" ".join(cmd)), flush=True)
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("[build] PyInstaller 失败，退出码 {}".format(result.returncode))
        return result.returncode

    exe = DIST / EXE_NAME
    if not exe.is_file():
        print("[build] 未找到产物 {}".format(exe))
        return 3
    size_mb = exe.stat().st_size / 1048576
    print("[build] 构建成功：{} ({:.1f} MB)".format(exe, size_mb), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
