# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent

datas = [(str(ROOT / 'LICENSE'), '.'),
         (str(ROOT / 'THIRD_PARTY_NOTICES.md'), '.'),
         (str(ROOT / 'docs'), 'docs'),
         (str(ROOT / 'assets' / 'app.ico'), 'assets')]
binaries = []
hiddenimports = [
    'pynput.keyboard._win32', 'pynput.mouse._win32',
    'cffi', '_cffi_backend', '_sounddevice_data',
]
for package in ('crispasr', 'transcribe_cpp', 'transcribe_cpp_native',
                'huggingface_hub', 'hf_xet'):
    package_data, package_binaries, package_hidden = collect_all(package)
    datas += package_data
    binaries += package_binaries
    hiddenimports += package_hidden
for distribution in ('PyQt6', 'PyQt6-Qt6', 'PyQt6-sip', 'numpy', 'sounddevice',
                     'pynput', 'huggingface-hub', 'crispasr',
                     'transcribe-cpp', 'transcribe-cpp-native'):
    datas += copy_metadata(distribution, recursive=True)
license_dir = ROOT / 'packaging' / 'THIRD_PARTY_LICENSES'
if license_dir.exists():
    datas.append((str(license_dir), 'THIRD_PARTY_LICENSES'))

a = Analysis(
    [str(ROOT / 'dictate.py')], pathex=[str(ROOT)],
    binaries=binaries, datas=datas, hiddenimports=hiddenimports,
    hookspath=[], hooksconfig={}, runtime_hooks=[str(ROOT / "packaging" / "runtime_smoke.py")],
    excludes=['tkinter', 'PySide6', 'PyQt5', 'PySide2'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name='Dictate',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False,
    icon=str(ROOT / 'assets' / 'app.ico'),
    version=str(ROOT / 'packaging' / 'version_info.txt'),
    uac_admin=False,
)
diagnostic = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name='Dictate-Diagnostics',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True, icon=str(ROOT / 'assets' / 'app.ico'),
)
coll = COLLECT(exe, diagnostic, a.binaries, a.datas, strip=False, upx=False, name='Dictate')
