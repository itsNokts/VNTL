# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [
    ('hook/vntl_hook_x64.dll', 'hook'),
    ('hook/vntl_hook_x86.dll', 'hook'),
    ('hook/vntl_inject32.exe', 'hook'),
]
binaries = []
hiddenimports = [
    'qasync',
    'mss.windows',
]

_d, _b, _h = collect_all('PyQt6')
datas    += _d
binaries += _b
hiddenimports += _h

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='vntl',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # uac_admin intentionally omitted — most VNs run as normal user so VNTL doesn't
    # need elevation. Running as admin creates a pipe with admin-only security,
    # preventing the normal-user DLL in the game from writing to it.
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='vntl',
)
