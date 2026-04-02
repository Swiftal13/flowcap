# -*- mode: python ; coding: utf-8 -*-
# FlowCap PyInstaller spec
# Usage: pyinstaller flowcap.spec

import sys
from pathlib import Path

block_cipher = None

# Collect FFmpeg binaries from vendor/
_vendor = Path('vendor')
_suffix = '.exe' if sys.platform == 'win32' else ''
_binaries = []
for _name in ('ffmpeg', 'ffprobe'):
    _p = _vendor / f'{_name}{_suffix}'
    if _p.exists():
        _binaries.append((str(_p), 'vendor'))

# Collect rife-ncnn-vulkan binary + model files from vendor/rife/
_rife_dir = _vendor / 'rife'
if _rife_dir.exists():
    _rife_bin = _rife_dir / f'rife-ncnn-vulkan{_suffix}'
    if _rife_bin.exists():
        _binaries.append((str(_rife_bin), 'vendor/rife'))
    # Bundle all model subdirectories (rife-v4.6/, etc.)
    for _model_dir in _rife_dir.iterdir():
        if _model_dir.is_dir():
            for _model_file in _model_dir.iterdir():
                if _model_file.is_file():
                    _binaries.append((str(_model_file), f'vendor/rife/{_model_dir.name}'))

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=_binaries,
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
