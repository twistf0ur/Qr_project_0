# -*- mode: python ; coding: utf-8 -*-
import os
from importlib.util import find_spec

# Locate the real pyzbar package dir (installed in the user site-packages)
_spec = find_spec('pyzbar')
pyzbar_dir = os.path.dirname(_spec.origin)

a = Analysis(
    ['qr_scanner.py'],
    pathex=[],
    binaries=[],
    # beep-01a.wav -> bundle root; zbar DLLs -> pyzbar/ subfolder so that
    # pyzbar.zbar_library.load() finds them next to its package at runtime.
    datas=[
        ('beep-01a.wav', '.'),
        (os.path.join(pyzbar_dir, 'libzbar-64.dll'), 'pyzbar'),
        (os.path.join(pyzbar_dir, 'libiconv.dll'), 'pyzbar'),
    ],
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
    a.binaries,
    a.datas,
    [],
    name='BarcodeScanner',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
