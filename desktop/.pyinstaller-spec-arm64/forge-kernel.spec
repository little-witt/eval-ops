# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['../kernel_entry.py'],
    pathex=['/Users/liuzhenni/workspace/ai/eval-ops/src'],
    binaries=[],
    datas=[('/Users/liuzhenni/workspace/ai/eval-ops/src/aceval/d2c_driver', 'aceval/d2c_driver')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='forge-kernel',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch='arm64',
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='forge-kernel',
)
