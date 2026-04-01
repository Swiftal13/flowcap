# -*- mode: python ; coding: utf-8 -*-
# FlowCap PyInstaller spec
# Usage: pyinstaller flowcap.spec

import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('ui/styles.qss', 'ui'),
    ],
    hiddenimports=[
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['cv2', 'numpy'],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FlowCap',
    debug=False,
    strip=False,
    upx=True,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    name='FlowCap',
)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='FlowCap.app',
        bundle_identifier='com.swiftal.flowcap',
        info_plist={
            'NSHighResolutionCapable': True,
            'CFBundleShortVersionString': '1.0.0',
            'CFBundleName': 'FlowCap',
            'LSMinimumSystemVersion': '11.0',
            'NSHumanReadableCopyright': 'Made by Swiftal',
        },
    )
