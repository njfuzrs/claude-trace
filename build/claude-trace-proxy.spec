# -*- mode: python ; coding: utf-8 -*-
# build/claude-trace-proxy.spec — PyInstaller 打包配置
#
# 用法：
#   cd build && pyinstaller claude-trace-proxy.spec
#   或通过 build.sh 自动调用

import os
import sys

# 项目根目录
ROOT = os.path.abspath(os.path.join(os.path.dirname(SPEC), '..'))

a = Analysis(
    [os.path.join(ROOT, 'proxy.py')],
    pathex=[ROOT],
    hiddenimports=['builder', 'uploader'],
    binaries=[],
    datas=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 排除不需要的大模块，减小体积
        'tkinter', '_tkinter',
        'unittest',
        'xmlrpc',
        'pydoc',
        'doctest',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='claude-trace-proxy',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,  # macOS 上 strip 可能破坏代码签名
    upx=False,    # UPX 在 macOS 上兼容性不好
    console=True,
    target_arch=None,  # 默认当前架构，可通过 --target-arch 覆盖
)
