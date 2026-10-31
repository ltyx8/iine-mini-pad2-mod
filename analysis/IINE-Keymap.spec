# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['winrt.windows.devices.bluetooth', 'winrt.windows.devices.bluetooth.genericattributeprofile', 'winrt.windows.storage.streams', 'winrt.windows.foundation']
hiddenimports += collect_submodules('winrt')
hiddenimports += collect_submodules('bleak')


a = Analysis(
    ['bridge.py'],
    pathex=[],
    binaries=[],
    datas=[('web', 'web')],
    hiddenimports=hiddenimports,
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
    a.binaries,
    a.datas,
    [],
    name='IINE-Keymap',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
