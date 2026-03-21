# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


project_root = Path(SPECPATH).parent

hiddenimports = collect_submodules("textual")
hiddenimports += collect_submodules("rich")
hiddenimports += ["chat_tui.app", "launcher.app"]

datas = collect_data_files("textual")
datas += collect_data_files("rich")
datas += [(str(project_root / "scanner.py"), ".")]


a = Analysis(
    [str(project_root / "launcher" / "app.py")],
    pathex=[str(project_root)],
    binaries=[],
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
    a.binaries,
    a.datas,
    [],
    name="chat-over-dnstt-tui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
)
