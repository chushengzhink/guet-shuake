# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（GUI 版）。

两个变体靠环境变量切换：
    GUET_BUNDLE_BROWSER=1  → 完整版（内置 Chromium，装完不联网也能用）
    GUET_BUNDLE_BROWSER=0  → 精简版（依赖系统 Edge / Chrome）

构建见 packaging/build.ps1。
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent
BUNDLE = os.environ.get("GUET_BUNDLE_BROWSER", "1") == "1"

datas = [(str(ROOT / "guet" / "gui" / "web" / "index.html"), "guet/gui/web")]
binaries = []
hiddenimports = ["playwright", "yaml", "requests"]

# playwright 需要自带的 driver（node.exe 等）
pw_datas, pw_binaries, pw_hidden = collect_all("playwright")
datas += pw_datas
binaries += pw_binaries
hiddenimports += pw_hidden

if BUNDLE:
    browsers = SPEC_DIR / "browsers"
    if browsers.exists():
        datas.append((str(browsers), "browsers"))
    else:  # pragma: no cover - 构建环境缺失时提醒
        print("!! 未找到 packaging/browsers，完整版将不含浏览器内核")

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter", "numpy", "pandas", "matplotlib", "PIL", "pytest", "setuptools", "pip",
        # 构建机 conda 环境里的「意外收集」大户，本程序完全用不到
        "sphinx", "docutils", "jedi", "babel", "jinja2", "markdown_it", "pygments",
        "imagesize", "alabaster", "snowballstemmer", "zmq", "IPython", "ipython",
        "nbconvert", "nbformat", "jupyter_client", "ipykernel",
        # 构建机上同时装了 PyQt5 和 PySide6，PyInstaller 不允许两者共存；GUI 是纯 HTML，用不到
        "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "shiboken2", "shiboken6",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GuetShuake",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(SPEC_DIR / "app.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="GuetShuake",
)
