# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['solver.py'],
    pathex=[],
    binaries=[],
    datas=[('/Users/jules/src/jres_solver/env/lib/python3.13/site-packages/pulp', 'pulp')],
    hiddenimports=['pulp.apis.PULP_CBC_CMD', 'uuid'],
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
    name='solver',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
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
    name='solver',
)
