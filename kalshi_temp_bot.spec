# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build spec for the Kalshi Temperature Bot GUI.
# Build with:  python -m PyInstaller --noconfirm --clean kalshi_temp_bot.spec
import os

block_cipher = None

icon_path = os.path.join("assets", "icon.ico")
datas = []
if os.path.exists(icon_path):
    datas.append((icon_path, "assets"))

a = Analysis(
    ["gui_app.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    # These are imported lazily / inside try-blocks, so name them explicitly.
    hiddenimports=["kalshi_temp_bot", "websockets", "cryptography", "dotenv"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="KalshiTempBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # windowed GUI application (no console window)
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path if os.path.exists(icon_path) else None,
)
