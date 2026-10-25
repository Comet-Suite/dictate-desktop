# Third-party software

Dictate source is licensed GPL-3.0-only. It uses the following separately licensed components; copyright and license notices are retained in the Windows package under `THIRD_PARTY_LICENSES`.

| Component | License | Upstream/source |
|---|---|---|
| Python | PSF | https://www.python.org/downloads/source/ |
| PyQt6 6.11.0 | GPL-3.0 / commercial | https://pypi.org/project/PyQt6/6.11.0/#files |
| Qt 6.11.2 | LGPL-3.0 / GPL / commercial, per module | https://download.qt.io/official_releases/qt/6.11/6.11.2/ |
| NumPy 2.3.5 | BSD-3-Clause and bundled notices | https://pypi.org/project/numpy/2.3.5/#files |
| sounddevice 0.5.6 / PortAudio | MIT | https://github.com/spatialaudio/python-sounddevice |
| pynput 1.8.2 | LGPL-3.0 | https://github.com/moses-palmer/pynput |
| huggingface-hub 2.3.0 | Apache-2.0 | https://github.com/huggingface/huggingface_hub |
| CrispASR 0.8.44 | MIT and bundled notices | https://github.com/CrispStrobe/CrispASR |
| transcribe.cpp / bindings 0.4.1 | MIT and bundled notices | https://github.com/handy-computer/transcribe.cpp |
| PyInstaller bootloader | GPL with distribution exception | https://pyinstaller.org/en/stable/license.html |

The build generates a complete package/version inventory, copies installed distribution license files and provides exact source-distribution links in `THIRD_PARTY_LICENSES/SOURCES.md`. Where a package contains bundled native libraries, its own notices also apply. No dependency license is replaced by the project's GPL notice.

Model weights are not bundled. Each downloaded model remains subject to its model-card license, including applicable NVIDIA model terms.
