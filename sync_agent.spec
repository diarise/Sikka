# -*- mode: python ; coding: utf-8 -*-
#
# Changes from your old spec:
#   - hiddenimports now pulls in supabase's actual sub-packages (gotrue,
#     postgrest, realtime, storage3, supafunc) via collect_submodules instead
#     of listing just 'pyodbc' and 'supabase'. The old spec would very likely
#     have built successfully but crashed at runtime on your brother's
#     machine with a ModuleNotFoundError the first time the agent tried to
#     call Supabase — PyInstaller's static analysis often can't see these
#     because supabase-py imports them dynamically.
#   - added 'dotenv' since sync_agent.py now reads config from a .env file
#     next to the exe instead of hardcoded values.
#
# NOTE: this exe is unsigned (codesign_identity=None below). Windows
# SmartScreen / Defender may flag a brand-new unsigned exe on first run on
# merchant machines. That's normal for unsigned binaries, not a sign
# something's broken — worth a code-signing certificate later if this
# becomes an issue at scale, but not required to get this working now.

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hidden_imports = (
    ['pyodbc', 'dotenv', 'encodings', 'encodings.utf_8']
    + collect_submodules('supabase')
    + collect_submodules('gotrue')
    + collect_submodules('postgrest')
    + collect_submodules('realtime')
    + collect_submodules('storage3')
    + collect_submodules('supafunc')
)

a = Analysis(
    ['sync_agent.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
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
    name='sync_agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # Keep True during testing so your brother sees logs; the agent also
                    # writes to logs/sync_agent.log regardless, so you can flip this to
                    # False later for a silent background run without losing visibility.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    sign_using_code=None,
)
