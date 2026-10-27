# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Comet-Suite
# Distributed under GPL-3.0; see LICENSE. No warranty.
"""
Dictate 1.2.1  -  system-wide offline dictation
PyQt6 UI + NVIDIA Parakeet Unified / TDT GGUF (local CPU runtimes)

Install:
    pip install PyQt6 crispasr sounddevice numpy pynput huggingface_hub
    # Unified GGUF support is installed on first use if missing:
    pip install transcribe-cpp==0.4.1

Run (no console window):
    pythonw dictate.py

How to use:
    1. Settings -> download or select a .gguf model (wait for "Model loaded").
    2. Click in any text box in any app.
    3. Press the dictation shortcut (default Ctrl+Alt+Space) to record.
    4. Speak, including pauses. Transcription runs privately in the background;
       nothing is inserted or submitted until you press Stop.
    5. Press the same shortcut again to type the whole recording once.
       The existing Auto Enter setting controls whether Enter is sent once.
    Show / hide shortcut: Ctrl+Alt+D. Dictation shortcut: Ctrl+Alt+Space.
    Mic icon = microphone on/off. Left button = pause/resume.
    The shortcut always uses explicit start/commit, even if Stop mode is Auto.
    Unified model: handy-computer/parakeet-unified-en-0.6b-gguf.
    Natural amounts: "four hundred dollars" -> $400; "400 rupees" -> ₹400.
    Percentages, simple numeric formulas and spoken email addresses also work.
    Explicit names still work: "at sign", "hash sign", "dollar sign",
    "open bracket" or "euro sign". Over 100 distinct symbols are supported.
    The full name-to-symbol vocabulary is in _SYMBOL_ROWS below.

Nothing is recorded or saved: audio lives only in RAM until it is transcribed and the
text is pasted into whatever app has the cursor. A temporary clipboard paste avoids
slow per-character typing; the previous clipboard is restored when possible, unless
you copy something else. Preferences, optional custom mappings and update installers
are stored separately; microphone audio is not saved.
"""
import collections
import base64
import ctypes
import gc
import functools
import html
import importlib
import json
import math
import multiprocessing
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import traceback
import weakref
from decimal import Decimal, localcontext
from pathlib import Path

_NEEDED = (("numpy", "numpy"), ("PyQt6", "PyQt6"), ("sounddevice", "sounddevice"),
           ("pynput", "pynput"), ("huggingface_hub", "huggingface_hub"))


def _ensure_deps():
    """Install anything missing using THIS Python, so pip and the app can never disagree."""
    import importlib.util
    need = [pip for mod, pip in _NEEDED if importlib.util.find_spec(mod) is None]
    if not need:
        return
    if getattr(sys, "frozen", False):
        raise RuntimeError("This installation is missing bundled dependencies. Reinstall Dictate.")
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        exe = exe.with_name("python.exe")
    try:
        subprocess.run([str(exe), "-m", "pip", "install", *need], capture_output=True, timeout=1200,
                       creationflags=0x08000000 if os.name == "nt" else 0)
    except Exception:
        pass
    importlib.invalidate_caches()
    still = [pip for mod, pip in _NEEDED if importlib.util.find_spec(mod) is None]
    if still:
        try:
            d = Path(os.environ.get("APPDATA", str(Path.home()))) / "Dictate"
            d.mkdir(parents=True, exist_ok=True)
            (d / "error.log").write_text(f"Missing packages: {still}\nPython: {sys.executable}\n"
                                         f"Run: \"{exe}\" -m pip install {' '.join(still)}\n",
                                         encoding="utf-8")
        except Exception:
            pass


_ensure_deps()

import numpy as np
from PyQt6.QtCore import (QEasingCurve, QEvent, QMimeData, QObject, QPoint, QPointF, QPropertyAnimation, QRectF,
                          QSharedMemory, QThread, Qt, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot)
from PyQt6.QtGui import (QAction, QColor, QGuiApplication, QIcon, QKeySequence, QPainter, QPen,
                         QDesktopServices, QPixmap, QPolygonF, QTextCursor)
from PyQt6.QtWidgets import (QAbstractButton, QApplication, QComboBox, QDialog, QFileDialog, QFrame,
                             QHBoxLayout, QKeySequenceEdit, QLabel, QLineEdit, QMenu, QPlainTextEdit,
                             QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSystemTrayIcon, QVBoxLayout, QWidget)

APP_NAME, VERSION = "Dictate", "1.2.1"
APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
SETTINGS_FILE = APP_DIR / "settings.json"
DEFAULTS = {"dark_mode": True, "start_on_startup": False, "model_path": "",
            "position": "bottom_center", "pos": None, "hotkey": "Ctrl+Alt+D",
            "mic": "", "download_dir": str(APP_DIR / "models"), "unload_min": 0, "size": "medium", "auto_enter": False, "pause_blocks": 8, "stop_mode": "auto", "dict_hotkey": "Ctrl+Alt+Space", "custom_symbols_path": "", "check_updates": True}

PRESET_REPOS = ["cstr/parakeet-tdt-0.6b-v3-GGUF", "cstr/orukeet-GGUF", "cstr/phonon2-GGUF",
                "cstr/parakeet-redux-GGUF",
                "handy-computer/parakeet-unified-en-0.6b-gguf"]
QUANTS = {"Q4_K  -  low memory / recommended": "q4_k", "Q4_K_M": "q4_k_m", "Q4_K_S": "q4_k_s",
          "Q5_K_M": "q5_k_m", "Q5_K_S": "q5_k_s", "Q5_0": "q5_0", "Q6_K": "q6_k",
          "Q8_0  -  higher precision": "q8_0", "F16  -  unquantized (large)": "f16",
          "Any  -  prefer low memory": ""}

SR, BLOCK = 16000, 512                 # 16 kHz, 32 ms blocks
SILENCE_BLOCKS = 22                    # ~0.7 s of silence ends a phrase
MIN_BLOCKS = 8                         # ignore blips shorter than ~0.25 s
MAX_BLOCKS = 10 * SR // BLOCK          # long speech is sent in 10 s pieces
PURPLE = QColor("#c93ff2")
SIZES = {"small": 0.8, "medium": 1.0, "large": 1.3}
SPEEDS = [("Fastest  -  0.25 s pause", 8), ("Balanced  -  0.4 s pause", 12), ("Accurate  -  0.7 s pause", 22)]
UNLOAD_CHOICES = [("Never", 0), ("After 5 minutes idle", 5), ("After 10 minutes idle", 10),
                  ("After 15 minutes idle", 15)]


# Application branding is embedded so this remains a standalone Python file.
# The same uploaded artwork is used by Qt, the tray, the executable and Setup.
_WINDOWS_APP_ID = "CometSuite.Dictate.Desktop"
_LOGO_PNG = (
    b"iVBORw0KGgoAAAANSUhEUgAAAQAAAAEACAYAAABccqhmAACfiElEQVR42u39e7xkV1kmjj/vWruqzjl9y60JCRAhQoDucJEE"
    b"uQodhBERdBDPEbyMgzMTfjIiqOOMM36d6jPOjCgit5/OJF4YnXFGz3HwKyrqCNOJQADtgEC6uYd7AumQTl/Opar2Ws/3j9q7"
    b"aq2119p71+nTSSd0fT7Vfar2pXbtWutd7/u8z/u8wPnH+cf5xzftQ87fgvvxQUofkKNYlbuwV05jl1x55HbBfphVWTLJ4/p9"
    b"9ZJ/8h17Lc1eweBSAR+iiYuNwiXa8DKAewW8GOSlAlwKy91KoGT8mRAAAkLI8SDg+DXA8d8kQFoRnhTg60L5OmjvBngMkK+J"
    b"5d1KcLfNcdecxV24CHf910f/6LHxmeKPRa5orB7RF155OS87dRWPHjjGfTjCZRwkJH3c+cd5A/Cgm+y333qlAoBbr7kmTw3+"
    b"7zz6jouz3fOXMDcXa2QXWeaXaGIvIXuE3CG0CwDnFbAAcgHCOYHMK4sdQs5TOC/kghALQnZFIIJyijIwAJwMBCHLJwEOBbIu"
    b"5DqADQHXYbkuwg2BbIBcV4YbUFwHsC7Amljcq2mPmYzfUEZ/A9YcH+U89jvP/Bf3RG0Z++rOWy/XAHD8mgvt2Cgss7BI5x/n"
    b"DcADe9JP7rOIja6MX1qZv7ezZ5cdqp3WmF3IZI+GfZjKeRW0PELIhwnxMJAP6+ycu6h70W4oAjAGNAZi7PhvawBDIDeAsYAd"
    b"vw+Tg5aTSd7aAIAQJVCioLRAlIKSsRuhtQBKQYtAiUC0glICsRab95yC2dy825JfVRZfBXknxX4pM/i0iHxVcXTCSueUdPXp"
    b"kye+dOr3rlvejDs5fXXw4EEKBOeNwXkD8CAwAv5Afi4PZeZYfmUvN0+AlX0EHivE44W8UkTtgjGixicoVm6KlGfi9FTlhB1P"
    b"akCsM7HdSe7ti1YGILlNnNCBCM5ny/cp1oICCgEtIC1PCvBZgXxCaD5FhY8P1PxHL7vm9q8sy/LUQBICsHLPzj/OG4BzeqIv"
    b"YlXdjivV3s/cpZ72mO8eLQer/rO/8O59KsuuFhk9UYirQHm4UnIBbL5HgN1KqZ3ZwjxEK2CUT1Zv5AZiDGAMKWLHE9EKIFDF"
    b"5AcARcp0kgtExq/P2AAQRLk/CSk2jo3N+DgllsV2rZSGUoV3oARKC7RSYG6Qb2wClqcEPAHBCbE8TssvK+BTNPy40vzYm575"
    b"k5/1vKSVFf3QnXdmg4fM2cuuucOcDxHOG4BzatLfddNeufm66/Jw89M+9+5Le/PZFTnsFYr4VgCPVYLHgXZfZ+eOC7K5HpiP"
    b"YDeHwGgEDIcEZASAYq0SFKs+IUKKAONnZYJWJ/C2egDBKl+3DXZskZSQIOgYECtjb6abdTSU1sh6HWRaYbS2geHaxt0K+IRA"
    b"jkLh04r28wJ8cX0NX/yNF7zmG/5tp6xiVa2urmJlccXKeRDxvAE4Fx7XHL6hg8su68znF/bsTnkchqNnA7xOBM/pzM/v4CgH"
    b"rAFGBiSMgmXh7opi4erLeH7F3PAJih9sUyx/yNgEPQsGoMbAoAhPpMwoIPgutjjBeD+CoIIVEFprhdJ7yDc2TwhwSCm+R6ns"
    b"A8z0Z4fD08NnPOMRw6W6DMn5x3kDcDZX/OfedJM+fWCX7LzpFN1V/5p7Du/pDDeeKuDzRONZMPZbAOwUcpfudrqi1BiUK8E5"
    b"SwgLt7mIdptAupQBqK7+hNpWA1B/DkEklRhNM3rnJwsMQQlFipBBiUApAXMLOxwNIDgJ2lMC3q6I9ykjf93BOw4vX3fz5N73"
    b"D/Wzy3ddLhfefqFdXFw87xGcNwDbPOlxkz6NXXKrXDtyN337lw89XHqdJ9Kaq6DVPhF8qxg+Ptu1cJnudmAHQ3BzAA5HRmgt"
    b"ICKAKGsVICLixtXirLaMTqD45K0BBIvzKdc72CYD0IZLUAskBhgDAY49IlKRpIjuZJnq9DrQmcJoYxNmY/QlpeU2jvLbCX5C"
    b"lP5Ud2P08eXv+rm73DF9w+EbsjtO3cGDBw6a88bgvAE4M/Q+GEDP/uJ7L8zn9MUKoyut1s+wFs8H+NTsgl09MQZ2bQPMba5g"
    b"LSlKhOMJLyKy5Rh9NgMQuu8PAANQxRhICmDFjpOYAqu1Vrq3MAcFYOPE6ZMCflAB/1cUPmhpv7Rb27v/zbP/zakQLzifSjxv"
    b"AGa/L+zLIvbLKhZZ5vCfcef7rwbsiwH5HgqfhE7WtcZqaGSqmAQkIdbWT6DW8bUzac7AAEhT/N7CACiEqT9uaxiR3ja9Pwpl"
    b"xgGAJWDtSJRYWrMhxN8J+CcK+V8tX/dvvwCMuQRH9x+VfUf2cXl52Z4f1ucNQGN8/5ADe9U+rOZlXvpppz5waeeUeQGAp1nF"
    b"J0DUowBcoS/cBWst7MYm7CgHQYNxhqxA69usjHWTF9WVNgDYEDUoaQOjZjAAIY4gMQMQneSSyBZMQxMVMyKT7xY3MONbaAmM"
    b"EUMlojOt0Ol1IQJsnlrLlcjnkZsvAPyIFrmlu27+9t+++N8eL3ECHIPaj/1maek8iHjeADgTvw/IMmQyYvukOvSVm79Vqc6V"
    b"RuE5BF+otHqKumA3zGCA/PSGpcIIoAJEEVRUY5pOdMJskwEgAXU/GQA4k7PeABAC2XYDUL43PZ+lCCwtrQKsAN25HfOSZQqn"
    b"7z01VJQPaPA9Jjfvz9n5zH964b/68gQ0ZF/hILC8vDwlUZw3AN/shgCyuLqivvx0dBf05dda8ket4Pul07mY1oDW2vGQpFCJ"
    b"UKYjhzJ++u56OEF9dzya6mvK5xfby4kZncgRI7ItBsD1Poq5WG98GjyY1Dkazp8MpYqAHwBBQmdKiQWsMV8A+YdK8Q/NpZuf"
    b"wCry8+HA9JF9sxq+Ra6oU5/Zmf3VVS8aAMAL8a7uN555wfcuEC+1Jn8KlFyue71d1AoYAbRU5QwsB6hrPln+K4VFAKcmNlxn"
    b"iMg258QMzHN5qoILTGciRM+JM1/X6rFzJlcTKb7UZCLPeA4w2Maa49z7zPK+UZRAtACqq2GHfKQ15npYeZG+Y+6DfJb9nwBu"
    b"LjGC/Yv7syP7j+QeFfm8B/DgdfcXsaoAoCy3feG73tUbPn7X86DlOQCfC509I7tgJ8zGJvLNgSFoAVEUKKrx+C7nLIvkfBGY"
    b"Bm5yuHq7Lm5s23R1TK+u4qH5PrjnuM2Tc2zdA6hfvZ3ze4Qk+oSkkCXoYRhBGMQY0xBxL0hSYGQRfliWzEMrZNZb6EnWybB+"
    b"4vRAQf6vNfZvlcF7f+F7fuH9k7DgUD87euwoV5dWzXkD8CCd/G5K77mfPzTXsdlDlJbvMEpep3vdaymEGQwthTkpGk5sX7r5"
    b"7uR3DcDEIGwF6WeJctcZgIDtV5nY8FfelBGJpvAirnsrAzAlKEVDhdaZCv88Po+hnmmISUiVxCkIWiuAUaK63fkOBqc3KYK/"
    b"0Fb/VzM0h7/4sC/ec+O1N46CefFNgQ2obyaEHysrugT55tB9qSj5bQh+Q4l6CkcjYGQgIkooXQH0xOeG78ZPBysgtsldFjjI"
    b"ln8CiPeajic7Njbi7uyPSeftIvCtbjiDVUFaOO+Tr9U4VegZp9jTPbMHsEx/w+qSVX74xIDIlDOBcSpWBCIiGiJdgMgHIyit"
    b"RCn1XVT576Fr3vKIbzziO8pTLq4sdldWVlRjBHPeAzj3H4srK/rUk6dx/jWHD3f2XrD5Aiq+WLR+prXmiZ2L9ki+tgEzGuUk"
    b"FRSEBX2M4oebMS8g3AaO/xcQsP6q6QJjKaCvsuKV7nVDeIAiRGjar40HUBsqNFGNg8+WpkxCEiBk1XOo84gKUKaunBl2nDkA"
    b"gG63o3vdDKdPrm1oJR+m4fvEyh//m+/5fw4XOIusrh7sHDny4AYNH5wgYJnaEzEAzHMP9bP5h33nVTobPNsAP5jNzz9PdTsY"
    b"nDplRvecMBbIoCQTkUYsTegbhsq2WlvLljaZyXVUzgFbfvY/iWdh25h8LaAGgNHm0JjNQa4g8ws75p61cWrjWSrD5b/2F/13"
    b"MM8+KiKfBzAswcIHa8rwweoBTGbR9TzcueNzJ59j0fk5iDwXSubIosBdTT3YEogP03vWSfFV9nNBwGBbW6BPVUC1SJwfpBJV"
    b"A1A2LdCZwQOo5OnjHoBKpeZiXkQ0hRd+N0Q8Bb/QCGe4DTGvyt1vTDsWrQVmZO4B8Q6r8zf//Hf/xyPF5H/QegAPKgxgcWVF"
    b"L37plvlyLr74c+9/6p2f23yzzhbeorR+Xjbfm9NZVujdjZVqpGHJd11WQUFDDXGByDZ6cb7EQ3lxcXspDAmry8wEXBQvot6K"
    b"/a5bxqJn5GxWV2FKfIqmP0tDKIFBdHb0aPucAnyVL9BqG3ygMihVnnAHAOhMo9PrXKS1Wsxs9vu/9hf//hcveDKuKC/l7Yf6"
    b"c/1+Xz3YVsoH/KNPKgAoFXhe+Lfv2tt72IXPsda8HEpe0rvkot7wxEmY4XBIQEOJYlF3H13lxZ/EpL/iW0w9BYFzvLOtLiWY"
    b"ygL4q3Bs1RSMJbci6HtLD2BKJooTiCrXXNEeSHsAyovfY9kPFPhBCqeoI/+U98r1YFwcAXHFYyDhScnkGspsgRAGQva6nc7c"
    b"Qhdr967daS3fkdn8nfj8yZtf+9q3DQqgUK8srlp5EBQYPfANgJPeW7yt39X6BZcMdPaDBvm/1nO9h5rBgARyEtkkjx9h8cVY"
    b"fXQmeu1+pRGJbIun7GpSW+7ECAZ7CJwlC2vogHQzGYB2LnpcD4C1hk6xRs8gES4ppMOguD5CfBuStOwQSCxeW1qIzTOtuwBg"
    b"R/lHlOXr0dF/dc+HcNoJCR7w6UL1QJ/8L8Rfdicgb/b879wU/K5e6P6iVuqhyC0ESoTIRImM00TiDZLQxY+ltkQS+2FKXCvL"
    b"biXpW5dcwXG5EF3AMZHpw2S/eHzikQilCoC4XsqZAXLtJfgk+dUJTpGSyp5TUZFxKDXN7YeXIo6LbwP3XyruP7zfrZz44oQf"
    b"08pNkOOIQKBgpUM7MQpPEi1vwyj/lV1PGjy6/CFvuOGGBzyI/oD1AK4/fEPnxmtelUPAl37h5suyjWzJgt8vc93nZDt3YHDP"
    b"cRJiKNSQgrvPEqRjldCD6krviHpP2H4x7wCFB+Dt59zh8apv4VbKha637wLHQTo1HpxR8E15rre/oqpgNayEGLUeQPX8Ey8i"
    b"JkbikpqSIGPMfW/nBaVDqXGcViEhOceBQZrRCQ/g7KeKvwlrATATpecXuhhuDk6ORuavtDV/0H34o/7yVde+arSysqKPHDki"
    b"y8vL+XkDcB+7/ADwg5/4wCOtmH9iiZ/QO3Y81AwG1ub5EIIelfjUXbQ3AECLjIAkton/eW3y6hKuVLUxenWC1rno54oBKN1t"
    b"VYeDnJEBQAQDiBkAZ9/QEAXHj6/fWiEHvbnOvIhguDF8r1jckIt99+u+75e/Psah+uqBWE/wgAwBFjnO5S7edsujrc1/DcQv"
    b"gLjUbG6CeS4iMocyqR/zOidiP77LXwkJ6O3uMf9CxDw8ziX6VTwLIIgvlAv1J/bzDdLZNuEMQiRGSYhsl0lAjPXnRjWc/Ai+"
    b"iEm4P33kP5JV8VIN7vm9AiUJsgK+4QURGC5RApkfbebIBzkAPJuwb8goP/P6lev3AMDlRXej8x7A2UL6Dx3KbgJQCnG+9KPv"
    b"+d5Op/dTorNnqW5nLt8cwIKWoIjL5Esx9yauPeuBv0hWwCIBJIpfJFSGAk0FLoIGjjybVsZ6lF6SbnjaA/CAxLosQwhwBsCc"
    b"l1Wgn/ZLsQ5TykeVLIO49wNeeDTdhiRHACnmZYxLAIKWVCLMMq20AkbD/C4YvsvQvvmnXvYrHwUg/UN9jZuW7fIy7HkDsI0r"
    b"/uqY1Ycf+th7LzQ5vlvt7P2LzsLcgdHmJsxgNKCgQ4FKuehpsg8Dg1A1AKlUX7itYgAQIwWdiQHw4/wHsgEojdkZGQAPE4FT"
    b"Kehum80AjA1J3AA49yoHIL1eV48GI2Qa/324YW48dmTuluXlZUtCDqIvD4SQ4IGDAZDyiltvvZjd4Q+I4r/p7Nn1yMHJkyMQ"
    b"miIqmt6LrNCecZAAm07t15DqS+EDjKQEo3l0JmoGwjRaoMQTwwOUlxJrbwDCUt6KAUhVLCaqButwjySTkbG6iRl0BT1+hJ8y"
    b"9dOAEeGWoITZLzJCBFMYa5SB5MJ8T29uDD+oLX9h4yvr7y/5AucxgDNG+g93+ocOZQDww0c+9HjJNpeV4Bf1XO+R+foGYJG5"
    b"4WiYypMgzldu5d4EH5DKfn6O2A9Twyo2Fx9QNlH059qxEJScpAGLaUSnZk5k+n7hrUTTgPDxCkpt1N2U8dvaykA/ZRrd5p6X"
    b"sfg8nITVa1JuoZEEDUjESe+J9bdFPZhq3D8lcdEZEwz2Kz0OEQXR+TBHt6O/3YK/2b18/p+88539hXHY2s9WigrU8x7AjI/X"
    b"fPpdvbcVVXw//PEPHADtKyHyvdnOHRfkm+s0o1EOpTtuBR5qVu8Kmu+t5Jx4AAhWbws//qc04wRuStDdXlH3ncSrrpvsrNAR"
    b"TT3FBDsvEATx6/rZzgOoIRqVrneSQFSXjvRUhf1t0fO5bnhF09D/nmBNA1Mm+iYQlVChPE8ZUqjidwGn2YPJQuNSiWFJa0e9"
    b"Xrfb7WoMN/OP5Zv5H3YU//jHf+ANnwGAGw5f33mVrzdw3gA0XBNLD2CkNvblmXq9KP1CCmBzk1tQKaWUFdYz+lqFAE49fYuS"
    b"30ls35QGjImHxGLvykSuYdklWHxNBmC635T0shUDMGsY0cQSnLlgKZzkraXX24mKpM6BSuiASvmytTbXSjg/1+sM1gf3kPzd"
    b"bDT8jVe+4i1fCHNQ50OAhli/5Pavc+OAFfVfxOL5IgLmhiLIlIhCmC5qEQJ4KbzgtZIgixRhC5ZZqFQaUBFeeaEbRsTc9nCj"
    b"a5iibn5gtFLHMVntE1AGz1b+kO4fznNixOhfRuKeVgqzHPxAAjqvCzZWjEwwqcehQzk+/DBCuah/UKcRM1qlh6BEMlh0Ntc3"
    b"2enqi0hen2v1i29/5888ruAJyOLi4jk3386pC3rhp9/Vg4hdFrH/5CPvXdKZ+kXR6hm6282Y57Yo2EjTeNnwGnE673hsyhQP"
    b"QJwWLJGcPwLOQCz2j0XjXmaC0wB6Sv0NtHImOEHcZ3I9nrNBUHdVe9y6BM8dH2smA56L7NwDT/e0euxE1kv81dVjSleAj+lx"
    b"Sugb/xBYREgLps9pGPcq9QDDCo7g9ClwgcTyHNbS5iPDubnObq30y/J1/txvrfz0ty/Lst23bx/f8pbX9M6lOXeucJllcWVF"
    b"rV71osHibbd1dw/ueZ6h+tlsYf7bhxsbVuyQIJWoLTR6a0uGb7mfCiZX68nGwII0R0CVh8UDu3ijoua1ZRdjmz2WxGtV+2mM"
    b"vtQi2lhrZGiptdoD8uXWUP/OymtO/7Ol5aMABisri3ppadWeC+HAOYNQHllZAQC18ZgrXgCFXxeln2wHw0LnSdREny8orYf4"
    b"f4evw0VTAi296nHiB2xSXf0ZHuPgA002hMUyKBBHGlAc6UAGk0a8lROQpKZeCvf39/VDUe8YcUIZN7TBFBuY1vvHsyKhZ6Ui"
    b"16jqjp3gEO4KHnHf6d9DL5avxPOIYC6R9F5y5a+KioRNTFyvQYEKhLKk6WSqR8urQHXZS172tCPv/OMPfWN1dRE333zz+RCg"
    b"BPpAiojwyy95wSsU+Z8V9OO11lPPN6zUS8SO0RAgpO42iHuGqSqJxKeV8KCsBqwJBUI31o3bRYLMZI1r70fXRSOyNq7LGUNQ"
    b"CdpveD8ZWfEraUBOQwBGBEHqMAE6X4Q24A4wgv20CwEm70k4sat8gCovI2jgWn4VY5XNre31sgURvEQD//73V1/79OXlZbuy"
    b"sqhuuOGGzjd1CLC4stK98dprhyDlnx++5aWEfW1nfu7Jo40NcGSNAEogYlnjOqcUfaTZvVdAha8ZmydKAmxrm3G0s3DK7UzG"
    b"bPl6K55Iy9hJ6kHi9A5lOi9heN1FQKS6rc0Xldrv638hJSLWWJsLTKejF2jwj2ls/nv/83WjpaU3HwZWzaFD/ey66+6/SsL7"
    b"fdz1b7ute+fpew9Qy5t1r/v40XCQw5HkrqfxzijsITXHRVl8rNX9m/AEIlTgMAXo9xJIKOpEu/LCp9YmFHenf9t4Ci/Mowf7"
    b"qUhKcCKtnUpH1tKC7UypymTNA+KfHRcEaWAaNlQNIsEKDEOKSg8C57PdjMUEXLSggMy0EmvNABbvNFb3v/BDuz5d9Ci83yjD"
    b"94sHsFiwo1aXlsydp0+/CMB/FJHH01qIVxoXWOHYai+R14ggdQ3vSeBhTqxPmQ6SQLgD8IrO6kwrK+67VN1o+KzC6rGsdBBr"
    b"5eu7HvNMVp/JkCFapecp8kwapfkiKdKcMYmFZ977EpH+Dia2f1msfhdBRTQEoVpQiRcxUj9QWzpMf/COvQ0RQKwxttPN5kaD"
    b"0Yu15IPHrN776z+y/JZ/KHUG7w9DcJ97ANcfPty58dprRwBw/Qff/z0A/1Vnfu6AMTmsMYaggjhovyOyMZmkyh92iJTaVlZ1"
    b"5WxDmrRjpej5C98LsIHXUZ7PFQQpzxmSgoxMacOmVA0uBpNy6KWKiSKZQD1YuYScxOoN2AlhyNcKKAdtkwcQy6e3OIdTC4DQ"
    b"+yjj6YhsWRvJMURYk570GaeAXbwaEGNhFqeAaDqRLVSRnpyAlGUdtGe0xvt553OMg/KwnrEIjCr2I2m1UsgypUieHGyOfrun"
    b"9dte8Yq3fGFlZVEDi7ivW5ffpx4ASREg75Pq639/yz4avk53swNmOBw35QCVSDzV16oRZtt2Nk0WMbJvaRAmfQE4XY05g5WN"
    b"EYPKlXHbslz3p1KdoFU9gWzx1LXpQxcDYAwDCNSAHO9KJriQeOBfOICkARR195cAF1AiyhhjQZpuR+/udbMfyEfmxMrK9W9Z"
    b"WrrxRL+/7z73yNV9OPvl4MGDAhF+/e9v2SeGfSV4lhIFGKtKwC8sxJkIRM5K8GnKEKRQ7GDfkufrinyERKAwCxDbhoA0NFaw"
    b"krKdJRBg4ZP6gaBtWJDIiBs4oROvtLMGRCj0wSia7wlrIt3eywXGytWaTABzleqoyD0GKkrIKmAZVlD9SpVl0EKMTguxihJT"
    b"wAT0UoOsLacGrCc5NiUPEQWLVQ83h/l8L7tCCf/FcNh96W//9s/tWl5ezrlVvfdz2QAsckX3AVleXrY/fcutj8bm6MeU1i/q"
    b"9ObmmedGCm8rOllTFN82kzx8XTewUgZisl0a9wnj2qZ9EKQFIWMzyKAK0F1TGInGCUTKF9u4QlsZZ3TymFVr5BqI0Igoz+AK"
    b"/C4H9IxF2DFhakSsA8BJJHXr9gpg0nhIpE3ZRBQUVWk2v6Jw+guG2+CCjKzyGaZUY6jNzSHn5joP73X1L8zr0y8EgJtu6uvx"
    b"V6A8WAyA7MNeWRaxK6QemPUfmNu1e1FrvWDz3NJYiEQ4romS0Oh+nOF14n2V9uxqXfiZ3NfUV5Dm6Xp/ojnctp3ZuGMdkal6"
    b"WNWVb77augExy+TgGd0pJaJGo3xkc8P5uc6ju3P6J1Z+/yefd911y7kIePDgQf2gMAD9fl+W5bqcpHrf+973PKXVK7pzc99i"
    b"BkPDUS4Q0WBDQQ8SKzvS/P5kzUDifSBN8GkiAknDE7Hzlduci4m7+VNGvCTdjPgUYCRyTU04abQvvv8R0+rzePKVc9Pn/5eg"
    b"HmukxGNdmgr33YutGAsBJCDnVFF/X4uwWj/gEc3oknwS1Y6Vc8ApgPLblIFEJtIZDXM5dWp91O1l1xmxr1t5+6sfOt5h2d4X"
    b"ocBZNwBHFxczAPiZD3xgP7T+z925uf2DE/dSCFWK96UmvgpAnzMKBxAX+5gMNptw7UM8ICj+US3d/9R2VspeIp2pXY6DUzDk"
    b"T3ZJdrRu39Wy6rbXCYDGVuiSSTc1em4Zs8S9qSCz4Rocd/LKpOCnmHwBm165k15cDkMNE9CGk9xWaMBemi/adcjlBfjSaECs"
    b"LdnkekSJAKS2uYWA19nM9ld+5zV7l5dh+/3FzgPWAPT7fQVSVq++evjTt9zyBJPbf6Wz7FoF0XaY56rw+71VPbF6hytDMsae"
    b"ZdWPDd5EfB/66+XqkqoEDOdTzKMIvQIvPSmYNg+RZNe82uk4W86/fbji1hOkvYKqWfPvBVGtJERFPzBln7w8vZfvd1dn3xWK"
    b"Sa5XcCHQywJ5AF6Q5/exA/iaAfC3TXARdz+6BkLUcJCP5nqdnQJZHGL4vSu/9c8u2r8fhkXXsgeWASDl6P79AhG+5oMf3G1G"
    b"9oeVVj9qRyMzWl+3SussuqiE/qsHLNXs1zbmD15LSgIrwLDaAHllPUEMGKubVHJmEfcD8tE4mjkLBjA1NF5bVdZlKJj+sT0T"
    b"GhB8GBo7v6DKJyIxep6q9zNhDAqsNQJcPD+nfxwd9eylpVUzFrc/+MAyAP2bbtKri4v2xw4dmtMj83IReXGn2xMFpYv1Q6Ir"
    b"fqqQJybmkcoU1CH/TedAw/lratxrzxcxAG6VHGVaJRhLpIVrLYOhx0bDsRXEX7yYPaRASsLKSsKqC3xXWVUMMKvIh6cQVJzD"
    b"7R9gU4j++EU1BAhag4XnCIt6gsnvkZIKYniletBpWTZVMq6qEEm14jEbDQ20AAs9/XQFWVr5veuvGO+yzLPVlXibT1q4KseO"
    b"KYhwD/BoRbxyfufOx5rhaFRYO5lkvtkiBGij+oMWk7gJMGziCVQmuKSNDdKiIXVCIq7TzGTjbImsXVKZbrItWHXUu6ukAT2A"
    b"bBIeVUG+EKr04/zQTa+5l9Gw0BESoZ9OjJ6bofCnKxBaVR+uCIyEWILDdESlF2FqP9+AkVS0lqPcqm43+24N/Mxtt/U7IuBF"
    b"F91zVvCA7WUejamUIiLDn7n57x4hHP7/lKhrFCSjMUMogYII78vyt21W0ajQarbh/BN2YfQDtnBl52zPWp7dFcuj5TYNihbX"
    b"yO2/icmUMglVdCzfHAwHu3fOXTQa5j/06Y989ZaVlVe/a2npbWs4C7+u2t7vNuby9Q8dyhTyl4nIq1WWZZunT+cC6fgrnzjP"
    b"LZB/2JIItBXvoIkUhOl3KPN3dau9sD5D4KYEKZEUXwkGeq7G1N0QBzScui1bkAUPrFJFB7EB13Bj3qquAn2vIGjH5nkFpRIv"
    b"E8QgtzIwrFmYgHsSVOv5KcMJeGfd1wGJh4nGLWHVIBLEHxfsDEVNgaBPoRMeAZ2N9QEFuEis+tfdU6Onl5O+3+9v69K5bR7A"
    b"9YcPZzfi2hwANtH5EVj7z7O5eaHNocYNPf2V3/u7IMMHonZkoCMXMlsd2naU9Zryg4v9KpJPLUUzZEZ4XRywuPIRjiCILWjB"
    b"jABHrlOfKor0PDG3jsG2XZ3rQDR3YkslAAn3ifEHym/n5TS8PPlUqNMDgT2GoK8X6F56yeOXiMKxO/EAiSsLhzG/N2FDWfBq"
    b"SzGEVYKRVGCYTYgeR1HMaTpdpUF82zDPv+9d/+36z3/o85d9fnzKvpJt6jq0PQaAlOOrq9K/hpK/55Yrhta8vLdj5/7h5uYI"
    b"gqwo8Q0B3JoRHHebJgakjehH7HVs4joNLZiYF96kTQhEsM38QlzXU9jWsavbqdo0RM7E9a6kNjkbkp9M87Q4R8sYUVjHorRu"
    b"XJqwdRGEPiQMJX5n/5dgzbilA9j6RwtL/N+fDMUiINaQSkQtLHS/Z3OQf2V5eflXAODAgX7WEMe0fmwL3XBx/369urSUP+NF"
    b"L7qII16vMv3irNNdMPlo7KxOE8c1VE+p6vvVrLZRTcDIa5H0cYiswt628BraFP83zd+AUsjEaeq4CG6XW/d1WiOQjWQlFebi"
    b"gWjF3KS8N3g9aQpS+AfKlecqiD5TF5eRa47IekWkvQGn8CqETF2kXVLckACFZ6xOAFF9wGa3v6otiAZguzIspl2OBBZCY0Y7"
    b"F7oXDzbzS37oRdd++KprXvK1V75y2fT7UDfffOZYwJliAAIAFx4/rgBATg2fAthXd7pzlw43NqwQepLyq0vZIY4LqEBnb0vF"
    b"QDa9j3JEK8GWGEAyRSjVNCPiQpkIKgfDPEpJAIIX28vkYhmZ0gwSaR6mWJ6vNUTFYH2b1iw6bVSiqXR/EkmQ2xC/YYkbZ1ey"
    b"KdVioEraTlhB16MMv5CBF2sbFkX7rVcd6PVlZDp9qFplBIKwKtGABUC2sT6CkFczM7947bd88VsLL0BtB5R+RgZgcWVFAcCN"
    b"r3rV6Of/5n3PIPHqTrf3ECEFxjYy/KLpHLpcbmmVppOtlgrXVAWmuPu1K6m0ZCuyjm4cpgRRSZzBbVNWehGsoym1z0YIa3CB"
    b"yv1xGn1UOABVfMDfJ25svE7CDJp9hKrArBFyjRkPRCZsrOIvJoYSTHgPsAyAPl/NOBw3ia5L0Tky/rWHwzyfn8syAV4wGtnn"
    b"/9UbFy/a9ek7i/XkzFiCZ2IAZN/evQIA/XceXlDAyzrd7veYwcjkg02rlOg24aEqSCEzx6fNhWUz3QTV4r3YttrIINIcZ5Y0"
    b"EVtm0epy/WcrG9gWA5DWGEC1hLfxc9lyrDSNnW2/SZEPqGAOqXRj9R4pQIyxVivMLXQ7L+eehWdc+6obRwC4urKk7hcDsLiy"
    b"opavuy7vr6x0MT94nij17E5vriOALg1iI3Ov1VPqOwBt5Qk0px6xxWutS2u2EShBihw0HRxhBC2Tyqm47Unm7RqginpwjDX4"
    b"xJRxRyf1Ny2mcuTEYqs8UPUqPEbo1D1LMQbT70U6BUfdesa7C0VIPWlPIXY98ZSh502F1yXQ+ciAlpiby55jBN+/0l/snpG7"
    b"d6YGYF8BIA53PnRPLviJbm/uSfnmpikGsDf5URv3t2H/BZyBWfAARCr+IvupGkMBNuyXYgmiBYYQCwPg5/UnuICKx/Je/F8c"
    b"J41Q4myYpdSpcbuT2GsbZqsgXyQsUGGlHhl/xuL+uokfxuhhMVBsYscmf2iooq8jtORI3C9EUrW5ch0IO0qPm8dbw+/Y+fC5"
    b"V7zzhusXAOD666/P7jMD4FYm/dz73rdLA88V8tmdTm/ODIeloKGUAFtUdCMWcwbFPxM3u0aV1t0v+XSAvrC8GEHcqBpWQxUc"
    b"oyLLrGrjHbLFey092jNJStwXj6rEc43mFZv89hihxyU/OKQeaeP2R+4mbUSUgYH3zrS37ykQJ1x7MiUCV/nbycAoAWRtfTjc"
    b"Mdd5BCGvnMfGJUtLq+aaa+7bNKC6+eabefPqqvnOH/ynT806ndeIyFUgVSH6qWIDMpa2a1R/iZ5Dqvu0KbOT+GdBGuZQXd8t"
    b"Saf34h8Gj73X2EHU+75TKSoJZD5alUYn0oVxFNxV3y22iaNWIGFfgKrrriaof+j6+p+lYoCcJFKVyQyLn0VQlfuQUDUGIiFB"
    b"6tjUPWTL+1vTzi3SrBaRkLCoFzA7Fzq9wSC/xAKf+L5/8thP/NiP/ffRVk3/zB7AWLt87AVkOntub37H83XW6eXDkVWErq3y"
    b"a6L6NoUKExc8yBDYdjE6nIGkYuIgjGxj7PPbpAcDPMC2FCyx04FOkYnEuI8FOFZEHP1xt5zQTUmE7EiRhhRgGy8mJhwSusbW"
    b"SyaGyUsVpvq8FF4kRZhqBx6buLYmPEgIgabTgJHwINqKPObyx1H/UCugEh4AAfuxFBuB3tgYQgl2CvEvd3xjx3PL+7myMnv7"
    b"8ZkO6B86lIEUEvilv/nAs80wf75WuodIx5amiR1dOBMGAEn+gI8NxNCv6GdGDEObNGBUBDNxHAIQ0yUVed8J9VWQk6nlYgGh"
    b"QyzV/gZbSEBE3HHGyTapNFuEXVfRTHAqCZMxNSIxdYuUqvc5KeMSW/lRQwACIxWMiXuTampKRkhFoVpQ3JsJ27GLUA+HxioR"
    b"9Ob0NUqZZ/31G35kB0k5cmTfzD/zTODB5bt2CUR4cGWlq3c//BXd3vyTNk6czKGRKRbhhFRDn2RjHlcGuiYrMlnIJEInniH0"
    b"lcT+UtMDrkKqCa6dAeOwqTlRtElNeGBdbM9UfMQtbNvOlF/qWJcl6L63ldwdg4xEJCvBGL234XPY9D6DprR+diKKAaD+b2m5"
    b"b1UbGlAiVAJqiMDa58g83isi7wFArixqWVo1Z8UDuOOaa8Yn3n3llRQ+a2HnrosAS1giyvhDsyvtaepHEGdxW0zXEHOEMnHf"
    b"VZMrHwkrVMDiU4lrbDq/t80FK2017KjtYOx4CZO1YtI7TALd/xiYwOS0koZJJ7HkowuITUoXkVbpjaD4/vlt8Jk20aKb9VkB"
    b"Zx9lpyy8xoxATFSkktqbeiXK7Znodk+qhCrxXofxUIENnkKMwDTuS2WtlcFmDq3wbGbygzfccH0HAI7MiOu1MwCkkJT9q5D+"
    b"/3nf5dqu/yAMH2mGIyiKbFnAo5JGqlfWbU7hSXuBENaFFtvEN4jhAWjJWKykBCPaOhO3n0WCSIImqVKR4Uxr+cDvO+hq36f0"
    b"9cMUnxt7I1JLUDeZWRf3+23Eayd47fvY4rbI3wmhj3QKsibMiXEVojgCy/Eu4zJmm1+wc66jhE+9EsPHkZSjgNl+AwDgICBL"
    b"S2Iyo6/KdOflnay7Y3PtlAGoWxFJEttUDYhfty1Eh5tYe25KUAVIcfiZKnK8tGQRqjbA/hZ+HNb8WlEV4W2Qekh6zvdbL9v6"
    b"MSatr9Fu5VvPRpy6L+6BQPLcWJBXiB0uvvd3f/ySpaVVwxl+/Kx58aeICA+SwKFDmd6w12rde5zKMtiN0RAoKL+cNW6t2S/2"
    b"dxNI7YFmLX7MIl9J1lQNi79daoZLtDqxIjgqfpvpYNWViBMvMl2Nyw5AExjKKzFmNPQlQvXcNsSEMBaWcQFNRa2XVTRb/FXb"
    b"F9pgJG2WEPRIgHhApImoA+4hEO7wj0Ol/brXRjzsDlzh/DuezMR7jXQODvCAmBaADzyHGgnO3x52FXYepl5fH4rWcpG1dmk0"
    b"GrwbwLHV1UUhV0VaiEg3GoCDN92kAeQHD0Lmv33uOmPy70KHsKMcQlFREQ4mkkstjUFst9oxnDQO7VJbytnLBtdgZdoUVEWQ"
    b"dTsBZqYCM3V8A2EVFBS3/XjhqaDoSFxus4VggSRAMkppFIpQo1ABKcODWbT9p+lSdxJjOqGk6qrCk7quyn6rWHyNWEGMyxGo"
    b"pt3gusWodhFGTOgj1PhPlPlWslkRAxDVDAzAPTfN5xGfXOPPQNY1mPTwPj8ORopArGXe01k2BB5DY79tZWXx7xcXVzdFwHLx"
    b"3pYQYHlZLBS/c27nzmvMaGTMcEgUyH80FYM4FTiacgOSwpmIgX4R0lR9ZaDUVt7VdvhBms4cwyVULG2ISI9BRERHG7CSqRSY"
    b"OFJhxVCm6/67JCKJYsxELQGtXoMBkVp5wr+Z4XeKAISIfXek8AFUFYIjvQNdrKDS8QcJ6q1bylsRBo1hAH4sjzBfj1h1IiqG"
    b"BYkUoT+mWEMZHi881loqiMpz+50XH1PfVq78q6ur6kw9AHXwwAGLfl91nv6SS2mHT+7NzV843NiwJMeiBQErUpjOH3mpPqnp"
    b"lZdIn1ESY5MNIZ20Snl77jtj1wy/iYfnYyQ634bnKF36aNqQidgi+A4MvhoDqa1x6CKOnPeZlbtVaKtM75PSP6i6dlVdvEnR"
    b"ENJhSmVyIVgYKkYKkZRdhK7LWCzPVmlRieWvXcJPJEU4Wxo09vYk9NK5MULSznX108yI3wngFgDYu/dIIzShGog/SkSIJx/Y"
    b"rfL8ZUI+ZrSxCVjKBByLpfMaUPtkWhDV1mAqki1one6LIvGSPE8snefWG8hUDr5yzcqpDYimBGtYguG9rMtaxHqPlGmYuEKf"
    b"b3TT9iAQGQ+IU2786rfwQkKIoyaN5xQBlc/WRTbONaga1N5LCdZlCgJ0f5J6JqG8isXwPFWWoAqETpTjUYTXoBgwIclE2hDJ"
    b"DAFKdQhLKyR2L3QfooBrb3nj4kUAcNNNsGygXGSNxB+AF2DugiHsD3Y6vYeNNjZzEckEKcveEHpL1QjLGeKHs6xknPHMsfi9"
    b"7vuoCMbsgnASEJ+khnzksnYVCzxCQm7aeGbTcSPKFKGwqhBUh5NWxTuq1xOmAiuNOaUG0GMMwKsy7yqtwYGaVBsiMX+V1uuC"
    b"heN74wN2k3OI28qrCuJVmHyB9FdSBDRUEw4xgQrgBwfIjGEA1ZFsjYGFeVze09/1vl/53j//m3Wsra5CoSY1WOsB3HHqFPsr"
    b"K11quUqIJ/S6cz3SWGntNqb/rpHgS6bn6oqHzurTFs9Zt7V81gFx0sJltC2bjm1vGq6taW7XzTDWlLTtmApTyk1paGUjpKtI"
    b"hlBaXsv9nS4syu/l9PrQ9DJ1GcS+fDPr7V5eXrZXHr9ezRwClG2Ilq+7Lt9hLn+EoX0BiDmT51XGH1rIctUAbAppTX3MSrjB"
    b"DGSiNuIdZ/wZLXUDUgVHifPSSRmMV/upRt+0JsjREmwYelINz6uSXyHARkbkuVklAtUCXZz5iVRIwZbCICkW3yysQ7Q8bpbw"
    b"I0YsipwjriM4/qUtaeZ6eheJp3eVfviWiUD79x+c5JT0fOeJNOZFCuzmwwEEolRksKoWf0cpuKxScGMsP7Sg29bRctvE47F9"
    b"VIvJOcv2mBFQBY25ggkEuECJMUyQ/1I0BNOMwHSaBaCgeM5nu1VKalibkCQ63zg5Ig0+VGRiquA5VhGyXnpSYbodNsAYoudw"
    b"G5BE9kmw/yrXFwiAulkEhXaGqHbSB+nOSijEyOup4tIFKuOzPvBbL7/0mjsuM5P8cFsDcORIGWAIAbt/fn7HY7TSYkcjioWq"
    b"tMNmvKoPARIsDS6dqnnd5JbVudZIhBgpGS5EUGxBvfBIOHkrNekt258DiPe/C3aihJFgcLZt9TXZEGBEag4TFZm+FHnYeKRe"
    b"fWgKtCV+Rya0AloYvFj6NSSZNY07RLpYe9+vjTAJm259WjdRLPVgYCCAys3oe/NNPl6Wxw1EVlbj2oFZgvlnQcpb3nHTw3KY"
    b"fd1er2PyEWTMLtG1gzViHBCLtwTR1tyVfh4SVOu56UC2CLpaAo7TBHp98wo2GBcmMA0kUoOeEZRS9CFuVENRk0mSz2EsTj9/"
    b"7AWwXSY0PjHc4h/rr55+u66Eoi0T5bhMde1tAPoYkHNc8hFZ+VxXt8CX2652APYaiUYIP9W/HQARsfQfAgYhKt2FQsJP9LMC"
    b"MLEOICSo8pGFVpKpTL7d0jwZwE0AsNg2BLjppvEEf/tNN/WsdF4ilCePNjcAayFu4U/gskoiNVanyV9bGRi81g2pullCAO84"
    b"66zchO+O2/RnRz/LOlWFrUOQ6edKcbxCvCLRrVocI//THoGhJiBZ9QymNT6RtYyuTXbifFfTwPPu6Ih7IsLsm7rntYU6E3cf"
    b"UzcfNnDP7SQl53oCygbn8NKR9eGEakgjuiFEeN2qAUeIH9dc5VeLGzSFDUVzUQGNEmD3XG9ekfsP/dqLLwEAWVo1sRbjlTc+"
    b"tvGXGgDu/vJQE/LCubn5R+WDUT5t7d0M9CEghVVEMBLsvjowDVupwmvBP0gKjSAOEqozAAhj+6mUmGjD55ZNQyaT28MAYiIh"
    b"LcQGQkUaV2EoZBkQQbOPqgFItcR2m4hUWZAu2FetK0izBYPmHdEJhbhKL6tpQSSwjBAURWRCI1K3gMhn+7wLerUEEtQ4xMRC"
    b"oh5U4RkYa2HIx813ek85fMM1RWvxm5oNwNxDHmIBQC8sXKpErpqbW5gDacoUaSzmD+ORCSkmEh+nUjlh1Z2aAQOIt7oqUjq2"
    b"HgF1xUfd1F65EqtImimGAfgc+kivA/pcgVhFomqDAUSScdE0oEgrFtssGECqAagn1IlEeObhIxaq+GGmf9vJN5kKtPo/oETd"
    b"6kB3IaGF6FduuMcXHsdknI6vRzHGKKz2g1CJ8LecB9OxwwoWNh0nTIvgMsHdTtdvKVqLzcGIHZFHk3j+7s29CgAW9x9TNRjA"
    b"ONp+1bXXjt7w+3+9o6P100nssaMcAJVA0sQezvi6XXq4Lpz3Ts2G+IY1lHd33xTxU5URVkM6JTyvin0mqziBOASichBY5/ot"
    b"4iIidLwAugSgspKoUs4oNYMnoPpy/CETQVDrSnrVs+naov9Tnbug7RYC0Q2gUg2oWF1lFfyVG5Hjwo6/1S6/9PGPkOzjdQKq"
    b"IfEEVOBqnI9qp+QKMasa54e4gcQsPwkSZkcve+ja5uhpa7t78wAG3TuvEOBo3ACMxwkFIszmsofT8DsgsssMh1CMLCdbnMi1"
    b"64wkyBnbyAR0h7ptwUJ01x8BYQPw0tasrBKyCSN6iFVjINXGkkgUCLnpvVIrkAX4J5xiAsV7toUH4HH6KdN4OtTAm9BkZ8in"
    b"J8A9xFz1CJtQlV2DE+XAIRNwKwaAgSoR6wxAxFCgAgC2A/ck4PgDflNWPxRrAggBwtq5jsL6Jh4+OqEeA+DvH3PP00bAX3nL"
    b"8sQlOHjw4ORzNOYeJuQBrfR8PhzaUOpbIqtszGWvYwIqpN2qVKpIWuyjUsdEUjCxtFTp+iPm6jnAoHKBOffpuvh2KgU22Wan"
    b"T9gpM61kpylE+BGpEKeWZibRduZsYSCbdo4ZaRWMhWpqjtGOxOK4yAphbFuFMcchwzTWLkMINXHlG4REEVcskmjqmRF2IaPj"
    b"XILvfibkyLqeGNHwMkbrJmRkDEnuhsW3H3rT911QpgSTGACLNKCCuiLLOo/LdFfTWisY5/7rmG8IiSJtJL6awMGtgn8tn6ol"
    b"kDfFMQRiZ2AQol0bsDhYKa0Yh6BM5cAmkmASab8RLw2uxxnKHm8pCW6/FBezyHQFqHp7JqCtlwznDB5Iw7VMKxSrYGbq/GjD"
    b"BGwIlzwtBUZYmDEZ8yq4qYZDI0qwAwrP3WPVtzhlD74B6Pf7anl52YoIf2P17y4VUfs6ujONQQq2Wqpjb1vxzTYioXVpQWlZ"
    b"vTdrOtBN4SXft84q7qQLG/dvc/5IarJVCtMNBeId9YISG6JeGsRX3p1IfdPvulNJ8TlPHaYBJ/vCY/IpZ8JpV2zT2vHTO6+F"
    b"TN5DcFwq1WedZ2pbkH6cdCXmZNuk8i8wEKkUYZgm9P8uQco4qzBmUKqpypRuQuXXV8ORZSYyR2OfY7R92MSGHOyLhwEcPbp/"
    b"wi9T8qHHkPaxtNqSVAI1MRnTenzxyTVCrx4+JPG40tnufmxAplvV7ElV1iv6AWwBUKbwjUCxx11TEY3TghBI/LADgRtvJUCb"
    b"xV+zy/VcEU5MOv3fTsQF6NOApfjbZQ9S4uyyQndd3NZYiBUW13PaUU70GO+/LQYAxHPjUvUYgUSzkEARKA70SQDqBb/nVIvf"
    b"V+xxATypxvaY4AnT/cjpeKEwkMUPKi+dSRJqCoj4VLCp9y1O7wmWYGDe0TrTgktNzoeX6h3EQQDLUw/gwudfqSZXYs23aajH"
    b"m9woWAu3HaVEqL+1FX9s066qXdVgiobpCoJWlHmQbrVUmz6q6xZc14XIJuLfFmQoz1tJ1TYk7jHCQiAB4kmb5hq+apqVcSwl"
    b"QcdFGBZ49fOoNNmIu7RhSXCVYwA4tQAlQMhQahwJv6jq8YTYg8dyZLoyM9XyK84AJSo9BmLs2TqswOVShN/FU1iaQsKZEtLa"
    b"Kw+/fnFPsbCwxEMzALjswjlnksuTu53Oo4fDgaWIElKVplcigjAyUZ9xtskUFWWCJhv721uwxV/tYtIDE6ZqXKIeSpx7JvW6"
    b"pXBRf/rXUHoZE4BWgk5cEzbetGjAu0Y7JeWEjoUSZ7ua7l/hCIOwaipkOvnNC6PDQjdQ3EKh4ot7NXvCwhOIgHoFyh4T+VAT"
    b"kYz6GFqXzEDLQFSjAM9clxiMuNZTxp9KteB2JcAq5CNUKcOVWv5AR3BC4Q3SfoxRehnX9KvwNxhN/cXMsEQ9LkQ1ICXShyE0"
    b"GNPaAyprbDnnHy+9wZMA/C0ArK4uKmDVZACwH/sNAPzWyi0X5VauWNixW0bDuw0tRSkRzpyaS7XZmS2/t92CII0pSKKxcCRV"
    b"wyDFRBViPPGaagFKEKaY7LbwWixatlcL/odEpDREQMdq13EmArXZepGPBgANKTAOaAnIpZl4Y8HTdIMNeA00EDUAXp+C+9IA"
    b"JFJ3Ekn9tZ3wkirUKr+KJSwJa+2+jugnkHivCLh3711jD6Df76ulJTGHDh3KPvtVPF5luIjW2nCwtuX6RItV0D7uRxuijyOH"
    b"PeGsSMQ7CM8zVbmerujiexvKwTpIh7wTuNWVTtDOtSpWr6PSXVo516SCnVPVUfQWau+8Jvy8FO5xBmWCXpFLxL0uB2OZqpuU"
    b"xnpAoPNehMevHKXfaPNQRCS9J8eK89pOuBthaxRV5vcLJiAq7VOqFYpRTcKkm19v/NFiW9P+bX5JIcRawtJy11z2sM2hvbrE"
    b"AfbeNGYFqv37xwDgHWvZLsnwFAVemG9uKliq1p19W/Ph3UyCYKZuQGf5qWy7tKGqyVxMzyHti5Fap05b3C9HF4CuYjAc3QDE"
    b"C4GQar2dap9VI9bRKq1n67bbLaXy/BQhWoqKtBQmqRXzqOsMtBUBkxRwikadRG/bONinEpgL5jvzsPYx73rLa3oAsHn5swkA"
    b"2d69ewUATp7K92TQTwB5kRkVyj8QH+x0AcdUc0xWV95U3O+28ih12qZIqM8OZAvsIDyOTswubvwerszhNvccDgYwSQ07hTY2"
    b"wC3KuHzCzCsQSXflpkzjfCqka4gdjvDYyxcohbHSYxn7q+LzCkiXMobbhG4Y4OAgLDAAiWcDpmKXfjovROuVddJqtkitWesJ"
    b"dHgpMDs9j7LWwQSshwco53NUorW2RLX/AxnvINaPYwCuux6GAxFNvxgFt1La2xTPh1mGCC6QwgqCCSapmo2KzBnFWAuAex6W"
    b"f+kqAB+/5vob8/4dUGrXrl1CUjQXdgjlyVmnu8OYkVXFlI4h50Ckki+CSte6S2xwhyKFEynENZQVQ8IrUWjZv7CGxxCeQ8P3"
    b"BHQbpWIEHkRxHmmZ+y9LlGNtzssJj1g4JNLg3jPIDDKgBjuZCNZU1dXw/utLXRtW5lpdgQghCPHqOgVWKb1gJcTxxqDbGKWS"
    b"3kzPkeh4Z1qMJpRKF1ZDkoC55/cwiHcZls2BpUB2mHx09S1vXJwXAfcfXRR1++1zIiLUtBeCvKrXndOwNq9N+UQqpWINOsD6"
    b"rr6NIYITJrRiC6aonU2lwcG2ihyXrQHh6ghIE2qvQFkJiETT0EM7xqM2/EBaQj30rMri7QkwKAltgApoJf5q5YJ6MWAuiNFV"
    b"wqVWUbVe26JhKM5Mv6+1u++Tn1JFTmBYD0CvlBdeOXDimkGvg1K13DjVVDWW2kRFMCXESxQIM9byvbCj5XG7zdoeAFhcBLIj"
    b"i/vz8ceYbxGoncoZAKHbHw0HAoJPjEfTlAasTxFKAB7Sa9rhpvcE8S7rpfvrpfcCN5/BxVr3uCAE0EEI4AKQblhiA/CQRTqv"
    b"fG+yn/WZQWIAUdPzipqGFiVKSctpCFBcm55co8CKQDm6geOMcHG8kbSXVrjn1UlkC5KQz+Zz2XWatmDnTdl849LaUrxjHF7o"
    b"qFHw04Bptx9BTX2E+BOT+05U8lXblsNr6eWGBEhV8UUQfOF2ufbxZikS8QaiVYPFrsYYwPJCCq9WVLsAfO3W47erbFnE/tbK"
    b"bRep4YmrREFo7GTlrav6SzUA8iZ2iqV3Rjk/icStiUOlZTqC1a/plffSifeDD9Oo9hS0wTm8FZfiZAFY7bWJePrAy0iUysBF"
    b"ytG6RCQBjAcAllq9xf8EWNSLTpzHUjCSE3OoQCtCKiGlzSqrasU8C4AOaYBQwVZSemEFomJI0nG8E0kZgFiTzXI/CVJ15XHi"
    b"LD2BAfCMhlTYeJIo+WVQwcdKlo2+R+2wDCXgAU3Tzgn5OlbZOsbafL6ruoMRnzgU7iAgR+64UjIA0MOTDxPIlbDj5gIT8k96"
    b"/ldaV0tibrVO/QW1quFcdWvjY3+76UAUq69yV3nn/LROqs/6HXPonJeIp/MgnrCNo9DjGD6BAwI6noqdgoCUgL3n3rTI/XBj"
    b"e6qACOR8T6NQ8uKoRbTqaq1UB5kAGS0ya9GhgaZFx1qofITMGGiTIzM5ZDCCGg7G3gBoxkZg7JRMKx7LVb58luGYLcKVEhC0"
    b"Ew9CFdoKrrLvtIOOdUK6OAjoxu7epMeYeBQj+EgQ7CjQSftNb7LyRrh1tvuG3E0hhlIsEoiXJKvuWryuruqJ6r1WSyYBa81c"
    b"1skGQ/NwwFwoAA/feSGzYrbshcgjBRAaUxaAtVuVzxJbJxUqqMrfEkVC1ayfRX/1j31HFSD5jKz2RLWLcJi/91n1jK/6qt4D"
    b"KP+f1AgQU08AECo9/t1pYfIcMIZDa2iNhbWW1ubIjIG1BpoG1hio3IihRWZy6Yw9AAhEKwnbd9lJGk8FLD4VSfN5DELU6PEV"
    b"bMPQAFRX9lh3oYQBcNuYMehFCF9MtLrCsyqLHiUFVd3yWIvwOrdeEsrAsaYpSKz0yZoan5zVyUQeDgC3Hz9usyKEfQiseZSo"
    b"TGCsJ/2VnOc1giB1GIDbZquNcJBFpGlFrLWVuAwYH/C2YNytCFKatoi5bayIKPxM1RwCwMlgTCawCr37gPMcA0Ssfy0s3htj"
    b"AIUACEllxVBgFNFTe3ZCdTTM1+42iuajVuTDovQnhfY4jRlS6ZMA74W2kJG6QJTdJczmFIcXQvA4kE8R8km7LtihsDnA6MRp"
    b"KtqRslYpWq1IUeTYS7COPr+1he5BYRAcgU/f3XcrBOlU+tl4V9zCwECmA1q57bcnuVBUhD5Kv7mU5SqVg9zCGwkmPCv6h3EV"
    b"X9/15mzxe4L9lzIijZO8rr5DIJYWIhShuuLz/e+74FHLq/dmAKihL+nNLVwyHA1AayhFzRHj9MJKoUmtrPcM+8XCCEW/k7AE"
    b"XYIZsAIR5vCdcyBkAkYW3Qk/3wUBrZ/7r4QRkgzdQUzz9O65IH4IIEENgFjHiLn726kh1VMjaZSC6u6cz+yOXmaP37uWnzj1"
    b"DyOaz2nBpwn1GYF8MlvY+YW/uPhpp5DoGd/vU22+ZHXXWv61R0J1Hiu5eez6N048piP20Yr2ybt2L8zbzU2YtdyKNVaRWmHM"
    b"/59MfselV/CVe1XBMSjfU7HmGm7DTRcQ9FZ/BNTfQOlHSgMgRSUfJ2PErbRTQdqsPA8hxTZxKvo4ifJduDcE/vyuz075Nd0q"
    b"zxATgFfBmWTg1nXenpisiC0pvpQ1pXG1D1/rbVwC4N6xByByya6du3v3HD8GglQQVbeyN8YfbXX7t0zcb/FeHf01kHGixIop"
    b"qvRdJiY4HH6AjRmAGjxhijdIgVPQ9xI4neUV0o8lKIKO7ugRc5j1zfXRxvoJUXg3hqM/+MZHP37TZ1/02kEwy1WflJtuOqge"
    b"e+AlgluBT536Mx44AHsQoMjSCQAfLZ746Tf+9Hx27eXPU5Af3rj31HUZ7B4FzneUAvIcylroYrJOa+xtlQgUTvggHIC3bXx3"
    b"FBMkHucYd+JWQwBGQwAXzffJQEE2AAlCUBAixAt+4g1PvNAiCuCxhmrM2jmQqvWc4KOl7qrwCgu1F8Bn5bd/+3275rr6P+3d"
    b"e9lr7r77a7CWRmSyuDTSyKNzUWrmrdTMWZntmMrKK83bQ0Bt6i2wcpzrRdBlEEpkm5PuswEI6P5dbvP+L7epcYbOCkA11h80"
    b"avy63GZU8Z4GDGhGWqD27NSb66ePm4z/y2TyBwbrn/rix46erkz+5mgt+li5baX72Ts/u4vk1R2b/4g2dnFhrrtntLaRK5NL"
    b"B9DKGigzNgIa9ADCFBNwjF9E0n6c8vlDfQDE0oDRWN81ACn2HxLpPNZPylgxD1Dr1vshNWdy5WPpvVhBGmoMAEGbKaXMOL49"
    b"KpB/97h/+9d/mu3odC6zNt9tc1NaRZHYTBRPvyCdEmwABWcpBjpj9LAGqPQgAyKhlMOkkGiTN1DnAZT30Dqlw274IizwZXFc"
    b"V8DrPS7AQO9c6OlMMFzbeI+Q/1tTDv3lw5/1ycliT6o/v/VGfeU119t9AJe9ERmoJHJS0IyDOCj7sV+OYK8AN9klWRoC+AaA"
    b"m3/9L//D3UL78Xxz8LIdc9lzJAfM+mCgwJ7vAdiIMo6fClRRpqD1NfFTQJ+bBgy4ACC8tODUI3UmvZQCIDHRTnopPZC++AtD"
    b"pgoTXBl6Fd3lKwlTezIdfxKoNvtpdVbmkicIkthPJrjp+HONtQ9XgosAIMtHg0cone2gMfHe9g0GpqmKqSZsSeFq1cnkuOlu"
    b"SkYcVL3EAySVCkUC+Auuw7p1Du75OXbBxUnlUVXDg8rTrTgstQsE3sR2U4QT8o+zXSTkVpAqy3pmlK+bzdGHNOwb/2z/d/wl"
    b"ADz30KHsqw/b0N/2D6fzZcDi2leNbsWrWuaKpBpFErK4sqj3re3rHN2xf/Qz3710BMCRt/7xz31psKmyha48RWvM2WFulaVS"
    b"5eqPKS5QMQBeNWDAMpwIe5Zt12PS3PTj8gnFt8y4MNCcnJ5DOQo9KhAEAeKuvpenR9z9r847VnpnSDQjEIQmM8y/5OofIeWN"
    b"59F4tM9lavfQ5rsBIFNQDwW5k8ZO6LipFTXlL0qNjZAGILCNcWjrMURBQYfI45EpZAoQuqPef+3/LGWu2QX33JAidPsncb7z"
    b"2gbhQqnvbx2gb6LDXwBCUvZkN6RoJQBpR7nAqndC8f/5o6uffXt5jTdfd10OIP/s9nlUXMWqwbTiGADwmpf96p/+l3f8u8+a"
    b"zdF/7ii8RFmLDIAYQw3K1PW3Ff28yTaghiqLCM8/3tevQv6p9PgLY/vqKhyuvCHDr5riq0fmk/38aiZpbAYJ00G3tMDInM8U"
    b"klYJ1MU7uvjaiXwPACils8tFZAfHVEHAUmZuq7WV/bdQeqvO8HxNf6deT3sDivcMxT61Wwzkvm/HT2Wnf0+e9Pcp39MEMjt9"
    b"aitQOfOOzjg/v1PpXH7fnh4s/9ETnv25QpRRgX2Fs/zo9/uqaCDLV7/sl28Tkx9Enq/uXuiqngaVNbm2Btra8dMR59S004xB"
    b"RbhzGjaIjfXvs1XhTVvfVlzO4Nl0zjM9fxuF4tregG2PcTETSwqJrlbQIjsAIBPIZQqyw1oToJfpOF8lGKvh8TM2760IfXhs"
    b"Qno5TW81lwhOAesz88KGOAzolW6qMSr6If5+dPgBE29AqmDkxBugA/g52QeL4DjH7WfhCUAAa2nnFxY6ozzncLj2XqH8xu89"
    b"67pPPvfQoewhBw5wFbCQ5bMuoLQ81pZX/UN9ffQ3j/LHX/7mD/+P//ET/2UzH13ZU/KU+Uxn+WBgFaDKSY6aLECM749YN6Ag"
    b"C+CFBqE3QHiydHWyXD4y7yNUKlKEriKag3Wr83RMpY/jDJ6utHbcauabtaDFjjt/9gU7MqG5TAQ7SOtfNGf4IGmPD9ynjxmm"
    b"Q4qDE3KCKiQeb5tDJJEq+FemCZ3M3qQ5CGVc9EMALIqBrKMVIALTzboyOrX+6Uzh39/11rfeuriyolevu87gvlNOmxqB5fGV"
    b"raws6rW19Q/01NwvWeJNOxc6j9oYMFfjOgIPBJx0/bWRwp6WBsAt6HEzABXEnozH18l2Xqyg7b4bzxoST8IAJOr9JUn4aZZt"
    b"jXUQamsAWAh8kBYidoe5uPswpYiHwGJhXANQo0jLerXc5vbXzbr9ktDFl4T2fuXcs2jyh++5r53zTNz68m8baP97z3HZr/ae"
    b"zjEG0KYaDmSmeJbuvpm+rw1sh4Ju1hV7ev1zGeX3ldYfWl1dNfvGYi68H82rPX78QvXKV/7eZmc0+r8ZzDuG65t3dgDJrEVm"
    b"jR2HAwbKGGhjoY2dav9Hn2Nq8kRgxHlqt/agDDGs/3q63REqocNSrPQKsD6FmVNxEwkyGIq2UuzkVkqqCO05ViAV7SOAkG7d"
    b"FJYgIpfWIrSw4+pTWllY49qlSiAXAtKDKU/qqwA1SXpPrOqsLbi3+kSDbHdLXCI0Lq3af9OP9TWrcbsOxEE8Y0AZv2emz8w4"
    b"xzoGYrrdIqPCfDaXcX34gYWe/p9veuYzN5576FCGAwfs/e1k3XHHp9jv99U//ue/e6oHvJ2j4d/t7uksg4WYAgugKZ4snmb6"
    b"tMZ/PdlnWl6sJ01A7MSL0Ai8i8JzqG/cAV9nsFHaKx5vI9mJKOHFgEEdQkIw1WMdxjstwW2djrjkeQhuutuFAA2hwJ4QFyha"
    b"7FFkD5a+uk8kgZ2cbDHuMdJa/nU66uH5Y+976ZMWXonbKyBUFlKBeId7nIY/mSXsAZjwTDxPwTUagaGoGJHSIyi9AkubQYvK"
    b"LfMTp+7uqs7fLD/jGV/o9/vqIceOcVnkfjcAy8s350ePHpV+v6+++5/+9pEe8O7hxuYpZQ075DgrYMrVupzw1p/g1gX0rCdB"
    b"Pun75zAHdazrDvyCoqhACWKTGtXVNJhcld4ICMQ8EsQfT0gkaA9eXUQZKA039c6o5p5jIGA1LCDGfAB2tdW7MiEuEFFdWFuZ"
    b"zEQ9mSYWa7BtPNJye9OxYA2TkDVswvC42L7B/ZAKezDxOgYCTngBkkgXjnX/6bAJLWHmunOdzcHGphH5byJ4XxmDk5T7HWMp"
    b"Hvv27WMBDkJGfD9g/kTRvny+o7rD3OQKyKKDk82VfBXqbgt23pZltiM5+/hYZkvMqymlN2N6rwHXaoMfyIQPwC4s98j/vvED"
    b"IwjVWG6SUebftuFu0h6fY83dSDZHbUlBjhm3GOWZkXMwcQxdQlJkP090tIEvMN5GGHCw44ILeydPHr+X0n3x2u7TH8LeY2r5"
    b"6qUhzq2HAODhG67v3H76eHZRFy/sAH9w0c7u/IlTmwMN9ry4tTAGhE/jVSEoxxnEN4OqvnPSANQO4JYGoM30SqyQJKgEsnuh"
    b"g3vXh++DVX+ulJJMTfDnZne9zr1ve0yb51RPLy7MGX3WtermDM8mMDHx1DNs0zbh+jvPDkVzc2C6lM/Z3Hxx+brr8ou+tFNw"
    b"7j0IAHN3HJeln13dmNM42rH2ToxyaGuUto54SADquc9acJCR1w4gWGkGWrYND+XLJv0KbKUzkbuPp3MY9DiItpUH4j0MZgiF"
    b"ZQuTf0vHWQuhdJXIrizW2HDWFNq5/pAa9iJrGIpSw3dI3apK447g75geoRcqCGAxdvC7WZYN1jbu1uB7tTJDAJj78kPsuXqf"
    b"Ny+/kACwYwOnjeS3bpwePaynpMexIBmFkGgIgCqrrxqCMZGyi6/YYboumbJj8+rdNDdkC5NG2sS0W8z3N/nVJEGwC5o92ZmW"
    b"7MosbjxnyFmmPo/pkKLuh/Ly8bFQxOt2HLkepkOTkMRToR5Le6xg0llIiLleD3YwvBOCDx275xunAeDdF95+zhqAU3d8igBg"
    b"RjitMvN3inzqgtaP3LCGwhi4VhKdnJz8xABIMLFdjT475Z+UCW7Y6ISmJweGwDL7Ul9tcCjZ4oSVGSdp6tRn6v4JS7k89pRg"
    b"d6YavgK30SCk9msCDmfVIUh6AakbGoiMeL8BW9kwb6V3j4NMawbCMmLEyo0dvcF51cGI6k6F0Ydxx62bBfB3zhqAAwdvtlgG"
    b"cOGF67x34yNdZe+cV/qRw5JjUtzcctKLM+mrKzkjyjtuV1RfeZepQpvK4hEYCkkPH2k1E1xvRFoPTbacaU01MTO123TlqoGu"
    b"Jfdk4Nlj7J31YJVbux6yvUFqm+Vwtf/CkzHxgzEi+jnt6UiZQwdrOU/OzV30hTHKfiDD8gFzDkdbBIBrX3Xj6OO/9I8+rTO5"
    b"d16INWMgECopvqHTywGSrq/3ZeNYmcyuVLy0drXTteyyxZX5jEOAGQzAGc2xUhbfjtOAoOxuDAHk7M7JMzYUTLpm7T+obfdj"
    b"NrgCKWnyZA2C+NqH054E4/x1jxodK4OlpauHALB//14FSH7OYi0CcGVRy9Kq2ZkPjo0k2+hSj5mAk/r6anwvaCtvXfczNsX0"
    b"7UeubPcAbxyK9w3gNhlrtFBAD+AFZ9UDSCJw5yBI2GZ4SOshlPYABL4EmadEPJWPGxdmbg7QMdh4AOGtxPHbFQDzyOWbB7f/"
    b"0oFhr5AhL2N4ifRxcOvkifZSctIybp51ZsvMq8dWxhy3xwDIFvYeh1pdQJoxgHPN7a/rT9B4HM/MACSRobrjGgAO1mCdo9On"
    b"oXKcnrx75Og5bwGO3LFR4nP8PHlyuDmEtgYYlxDXrt4y43yQGp88GsbZtmNFIp9l76O49uy73NYSIOZBXpyd9e+0zau/3E+G"
    b"aVvTpHWSaSRExs3WZJCjS0xIP1defuU5n5zdf/n85Bq7+ejkxnqObEwzl6IxsdwXk0LO6LfmfTae7xebMb4/PQC9DJRcQCUi"
    b"iuQZT5D7/f7UrfKyjWZji4OsRUaj6BYimO/NY3NzfX6s/Su8/Y45OdcHV+kBEJBj1szvzBROjkwhNJ9a5NPV65SzNiQeGC7v"
    b"2fjwsb4EFWAyRYwAdJRMOmmd0U1uDLN4lu+d1LzN7RsRwq39LLUabxzLgJEkCdmdLWBk1nfL2RNYP5tDk3dZ2TPfyXBqwwIC"
    b"K0pU3MlOtIRhu3FVuTlyphCbnBXjMjug3j7enOU6Cj5ELuAgA+1IRHSkX/RMX0e2cLfOigFoY2C2IfPR2gDUXE+lkpL+EjkH"
    b"jY6R+eJQXnn55rkfAuzfb4Cj4MqivuPDX58jusgBj0KLSKWaF2dH7rO0HuZS6188kCzp9k57/7xjwinyTKAGQmZKVNfSnsWL"
    b"3dr1yhYNwJalk1oaqZg0ujQYBwmvjVVdhfH2MRtIDS204fwt/VsufObyM+/5szv+zJR6fOdobCkiqwYAbv/MNy7pqu7ckB1Y"
    b"R98fCBR9A7E577UwCC0ZxkktjXOqVj41KJtoOrKl8bl1T4RbniexEEqJgJYG5DBTpCnXImH64+RcMQBN5ryFAWgKU2LyYNLC"
    b"A0i+53ZKom8oQqMhBW1eAGA4QpeyB/N89KH+oQ9ft3xdjoPY7sTN9j0OFoTL/nOzL613H2MoF2zmGgYZQIoqep6lDUAo912d"
    b"9P6+bcEZO7M3IG0H8TaQguquwjN8Ilsa04wZPpIE8kyAHISVhnhru9x1mdWCIb5qFivOlm6ICtPGgbCoxDALidFKg9fu7+Qa"
    b"IuvjECkDUL5mUcloh0Nkub1MgKd0FjqfAHBq/+r+cxcIPLoowCo+g/kFlXevgdjLNnINgw5k0qJVTbT/qxPZF7JgMOkJX6c/"
    b"RSRiovRLavaKoQZMTmxu69hPmwueMWFoMqaqY9Uo4TCDlTUIFjSkwnvf9nCA22AYmF6VW4NtEXBQUjE74wtLzACEEmkx1z+q"
    b"aARfhUkoUIDYzSGyXD1UWfu0i9bNnwI4deXxKxUCjf5z5XHrhRcqAMasX7DTqOHTFHjZeg5YdARiGcpwIcQDpGabM/kkYhCQ"
    b"0AoIf1QmBwXTK6+3DtQz29joocw2oSWIsc4UHpuULwuHINYyBZy0sBdokWJUcSweegZezHYagFYeXksDkPRsGoC6qOFIGIro"
    b"JA//ZtoAlIVxeiwPnF+QLexcH6w/1Xa6PQC467a7FM7Rx129LykAUFrN5dK9Zmc3W1gbbI4AdKQQRReh5wH4enUsVvaYcZhK"
    b"cRF1TDrWvBfzKRkJltkQgwvOvcRMEhGpFM1qEYzAoVU8kQE8qSADNY7QtheQw5npmLeZ8NIWvGOqg0tLg8CtGQBFn+IqTggS"
    b"6h4q53Whcmx2dLvZyK4/nDkec1t/5Q7gilCp/Fx4CABecdEufr7/Y3P3ns4fr7LO5Up1YYqWUwrWcfFjBsBGQMKU2CW9jEEc"
    b"G2BiPWc0Vmdt/M6IKWiqUagBKdsL3k26Rzf/5OkgOmx/p0SBtEOB3KsAnFRQA1WCgG0Vdx9gTzVD5yGV2B6TOVeMdAWKdAnS"
    b"CVlxTxXIuE9BZqHywQAdg7muke+/aOMR33r18rgwqN/vnzNYQHktVy+vDo+fVI+VbP5lFp3OqSFg2NEWGSwyEBks3acePzF9"
    b"Mvh78qQKntP3in7EEf2epqeEWj/Oe3WvxbkOmf2J9k9gtv3ZqEs0bjJByABUJ5VQTtCYkYKaaPJHJ1BLY6Bm6BGQ2i/U6Zc2"
    b"8l1blfaqeU8H/QGa5L90jSyYN8mDfccSYFP58MxKoQwsOt8c2I7V3Tlmr1gw+juKCacO4uA543zuL1SBAcB2F54tau5lkE53"
    b"PRebS6YtOzDsIGcGUxiD8bOY7NSBQQi2T54KBlKIfenpPlTpZ3Fc05OJ9xjZljIozfsU/k/FmLmGJLItKVCmEgbM3x/lPoXx"
    b"USIQkQEgJ5QAJ0EZqknr4ghIxUS+PCbbnXBOWumiEUkvxDuG/r5o6k2IFvLhgRte7qcDSXAd8xSCHoCxfgGKfqORrHimpMGL"
    b"ngEKuUFGkT3dnXvWTP78u37qw49ZXl62OAphn/c7HnDouc/NFgtV4Pf+63+9f2D1C7rd+d1WOjKyCkQm5aQ3yGC8CZ1F/y5X"
    b"+JhnQO9vVbxWzt/amxgIVvmx2LsuJofGVKNIe8cEHQRK6Kx2da1bvWP7+W1H0+qAyZW8ZjaxgnxUVDcHhjiZibEnlJJNLTIR"
    b"WRDGgTRpGaNLyzi9VbzeEKNXYnImALsGME9iGYFEPA8nVRcanxAHiMb5ttxHPANVdjsSFH8XaTNriXuH67lS3W/fwOhHvnr9"
    b"O39FbpR19g9luJ8VgnY99rEiy8v2tn5/5zfWNl6pde/bT26anNCZoANDQsSCRfpvLEk95QKI2Ek8L0GKUDl67ZTpNLABBlCt"
    b"CXV1B9wo2h0IuvhhdAT4YyR77sqPVAq8GyG5WV83b2woUJcYLFp2m1YgOdCK92TUuEtRrWvRUBxLNUpDBqOpoGU7Ab3WYB6a"
    b"J3krAK/GiMQQ/jYGYGI4ygkfeCUqMAClYSmmhiIBYyx39nZfsbZ58gdHOx/2t/1+/2YcPUbcf3VYAkBuP37cvus1b+ndvXn3"
    b"dZDspXPd7mVrm2ujws1U46YetkD3Lca9TOwU+KOa/i2lZgAmmX43Q+CDgwKRNJdgCvaJB755qUPSm9YefCdpDkIIBUqCkVYF"
    b"FiUKEkpSd6pOj6peq0pYvdKixES0KBD5OnPclSnInbRcz5QaJ/8svQtKCiMG6Uk51wyA+Ok2L3PTgOhXXkf4AFJnADj9UUsD"
    b"4BkDdz/rSJjb6fC2RULWFkn/EanXzVC07j1mw5r+0r0v/TlZffKHVhZX9N59e+XA8gFzXxUN9ft9dQBQB5YPGlkV+86fufrA"
    b"yHT+fTfLHrU+ssyptS7W8/HENsXKLpOBKcWP4jrEnpBi2TXVufH02IN09mfwY7MmHRhr+RpZyVmzxotEmALnziOVKyhNoxq3"
    b"nl7TmndmNsfXM8GaFh09ULVZBmZITNTtS1bPycikbLXq2+B9WzPpg3VUucaGMa6+49qX7bxtaAD8nopgBGNw8AN3eI4nvUwN"
    b"wfhD1MnhZt7N5rO5ztx3nLYbr/7Aaz9+8hlvecIniukh/T7V8vLZbRfW7/fVwYMHKSI5sIx3/PTrvz1T8uq5XnatMQOujzbz"
    b"jup0LAwICxYTX02+kXiTuHT5p9Fx4aZO+9g4k99PHYZ8AXEMRanya4uoHpPQYfy0iNUCWGdNppdQDCd8yEpMpfri63zoG7BS"
    b"E+GPCDnDzK//eUopWJE1pXiHvPfX/+5KWPv/XHrhpa88duIuWForKMo2OUMNQMtQQOpwgJaEnFnd/qbYXliDC9SReioGQeIG"
    b"IOb20/cOLMZELE5c//I5fZ0TJJSMSGtFKwhWN7T9xWf96hM/fV8XCJGU//fnfuNJGfL/2FH8HmOGNhOjBIYKRpRM8XqBgZLp"
    b"N1JOCKAK1x8TQ+AaBKduQNw+ffUGIMYuTIcK8L2KWm5BlWgkCZdVIh5IG55BrVtf63PUSZpNrsWIiL5s9xy+emLzBrVj/hez"
    b"U5+/4Kt7rrjndCaKQoiyUwRAZgHumiZqjXsfrvIVm8e4DZSgcEfVgYCIAHjwPcfKSi/BJLdTYZGx+y5VjyBiAJTjBbhpz3Kl"
    b"nxgAjtedqAEYhwOSgzAQ0aoDK+rFHI4ecuinP/EGAH8BACsr1Dvf+5ns9Nf+IV9aXbRnSlkjKKuLq2pt31pnx9Edo6XVJQMA"
    b"f/QzN35/R/TPqU7nycYOYJkhFyArYD1QoMRM13bayTeDmKn2P6SI74u1VNywQCaGAk7DEJmAh/Rie+V4AC52UA0jxvhBuPLS"
    b"qRmgNymts62BacjUqi6O9LwNZoCtceClJvb335ckBUkmlCAFAZQigdOXPWbXPdmL3nbV4H1v+ODauGtLMCERKSIAvMKYECOQ"
    b"oClGzJ1HQ5hReR0c6LnoEqzWTGMASLjmXmwvfhgR0ngn4USskCdG8S3TiEFIUa5TxnP93UkfPsf72kJZY2RGw25nx/xct/dc"
    b"mqH56586+i1W5D3fvSSfKmADsE91E6iPHQWP7AMPHoTTUbRS0DwJww8ehAA3qcvv3CW4AfmSLJnynL/70//jCfMiz8uU+YH5"
    b"jnoa7RDDfDDUSncJwMAEv99UClQciK98x3pbHUPgMQYdQ+CNJHoT3joruw1CAjd0mCLi02309gfE21PVTsF4IZFU6gam16GC"
    b"2aMCMDJc7ppF65nsTzC9HlIJBMgNRRGnZWnVZATlFnzw3rXN08TYCEiq4KYOpKt1vxMAW+q8slVXn/HUn6DKF6jE83Dougmv"
    b"YDJ8WTUAKmIAVMQ4oFj1y9je0gf+fANQgICUwgCMjcDYaKju6eG6MaIx1931PGvsk43I4//kNZ/5X7SjT31ZfeG0LMvAXV6W"
    b"lyvru4QBjVMFYgHgVTcCb/+xQ3N298Yu0aMnaBn+iMXwZZme231q83SuMRItnS5hivClkAcHYOmCgCUoOP7OIpy8niL+tjD2"
    b"IYjnxsJF9E6vLzb8GgE60y12rhBwjOSKE0shZ/Ke4m9wFpf6TD7P30YlokDg+NrIAnIcgGQC4fvxgbvWNtaOw/IiJUqRxqLE"
    b"AVq68Wcc57Pe2DQamC1y9QXpSj5x0imSiPuJKnnINQDTH306NC3diNf1BqbvWU69AeNEx8YxCFZEE4L10SYIdRGhftwKX2p0"
    b"9/9eII/5g/5z++9Zvnk5D0E8jFmE6vI7x1/7jssOEThgxx6C/+v1X72yc9DJv0eBP9oV/VSFbLeFnRvkFhSdjYt8TdEZafyt"
    b"SEILp2s6ORExECnoNHTz0qUUyHT/grMW4AN04EEHE6Czyot7V8NVPx33e+dweAexVF81dEilCBmk+Jj2Hci0fyHpVCI82LLG"
    b"SyCptUhuiaG1d4qSuwAwG4cg6pjh8A4l+iKtFIw1jMXibttsSbTMEjnLBiA4v4i/utcaAHEMgMTz++L07pIghRgzAO4qj3DV"
    b"D5D96YpPb0hbFw+nD/y5f5vKNkFOILe5sVCq01lY6Oq5hXvW7/n+TDpXXvGEf/qK//qEH/+0pfrUUPNTx7nwpYMHLz4l5ayq"
    b"rhPql3/iYxcAoyu7nfyxmZhHC4dXEebxhHzbjvndkuenMRyO7BCGWkSPFxUW30lPOHOWgJJSA0BPmWgsY2E6uA0nnkNl1XdT"
    b"fcKAqBN5cuxST+g6kvAAonGy33osRrEQaRehN9UTbsvSLnWpv6qSxpgAZGGhvgjBMQDIACAz+AZU9iWC+7VSkufFREa6rDbF"
    b"9pOGqrw2qUGJpBnp4AFuMw06Ey80TJVrioUAARagIuQfN+YXFx8o+90lKNPWzemHhiB4r+lZuv7hexYCitIW5Onh6TznhqWo"
    b"HfO93c+a03PPuvv0sREFHxHoj+zk8DO/+lPHjv+Hf3lsc6Rwamj0iQxAJsMLtGzuVvd8dk46+iKxdh/JpwB8wu4dl4D5GgaD"
    b"3B5fPzHUzLVWSgOZspF6feXGD8UdU/CpqHaC8MPh9mESJrC2ajDMAkwZ8tNEH50GJNOVXQV4AKPiIyEG4BOIpnsyEmejwjOg"
    b"NxrctGJgcKJsxfaAIGtJQuPRrJSCHQdgXybl7okBWIc5Nk/1JREwEy3DcfG2bruStw0FtuoBhJNZIpiCbIXEUzpPDek9BPUD"
    b"qKk1gBO7IxHnh06tj/QnPACKDwRWMgVKKJKVQ+305imMcBqEdAh5Ki2fKhRaGogoihWIWJhJjD724K2MHW8UOPup9RMQjKBF"
    b"K8D2KOVqDyjoos9kXsRDYw/AytQLcN1HD4CVkvhrp+AgbdEibVrG4gF+nJpQn/Djgn5uJsDPAliphgOMSpNVGXvTDFLgbEvM"
    b"KRcPUPSBv5T3AaTyYxK9EhSZETeDEbrGjhkSUkkR1Fp+cTAwUw/gws7CnQO78QVFsRpKyTgESvPuE17BfWEAwPrPV6zCN1UD"
    b"IP5+TQYAQUzPIMUn0/w9nAkNJ6/vu/sRA8B47j/cxoqxcJ5TI0FDSzO+Oq1URzqqCy2ZaNHIoZBRI5MMhIHGEBoDaI6gZACx"
    b"mwA3YK2BoRgNCEUUqQApS3AJSLle68naaUvVH6FnACfhVzkVJl62eAScMmUoKNNSYbSiHGMQAwHhZQe8WvogJQihg57brTnt"
    b"0bkcS++F74ffmy15fckpH70umWIvdqwDIBSoz1+1dvwuoq8yrlDLkqx96PW3fB4UpZWGWFBFYh1JQAwhoTKV9mty9duEAZK4"
    b"ptDgqCBvX97zMp9fHlgW3UgT2YdxZp9MJjmcoUQvMt2OEMBG3f94unCcsBAZ1xOQuc3tyOQ0UDanIIcwp8JQFAmLDkaiMYJG"
    b"Llrl0BwpkVxpgRCiUcT54yyyKtb+sdtqJ5IVY9DOeqw+3xEWuLLgcFx7eMdMs9ZoHQL4n0HHnU9r+PkEUIU4bVgqNQTWIx7F"
    b"QMDSGKmEO68Kw6MaEuTSsA0tjMb4dxpzX8azgsSX5cZbR4evv6aTHTlyRAMwmdJfpuFJWntBmApsSu0l04aM79uUzkuh+ykE"
    b"P3WNUw9A0hgAErJdAbjnFVI6eJNFqGGDSd21DXhpLvnVFoaDqAJ7Afmnuo0eR67y+fCnhhDQ425jEriGJaoaDCYRiKgSzisi"
    b"7LIGTxV5cWLiEYxBZlCmA57UXo8+vy26EzMz8QNOXIQYxz/wCuiYfYnFzHEj4KlgS9PqnxIonx2wuy8eEuQkSOiBISByl8Ho"
    b"LgCYu+y4ZEcxbjgpI54QLbdtDNafoag6pKWIyCxNLlJYQVN4IC1c+1YT3kFuqzn8IHMR4/UjVtTj5/pd1pg7wa0DTlpvDQpS"
    b"gN7EhieI5U5yw9TKLzCBoTCUCbfAOmlG63ofRFRoy48U48ITJZufUOMKOilCACiookbeAgDHVfrjrhNTcU4R8WDAcQOBiAdA"
    b"OuAz/Wp5Tiv6GdZ+O4nUsfGFJzAqNQVFjKYBGaQB/dXeBwvD65Aon4CkF817TLWKD43E+eq2uSadfrKTpFbQayO7aak+DDN3"
    b"LwDsx11W7T2wt/jtzBrI26zJT3ezTpntYEhiqUwaNoh5ML2ShsIdbkgX7qNi5/e2iVNIEl/VGwVCWMPj53TiGAeBt5PJX53w"
    b"IYJvIN7EZpDnH+f4CcNqgquc2CR84DC6+lffq2WS09mPUy8GdOWvfHGMiYGgHu9biGqMBT3EF+ig8uW9oIvjdPx9pJ7l9Uj1"
    b"PUzfm6QbJ4o6qrXM1hSHKDwmpoKUOkEQBTR9Tq1MmKt/3FYqDME27xzsai2GOKkoH8tG3XHH6aMPYXbg2AECwHpmT+6w6pMC"
    b"fbyrsz3D0QBgC8ZdCnlPAHvJktyGv2vZhRGPQZCg/ibSglHKb/C5ZWrLwl3tfcEFW8lMS/K1L5MZTvgYIBjHDuiRZ6sTP+0E"
    b"p3xVqQRSVZkpOqGB8lD18UQ0E4qreGmuacxsgemqHmADrBTwcJLom6b83Dh7nE9QxWdNV2mpJCaZxNurfgVm+DsdKEgt/lCP"
    b"MQaeQwt8IChdIgB0M43N3N5L4BOnh9kpAFgFkB08cnCcyPmr/JT8o87HBLy3qzsQCqUozUiRc9qEAjIDm6+W4ccgbm1w84FI"
    b"tV4MV2Aa+EMyrp7G7+Hkrsb9znt0kXyfDRiy/0ID4OIK1hOnDI+JXYtvqID498O0aj+qLVdOdMsxjZciwXScmgSfE0Cv1kIh"
    b"wACCCrqyI5ASeBTfKTjI6T4FKYOTbVJZE0tyEArXPhoCBPRhqYQHSIQHYYGQzx6INxSRqslhxBRJLARI8QLi6cLiypnpjBb5"
    b"caH52NW/uXoagCyurNpseXmZRSYgv+17b/lovq5O9HRvgt8IZ0v/tTYAsVU7aQCkVqWHqFHsaWkAyrBMMTbpHTovU8Be6j1M"
    b"4lIb4QFMVnFKgB20w73D58Rtj8JfkmzLGfMEZlKeZWkQShdaYcwZELjFLqXk1PSlhWWhSCnW0d+zRWgBhw4rE0zeI+hQOenE"
    b"cLIEqTbSYQnGybXSvCyfAbhHNJfH+eOUZwYmFr+CCK18ozvc+VkAOHz9NZnIrSMFgEUmAFf/7DPvEZEvbWxujBThdQqQVo5i"
    b"9XVqW+xvNBw/QeHhqxSHr933YmrGCo6gp7tfRJjDzpiqQ016Lv73mT1rSLFnwDit+zWUjwHM8AxVd1GzD1uo8KJ2H0keg5av"
    b"/b9jI6s66qZQpVS2h6Kj1eNnfcZm3/T3s0WIJKLk5KZZJ9TtV7xpdQOAzF12pUyIQJt3TttOK8OPnVo/eUAr9QiBwMBaAVSb"
    b"NF1TynCmgh1IMgSIkoQYX/XRRPAJJr6NuPWocfmTfxOB5g3iqzf9Vd3GcIQYKMi4Z2DhauJIVMoijBNR8TISz3CVD0GpAvwr"
    b"CTZjAG66BvngmarSZKmcZopByWwRKtCpzLdOZn6cTiyCDrG1S+YEF6CdrrDSTMlpTwRqIvKcQW+XxOdFz0RapUWRWm0a8xkl"
    b"6sPllmO4y04MwJ996s+cTKo6QphPaVEPFxGhtWws192GCV/6YylyT9K1D26KSpQAu8dVV3sJQL3CghKVJlV16b3JOSgJ0o+P"
    b"AdBh8009h2mhUJIHUKERo6KN4xuPUDCriUTiPCfcgdiEL918wmfaj2lDtpjEEnwiObbGFlUx61L7wRf4mG4TJ473y4SVD3tJ"
    b"jKvvi4p4tfsOS5DCSvDnnyMShjgpzmp1miAt+tG0jQH7z1ct8PsFhtiBpVICEjbP8SlidFu55wEcsMDNxV27uUjjAqLs8KMK"
    b"6kgv60omGkLasEhL2JLBl5DcTtJu24YbDGTAg5ZbiGQAXLcfjKTYEow9JlH3FOCXitMlcr7paxM5VzX1U53kaOH224RubT0S"
    b"EFs5VTIF5mrpV93fqctfdZN9d9hNozGSdqu69OXxOuqKxxpnuL0ArFOpGFMojKX6MIE7w/1cHqoK7qGqyXWpGYLjCnRe+du5"
    b"LqtFo5dlCqI+0evpz0wOXz44gUZxEAcn6+Hjl599ByGftnYMzZRKTJKYvDIDD8B/PVWIEVcuO5HrR1Peng0djVilKSfTdEQA"
    b"9kmE7FmdfP5xqHH7XdffJay67nZNfB9cG7z8eBoLYMv0X/W6Qze/mveuhArue0yEE+F+bgcbpnCEMHee6IwTybGn0ppI5ehr"
    b"8u8pIrNnwM+gJdisbb88n6Usabci1gpGuR0J1Gcftfyn93LCtxovoVmBrZL9vsLBCW/gS8N8eKcWdbkCMlgmWYGNeXoG9MQ6"
    b"zb6WlXyzdOIV+LRdBC6/O+kRxv3B6lkbAjBOBAoxAMsg9Rd4D3XAo/GyCHVAYIgBNGPbgmrbzHBiVNX61JgCPInzWcTm2k/r"
    b"Fd6X9dKARcqR1mP/KSeVVaoD0dnmleaW3YSFnnRYhR7FatHQ9I67ZTWBWScdHD0MHZjg3RNxNYAg1Seplg71IUD1b0Z/QUta"
    b"rVW2mVsLwW1D8KvlJxw82BdgeeoBuBzwMdFTvkprDltjNrq6AwWxjQ04US2VHTfCmDKqpFrAFf27zhOY5ekvgZJOrTGVWpMa"
    b"TwERIK/NcdLCO0AahGM8xUfE2WTVnL8EBGB/WE8NZtwVbt20kolVlpEWWU4PvEpbLsbP5SHrrOuL57P7Ul5B7feqWcWR5Ey0"
    b"uD8NngFmfO1hPaLY0Ro5sGasfFCovzxB2hxtuKkBOHiQpTfQ0dlXMtHvEXB9R3eeRX+l2ri8esLp36rmb4kcF0tqqJo0oxtx"
    b"qeBcDFJ0qCHDMJKig5faa0rlpYg8KcFPiaQRxWMbtknx2YZofva0YLQjYyDqIYlYNJ4ys8HfNki1hdtSqblYKrE6uqrns4l0"
    b"YOpamLg+VNKQiIxUlRjRdYnyurQgZnp/XLMhdqHbhYKsK1GH9vbwtbKB60G/fKpc/Mdr9criir7qF55ybDQwHxTiZFd1RAiq"
    b"Jv48JfJs1004Gq8nugvPsuqHK6I38YjJs6ooL5UinarXEK7IdWSdWIweAQmDuv/0M7aaxEhBEaPBoDIvMskrqcOAE19duVQQ"
    b"k8/W+trWYQU122ylXfcM8fNW43KKU/sgDR16W77XiBWo2fEAEauUAinHc9oPXbq8enoRRzOH/4QJBuA+9u3bpwEYZdc+B+78"
    b"/Ppg/VG0zMYns5Sijst1IuvTe/UUX1f3P8XUCzMKqVJeRnj5dXG+xPL9yfck4u7H04BxTgCjYQaCWgDr6QX6AiBs8ApSnoIE"
    b"YULKA1IRXoA4RkySoJdM0oCYVPE7RkOCYwsZHbdwRQXboj4mi9/MURiit/I6Wn6SzrNP03is3CQ6EuTpLL1EtArdahGX98CI"
    b"nyaVX8jrPORhBXV8gcj7RQwPy869G6MNQD527BTvAYDuPacqP75KFCDI/J3ZmgAfPr2xdjxT2pMIVqxPRkSpwGwQ76hxmFSQ"
    b"+guTLFtlwtUx6+DxAprYgBINOZrc/XgXO0mGLikGYYotcmatguqNRTx9lWbMMeK2M8L6awoZmHTxpRIe2ERYMP09VZTZZ4NR"
    b"aJOudzieVM1+KhlaVe97aNjapQGnlQ+CbtaR9aH5Kqje+8i9CzkAnBxcYRsNwP79+41A+I3eLtMR9Z6O6I/t6C5Ika7jpHNb"
    b"nZuPFuW1CLT7G9z6SkNOVqm7bAT60kAcGs+RXv3jbn0MYKyGABPmXnK/Kp8/1Qs+VgLMQGenLiXISCagrvwVsXvMtPuOMGXJ"
    b"2HcRvxQ55nEkUnsVAhPbhSC+G46WYBuqYUE0DMKZpfYmP2r1e0eBURnTpXpZJlpnR5hn738kHjkEgNuPH68YgEoIgCVY9qlk"
    b"WUZ8y6cPfe7Y8Rd3Vfe5AwxgOf3JZnHzU0zAZH8+xll+rha/DT4tioon2H4xlx9B6g5JpL8+BLDRoqEawxGo+0RDAMqk6031"
    b"eKC+a161tUYT+OdX39Wh7ywyR24IgJCMEiELTRV8VSEN6tOD6RCE6SkZlHdAVdh+LqNO+ak8aRMocdJbSCgNcuI125ysU/Uc"
    b"6RDAH7lVSVGnksqZJJU0IjnO2CMnQfJj+3dd+XFZXrZcWdSytGoaPQCBcPXo6jjyeO1VAxE5ujHavMtajsVeSVYmdSoNiLQH"
    b"EK76oRgIEmIjVcAq4QHUrqgzpO0CoA8BxhCy95BMK8ZfI0UEiqTv2BDyNIVB9XKXqfy/FyC3qBtQDcSfZnCv1bZZgLwKcFcH"
    b"sKnWabpZU31okz6tELJqtpWEn9LzIqiUEksxG6P8s0ZwRJb9xjCtMIDJwOtTaYt/2BysfZDWYiHriaKYCqW3KUphWr5batJ+"
    b"sXPaCLvNbrECrkZlLinoGcbwtubzmyr2LBAAlXUeDRo4/FurJanHAGY9vk0NJ6L0X5/C23QucWg8QLoWNFaph0mXgDhdN6Ql"
    b"C+KKSm2+J2qQrbYYQFvZHAEhZr7TERKSW7xLSeejLFJ/WFyxrQ3A4sriWJ59WeyO4fDjGdTfiGDQ091xbUDQ5Val0npsbo+N"
    b"oEQ3liIMLWHIyXdX/Ul6L5pzjxXUSLU+nzHSTV1aEP7nV7yDhCcw2Vc556fTRWiK/qdX3ojx8VKhzaIg9YVm8RVKEpOy+ZlK"
    b"g6moHqEv65V4sikdNy2uItWYeBQVO2nyRJo/g4maBd8bUD7ZKXoNqlITEd/XO85mKgMgG4Ls/zzhl3/vU9h/VMo0f+w3z6J2"
    b"vNiZfWayLKe++O8+9EFF3mWt+RYSSmGWKr9EWs8lNDItHJJyfyfWklOV9dR+jAl7JF6TdVhBU/lvSgkYyQo9v7+tVFpa+tfA"
    b"QJEopQPHhPBHO+R/Gl67isFx1lyY6vOFv12DAU84xAcZxwIgk+aBMZLRZJtHLPf4DeK04J6ubwltfpf2GJUEkbR1lG1yw2pL"
    b"kJt7BQiCfqNEtj7MDYGjmZVPC8DD775QFbVms4cAR3BEAcAJ2i8L8b614fpaV3fUVjllsSo/lYhRYy44a1JSPEeeSBThNBXp"
    b"uK4pE2GObT2pz6YGtSTYh+GwqqsarJVtSXoWcWES15DGV9FQpMNW0pVbvzZEMyNoua9CvdgHWmyb/Bac73b1xii/WyB/bvT8"
    b"3STkmssu45YxgKNHjxoAcvoSntRUK2L5mZ3d+aIChGytsttyW3XVj6W5qqk91HLs00w9hCm+Vpz+ulRf3XFsXSYcr+yLA58O"
    b"d6PyPWed1O7qF7/GgG+eTI2pAq1u41LPkCprSO3ZCnCI+vQfa0DAOgYe2yr1Nn0fRGsTUtee2lamCeeyDCLqU0rjnU/45d+8"
    b"VwRsstW1BmBpdckc6lM/42efsfm1nno3KZ8sSw3F0WYQ+rJaipGW2altFTWe+jjfBnF+6wkVee2LcSKhzOP/HS8blohMd1gX"
    b"oKYU5IryT7yXTaywJ158lPKQ5AxlwiKrDpkMCdrH/SrS2lMFk7Iu5k+v8hNacPL48POQwBli1xfBJlj3OVJ/nWHmhP73Rw3W"
    b"AOd+QcZ9HE8ORyDUx491HnmbiPBQv5/J8vLWDQAA7LrzVhEIr12+dl2Bf7c+WP9yBmU1NMQRZwlFP6LDKLLah4M15TbXudMW"
    b"sVLWesS/TViBlq582p1vOq7aTLo+dt/KlrOTJ2BSSDQlN1bPHWUSKUcD6QcNTLumoLStoqU03i3WZEQYELfgGMKIjj9i5W+e"
    b"hzXdZjKlqZSSwcj+gyi5+boi9Xegxe/YaACuuezPDACsLK5oK6O/tWZ4qKszPac7QKEWJKEKUqoxSHJSSk38LF6ZbJX+2JaZ"
    b"V0XgJwyr2jAiEQIwFg7Ej/NJIlJvmBpDkbjmf9oASUtMoGm/arFQCAb6xCynuUaEURhj+6Xy+1FPoymMqM3jI1iZ3XsfTsQW"
    b"Jbm1PANJkM6qBgEuj8TxLtPsT4EF2NGZKFGSif7zgZUPTFJ/Bw+aMzYApQuxuG+RH/rcnf+QiTqU2xxaFKQAXWOpvjpXP2x+"
    b"UZXcCuMkd99UbJxK90VadbGaAjQR7b5YCICE6s+0JkAiPQDTlYCxzsH1mEbcyME1bFFjUKLmbJjk9V1tEEkDxokq4vD26xSE"
    b"VDotRlWRIbPR9GFKBVgiqcS6VJpqICD56sUMlY29sKBOYkxFVIVioKbT3ShCJrJQJEUZCwwN78nB9z3tV3/7q6tHj45VfxKp"
    b"v5kMQAEwSYkJCOSjxtj3D/PhRkcyBcLGWoVV6L1JcC4B7lVkueL7Au3ENsY7uitVXPcvZGF5fzMtnZ12/esBQdQCf83eQZWz"
    b"77a7SIc2ZxYM1MlSJXj/KSMRlRirr5+oB3jPgFm41X0TAB+IGq8gDVjHuiO6v395XgHsfLfHobUnQfmzgbGfFICL+/ZRWmLA"
    b"WevRcBA8hEPZcMAvaavfPmT+6EsWLrj0nvV786KVbNKi1OnS1Rel1G+bZVDbGjZ3XfxuI+eowwDsTBjA1ifn/f1o29rKX2vc"
    b"b6q8X8VOfBSntNdj+oWA9izN5u+rFGnd9w3vnkqMAPe7x97ntFDZgnPdLFtfG96llPr93onsGFdWtCwtmbZftJUHIBCKCHfd"
    b"uUse/vqnfUMbeZcSfK5QcqdEOu0Kq/3v0rJaEhHVbMvjbym9xXTokO7Eg0g1Yd1+bUg/Eikiqr+OqrGU1oDkzLxohIVDsVZh"
    b"8a7C9WKaaCFqmaj8Y7vaA7RhJ87C72e9JFmb71P/eW3FUyoKxGOJHlGyNhhRRH1sOJz/wLU33rheNvlp+/Nns4yRa4r/9+75"
    b"s6/fdfrFf7O2uf6wTHUuJw2sHYuFsAWyH3LNyQTwl+BIM3L+lLAHgslfPU4aYu8GgRCExUDNqbqkEnHj/owqFE+ZAHJGq5/f"
    b"oLME9zjV6Jiw8qbsPzgr97SOza0lLIGtYqtM+weUrTyn4ps2yCDEmIChCKedXJMrFjJlC9rg+40bh5Ku2IYNqM9hUGj97OcW"
    b"XaZJlZ+4vQclIe1JR0QlNP+wWkRnWjDIzYdJ9Y5nvulNGwBwrImjtRUPYPI4/itlRTAy8I83883Du7sLHQ0NkhYz5JzbhAKz"
    b"nq+Nq40Wqca6dGCb0ORspuHafc6ZuLnS4ooE9d0b6r6VNBbUsJaPkGIHqkTmI66dUNdeK3Z9KRnudKjbpq1XxIuq42AUU9YU"
    b"wlxaa23I94J4D/t91e/31YHlZTPLrz2TB4DVVTvNDCzfduxf/d2hteH6dwNYEKVhyYJGLjV5dOcm088Bs8FTQM0qH+duV/PM"
    b"sVU4DeS5bMPmdF9SEiy5kldX9roQJm4IJAk+ImEAU+GCt/J71ejVySnOeuULW7lZAniv3ZVt6rU455vUCQj8unhU1sdY10rG"
    b"un1WTJt49QOpfVPtvRkcE6skiG3zzlepa6gztvSvm7CZ7ihLi7XN4RcyJTc99Vd+82vjVP2ilhk9gGzGtYEusD/H7s2bZrhC"
    b"4Q/O6/ne+mjTUJClBqPr7dWVvgJxUQ5EDUWwH2MDXwKnMX0OTxCEDVp/qGvckSoMqsMmmhmMtYQjoibkmt1FIcJ+vGEy0Z3s"
    b"jkPrFfwEBoBA2WGCFWKQn4qkV3SNaQZHEBgY6wNrtBX3fjp6lJ8nYbGPhN8vFnDB+Q5huBC2CatPuPoNCdmg1eAZwHyh2+mc"
    b"GgzWiOz/P5SFvy23LhYL9NkLATCtFLytf1t35917PmtM/gfG2hOZzlQOQ8ZSfox1zZEEIaiB/59I2bmxfswtjQNuiKcSvRRO"
    b"GpeoeA9skcoDGlR7pNa4VFe9OFgYa03Rzv1ng5vvx/iofJeYBFmqMw+ioFkKZINjYJAEcRuUkpE4lhJpo54W6ZhIjrGpb0IM"
    b"6w/CCcYZkYwQhSwgg9wAlM90TPYX3/H61x8/fP31HQcpObsGwBsOv/eozR7nD4uo/3N6uLHey3qdcUqHrS4kJWrZXsMfNRr+"
    b"cfEO1lwHG66trm34Vh+zHssZ8JP79tGWcYgtliqnYvJyGKc6S1QFQf1qPP+1RZzS6xOb4mMpjhnFhUerVGhVIU6509QWDsXO"
    b"Xq+zPsq/ZiH/KxP5cr9/ZnM42+qB+7E/B4ALLnjyya9v3PpfTG4esae3+7rj5iRIYennkXXue3xQ1GUEUhLeYGxVrDsm8ZqI"
    b"Vty1agne+m9GV530jInoAdRkmAFfH4FnMKWnUbs4mYaihddEOw9OFoCRFR9+PE8phocEoUUMJHSPVdPexpPUhIrG6pPJz7A4"
    b"RSYdfqffxI1LVa1LLoH3KkAyQzCT99WsN0ACkukM0OpmDE//zpPf9Dtr4293Y34mbIWtDY5lsYf6hzJZFnvp/DWHLXhobbR5"
    b"DyFGxtKBTAtjzsAKbNp3wimvd/NbpeHYRMGtSQOyhUKw624ypWXAmvg/JgMVCwNYYySaU4UpcBMRtz/Fk5/GuDGWIFDLm99S"
    b"gwxsKc+OiuqOmlnDry58iRcD+ZRxRGsLvFDBZHrcxXhzaD4sot/5zDf9zj0AsLq4qOQMnMAzch+OHT021oVdltwi//PTo40/"
    b"6+iOnuv0lLE0YYw+i7u7lZXLnqXPOFM3fytpuq30Obgvw4JZi43imQrVMt07db1ZO4RViyEd0wKMVRbG9nO3C5BoHRZvD1YX"
    b"0qgE9j82SMbCZjpTJOzpwXB1zuibD/X7GQA5sm/fGf3UZ2QAxrUBIA8xe/ivP+sjnUx+Z5APvzRWJ9USSmwDs1bvpZDzWIvt"
    b"WFahrfhGvacQSxPWtetupxxUt5rGB2Zbo1DnTXJL5iqiEBw5u/8b1DX09D2BZOksJZKeDQVCYhM43oOgtTgMahq0BqFKxai5"
    b"TYnRtk6hphU7wUzrzuYot4R6Xzfr/uk1b3jDHeVnLjfU+59VAzB5/OYqAeDyhZ0foZJf28yHX93Z26ksaKyT8q+nBLek+HIr"
    b"Ut/x1zY5+ZtoyzNQgRtDhVR1X2yWJ2rliUmbbrZSnm3jnTjHMSxllmgmIi3fVSXhCKXShTheNagSYqROlx9KstLPW42pWoQD"
    b"qqLYg4iQByKCnbFeh+lqQFUbHmBC+UW+Z34ehHwyU7r/9Pn5TwHAgfHEP2NHb3sMwOqS5fWHO7J89ekhh3+U09wyMqNR8TMX"
    b"FbSSoNa2S/shmWKbXREIlbJeaelFpBSBZnk28wqaQoY2YUGqbVoq5q8Tz0iDtX7Nvwvb+bF8jNYtUe08RkRHw7RfHQZS53mU"
    b"v7uwLtUXEehgmgHIhAGk83lht6H6SV/sS1glwkwpvZGbrwLyjqe//vU3y/Ky5aF+JtsUlW6LARCAt46hJ3n0G591VwedPzw9"
    b"XD/czXqZVplYpi+2beXfdsev9/Vnzhqrt1Uwuu9SfNvx3WJ03Pb3PS6T1kxBjodecU+mGoqkqMGo6RkQTyOGRqyOI0ARKqWV"
    b"6Eyd3Bj8jdV6FRD0+32FY/u3bRio7TrRtTdeOyr/vvwrT34nyN/fzAcbnBAYyDpdvlrXm1usAIwMlomuYGJFq15PDQ6BFoKh"
    b"8AlRLrAYH/SsCYk4s/TZmRnGFCmoHZ891Xo8JMGkeg6wURNfAjGOdpV1tkD6EckU2GQ4IN4xVUGQVOZg2hegmgVIhAeEFYga"
    b"GYtRnn9WQb3jO/7zf/54IfKJotz33DIA02QlRVbFzGdzf2IFb8ltPtw9t0uTHDGaZqqTvUJCHutMU31NPftqMICgVVgdBpBe"
    b"uXwXOHzfokrrjcugJYRPzthTkOTqG6LUSKoBVQVBGt39OnVdphuSVJqCtm1Ywvr9UlJkTSXMqdi+rlUYHKUgC4x2zy+IBU/m"
    b"lq+fG87dXDBwZXl5eVudwG01AAKBiPDw9Yc7l73xyXdlne5v5dZ+wFgOtYz7CdgtjM0HomDGg+tx3959e0aGq40wSFugdBYA"
    b"VWrd/WbAVZXFdOx1er1hbu8FsTqC+cOnv2355G39fvdsTAV1NkbKqctOkaSc6qmvKMhvnRieuq3T6WYQBRJsl+ZL6+htCQTk"
    b"FpuDJlf5GUC/AqVPGbN0B1/ZorT3rMh/GhysIxKlqxDjTUGSoBvbtfyuaAK2lPOqFwhBsCKjWe+f7QRDkyQpxL1IEbEiSjpK"
    b"8/Ro+B7D3q//o1/7tXUCsh/Iz4axVWfjpAeWDxgsQV29fPVwY3jqT63gnZv5YJN2rBlgJ1mBiFgnt1EFqFbNp4pF1Ov5ocaI"
    b"SESpt026MMbiE6/VeCMYxrO9Po/jWIko28Zc71jT6zRtWSUnfdSo88y1+pIYQ1R0sypuOsu1NCkHebgCVZ6pDJbgZj76iBG7"
    b"8vw3Lh8VEa4uLio5w3z/fWoABELsG2vJXP2b152WTFZy5v9dKaXmu/PKWJvPjv6eG05uGx7+LN/lgRjacItXPus93s6MD6Oo"
    b"fpj2jXdUYpL3gFqvLvW6qs8wBsm10prgqfXh8O1zGofIsbrembL97nMDAIxrBQTC2/q3dR/7q9d8Yk7m35wb+wFjaTq6m1mQ"
    b"1hW0qkH6Uz/m7KBfc4Yg1ZEHlArRppXwJ+sGhXiTyWOSRVZQ1jaoSAOX7UICiazySKTI0roDkiIERdp6+YJm7cKAFNAX3Ydt"
    b"gEcgpfwjsTLgSs1DfchTF85MzmNhe1m3szEaDUj8yR6za/U5v/zLx44s9TsCcPksrf4AoM/2arH3wF7efPPN3POBvd940jMe"
    b"/7mRtd92wcKeyzaGm4ZCkaK7QKzij5Ga8niVnyTFPqupvvav0+f3V4b2ikDTFSekSYfnpadmJ1FFo/DeADF1pVjTSv8cXnmK"
    b"NNBVhJ658OQ8BJXgoIIciF8jOL3K4nzSZKoEDZnQwKhNxXckCQ763yWsHpTgW1RpT84XC/Zg5ByuYmGxQOQLvZ4emvzdG938"
    b"dd/5a8t3EpRLj1531p3fs24Abr75ZrB/KLvu5leatz7iO7/89d0XLijgW0WpSwRCY60VEVXXvjulHFQ7kRkzBjHhjJpW4U7u"
    b"PjbJgZQ4iVufJUn+f8W9ZCgAkRYdjRme0FDEwbyUAaAzQasGYPKpEcG30DjEvqHr1lQlQ+uzkMnmXY22oPgUgfdtpdYAlNcp"
    b"0cnr312p/Ds5X8SIMPisnBzNZR21a25eb45G7wXsG174+l/6CACgj+zmm282Z3t+Kpz9B3H0GNmnktUlq63875OD03+UG7OW"
    b"qY7SkrGtgMh2x/n2LJzzTOLbrZr7BwqOwMZ0WJvsxlZxgFh8H+NWVKv4GAUvVS2uU4cBjBvSwnZ1Vyul5fRg87Obw/w3r/uV"
    b"f/9/DvX7Wb/fVwdbtPXajkd2X3yIrC4ZgtLv9+Xxy9/2hSOv+8j/zG3+rTnNSzq6t2CMNQR0XAcwjgEg4Q1MV+xmN7/unC7q"
    b"D9Tr8TUXBIXue0pspCp4HQ6jGKswlUoM9QBnSSMyWPv89TdxJkezj4GYB5wCp6leoN99wJ8qKYqttDANQXjEyiVUPI+K8Aen"
    b"q3hYXdn0uvRAnaggIh4qdq7bzU5trH8FYn+pt/uiPxcREjDXAdhuws/96QFMMgMHDx4kQdm358mfyG3+Hy34PkMLUJEQWxAh"
    b"omCbRQswb8bCHIm6/YhowLWpBnR+XCI5+dPnrQ5GqWkSUj8lGJOy3EZUPQWsNWVN0tLYdSWyqYCgbVowBh62KldONP1szQQM"
    b"qgEBgbW0SpTpdjrZxmD0FVh5+8bIvPO65Z88zZUVLfcx7+0+MwDjsEqI62/NZFnsk9/67bdppd88Yn6ok3UzrbQCxBrGw4H7"
    b"4o6kBrBt6ao3reizpg65zd9le++TbPM5Zu81UH/+plbrzb9Lk+5f6jUTJtoQVisNrTJtyFOb+eh/9yC//9K3LN+7sriocT88"
    b"svv6A+XGa0dcpF7FKva/8Ul/+bGf+UhvY7T+EFF6f6YzPTK5pQOhpFxoREG/hvAgAR5WBk5SulySP35cOiu2X6KXAaWm50H9"
    b"ubYDU5i18U1KUj/p+keFsiXoixMAqMJK+FX9pNjF+I53TBq9BAYRhfXcMKgQ42vRCyAWxEy7wo91ELNMq8FouE7wT6G7/+26"
    b"X/vXn+33+2oRoCwt2Qe9ARhjAjIBOL4yuvMvH6EfsW5o3yyiHk/AEkV6sE38zq3H+dWJ2z7uj5V1xn0XqeULoDYESK9CaTKR"
    b"zDTZmZjZqdgWQehUneyplhq+sSt/3WnlnY18EiP4gLTyQGJTuvq9JdK9GkFrE7+JjUi4XwID8H5DUlmhzkSGo3wTgj83lv/p"
    b"JTvXPt3vQy0vL9vl+wmYVbgfHyuLK90Xve1Fg6vffPXfKJFfGObDv1/oLWTF8DAsGrglCT88M0WgWL4/vXpKDY8/ppdfvx8i"
    b"acAmo5CqDpSGGHx7nP50eytGVr24YEcdMSnCHGDVnWcN8UdYQyOO7B/bJkEIkepbIA31DlMcACYTZbMsU9ZiAOCPKeoN3/vr"
    b"v/BJWV62l995vb4/5+D9agCWVpeGh68/3AGAJ77piX9iIG9ZH278gxKFTHc1IbT3ESAyS4xe128Q2D4BjwdHBWRdHC8zHqtm"
    b"uHez4Qeztqlv87AkRWnJdEfnxqxba/9fiPzm97zh3xxeWVzUh2+4ofOqG28c3d+/zjnxICkHD0K+/96PfZdR6tdF1ONylp2G"
    b"phST+hi92c1PtQaLhRLNqb7weAlaolcxjMo21msJ2thn17Qec7+bddiEY0Q6/hlS7DlZv2RajhRi8gq2+DXolC2xWqsnNpIr"
    b"sdP9ZNp5192mwhyAVM9RW2oldXmasFm5DUcW6kniU7Zk037FdpNprY0ZrQP4M+jR8vf86s9/8mC/X9b13+82Xp0ry8Tq0qpa"
    b"Xhar79Dvye3oZ4y1fzeXzUsxMgyLBtNk3C2fpVwXidg9LuaRbvFULSFNrSQyE6jXFOe3KSjitlh4Vevup1b3eAqPqHL1WVnt"
    b"K6nAaEu5Go490zqBdTqCiFCxkzUETIcJRSt0I1B5pjvaGLueE6tqmP2HF//qz39CAO4/uv+cWXizc8UBWFpdMp9+zad7V73t"
    b"qgGAvzz8uiO7NkYbr1E6e7YSjWE+cjpAypaMp62ZEG3TcZxh24PPna/PAGzNAeU5cB3b910sabTOdFdnGI4G91jLd2jgt170"
    b"ttce7ff76qJ77uksvW1pcC5d9bk1uPpUwLia8EOv/fh3Ucl/BPBtWjJtbE46jZviBUQzZgEoHtKeLuqJn98WL5pFRfxrsGgS"
    b"KpFAwtw5llW3PwxvGAk1mkKAiZsvjIQAU/e9/AUUbHVbuRY2hRGT2gLrH+duc0MMKbdNv2lM0d//bCSlYiruu/h3ud61dwIt"
    b"CcIDkr1OVwb54F5l7f8Sgze95K2v+wz7fbV09Kisrq6ac2m+yTm4wEyWhcPXs2PmP35tDvnlru4819DCWpsTUCJKWSezzC1U"
    b"AAJu2q8qDBITDI0aB9ZjDMkW4xRPnNRWDERMdDTWwqy+Fbn1sIatGIBgggYYgPhqhok4PzbJpzE7gnNFJ6zUGwrPOMhUjtZX"
    b"X7SoloZZJ7aP151KNJcxxSksmWsBd/TmO+uDjWNE/ltqxBu+962v+1Iw184pZ1CdgwaAAPCu17yrd+2NMnraW574AUX+p5HN"
    b"/8iS6wu9XZnSWnKaURoHaKcVACJSPQcgQfENU25u7X66TbdUsICmeD+GgsfO04YExJltb9uGomz4rDBLLjPERnUafbF7U70G"
    b"cfoVpOXEUek9IYyXl4d1CU5dIC0x6GW9bKG30NkcjT5iLN+QG/M75eQ/fP0NHZyj0pYZztHHi972osHh6w93bj9+jX3GW+Vv"
    b"bn7d0W90wI3Tw1Mv6mVzD4GozigfcdwkVmTLlqbFL9P2l9tOFZvteP/+dSrZcl9ueYHkpFfvtHuyVM4lDUYk9dkSOdoHNVlQ"
    b"mjoq6wEwpzfWP2Vo33LJnp1/dN3yT24e6vezY/v389qlpdG5Os8ED5AHSfnga29/iNUbP0Ko1y10dj781PD0CISGFN2qkyus"
    b"RKXGbU3cn3LnfRai68KnwwiLeA0/g/4EMZEQL71HuA6rj0NEwhQbTTfWhQB+/C6R+N0PAZiM82MhgMRCgEqqzzbG7KlUn/9r"
    b"TM8d77yYUmpE5fwI04Dle4QVWM735vTGaPP9Sg3/7V0neh981Y2vGj1Q5lX2QLjIQ/1DmYjkAL7+/p/9h/+e591jm6PBv9jT"
    b"2/PsjXwDAzMYAOjSow83g4CCpjJff52K1R7EDEMYmlReMyUkEpMB24qGoJyxja9F2Eumn7B2BZ3mXsrvzxZZAGloe1Il24a1"
    b"Fa7gSLFMJ+6QXzU5cfQJUHyRM7pF1kSuRKTX6erN0SaHZvTfjIxufNkbfuoDxeGCfl/kLEp5fdN5AIf6zICbcN3ydTlA+T8/"
    b"edtL55R6jco6z8x0p7s52gQJawuZsVoDQMyE9Ee9AaabgTRlAkB/nUt7EdKii3ETkCiRDsbNHgActD5G9pki+NXVW7yVveEc"
    b"EY8iratsPcQ+rpnECvBXJe6kAEH3mDAjAJCWSoQdpZQSwSgffI2Kf25G5i0vf+urbwMgh/p9fdMy7DLO/cn/gDIATuQlq4ur"
    b"aml1yRz6yU8+Dpn9FaG8yIJaKS2GhnAcPyDN9qv3FtLGwEaQ/zapvxj7z7bUEoyFAKgxAJXzMM4wRE0IAKe5VXmEct1+SUzQ"
    b"KNKf2FZx7RNpQPcbCSsIfi2LT5o6SCToQQFLUAgoLYCltTRfFfL3uGfwK0vLP3n68A2HO9e+6trRA20+PSANgEx9T/zN6z52"
    b"pUb3lQL+RK+zcPHADExu8hwifkjArVcKhhOp2XNoMAb0BU5iHkZskqcwgGS6sII3uOcPDUAsjx6P86uxeN0qz6ihSOX6Uw3g"
    b"JEIRlpqmbhJDYBpz/TaKBgmshWCz1+kuKAIDs/Ee0P6WXh8detl/ffVdwJi/IstiH2jzST3wLJYQoNxQFBG94M1PvF266gYL"
    b"+/r10dotSjK9Z/7i3hg3ZD5uRCSN8e6ZFvCcb192Ntcav/KxqnLctiVYDP2XSGZhQriyJI3Smdoxt3PBGHN8fbD5BwZ44+LT"
    b"9/zxy/7rq+9aWVnR/X4/eyBO/gekBxB6A3/5GnRf9DYZjL2BT34fyNcsdHdcs2k2LwAUrDUw4x4lsiWkP1zVGS8Usg2eA9BU"
    b"/IOGZqUx3KGFB8BUoVAbD6AtiSeBAcxK9kmGAO5nlzBbjAlok2x/pNz6ik81OZ6ZUqJEkNtRLuA3rM1Xu7r3az/wxh/5IkG5"
    b"8fobswcS4v+gMwBhSLCyeFt39yN4aZe9HxoZ8/O97sIFG6MNApKTzCBKGlOEUX1AeBMQXmzuI9H1cT2KenIfoKszIpPz1zQ4"
    b"SaYZz6oBqAMB0RIDQDQlKA39m6cxeogBTO9krPcyHAOQAgHHCkDWAsx7WadrrUFuR7d0gV/dNPce+pG3/tQpEanVVDlvAO5r"
    b"I+DUDwDAyqs/9NAL9J4XINOLxpgXXbDjYn1y4wRymgEg2ViBuDpBpcEAWIqnuOvn6UMoKTKxWZ2A1Zg/wSVgFd0HQu26SBaA"
    b"KYxhVgygTRYggQFEgLhk10Sp9wC8GjwhGBgA31Oo3tnJL+R5GwLQUoQ5CJnv9rJulmFtc+323Iz+RKz9i+7XFv52aXXJTMbb"
    b"8pg3+M0cmJ1zj5VF6r0LX+hc93uP2gSAv37tp55jiB/NVPZsQ/vYbmdOcpNjaIYkCBElbWoIQgMQTxGmQ4fQADQ1GN2KAUim"
    b"AR+ABqDqRSCSZYjX5UvkTqYNQOEjjd1I1et0xyGjHd0jtJ8y1vxxPsr/5yt/85VfA4C3/9jb5/7pf/unAxF50MA9DyoD4PgE"
    b"E5bKof7n59aPj55P4OcFfAZFFIBE8qdJdDQs1Kl2B0obA5kw/1Kr95kJjmynAahj+NXH9slJHhTyYMYQoNbNlxomYMK0iiAI"
    b"lCCZUsjN6Osg/1CTv/HDb/3hz/T7fbW8vGzxIH08KA0Ai6W2tNQr/du6O4/PP85w9DyKevmOuV1PEyicHpwckSDBjKJU29V7"
    b"ViDROuk/tpy8FZIPW4CElVDgLBuAKI23ChZiBg9Agko+REMAVAwRQqWf2nJgSxBGFIxS0tvRncfpzVMDJfbtmupPRcyRV/z6"
    b"K75cjqfCCDwoEz3Zg9OqjUfQyiI19kEvLcsQwMfeef3hz0pv91dObZ76no7qPk1EP36htxProzXkJs8BKIrIVPv1XDNsD7a1"
    b"h9t6nE9frtKTOVaZtQpKet1e1sl0dnrj5Mm1jVO3isLNw9Fo9Z+97UePlpN+P/ZniwcXRyJy3gN4QHsEpNz4qhuzO268wyxj"
    b"2d7w4sMLD71y148Q8mMi6gkQvRNFzGBoErLjaQCvEQ9gHVZQrx04xR/qjjtbHkADEShY2VHH9pM0UafyWS1i+4p3IGEasOoN"
    b"KAGUjFPDSnFA5vfQ8i/saPTb/+w3f+hDYxxppQvALK0uWXwTUDu+KQxAiAsAwO//7D/suHDUuXzI7j9SWv3LXjb/+NyOMDJD"
    b"AyhjyYwiimzHGLR14QHbVhsmQELW1xo0pR2jGY0pw72RZZdK4SFaGdgOA5CKqapLMzaLefj5fecaaalAQ4jVSrrz3TmsbZ7c"
    b"FKVWxY5+t6t7t3161633Li8v50nX4UH8yL55DIBwrDx8kz6IA1aWZQ3AZ/7ktR85Bu786uZo7TmgPK/bWXhSr7Og1wanMDSj"
    b"fAwpUKMAD4F01WDMJa3vE+A7tU1BZtv97qswpHn1cNx1Fn9LOX39artqYxC/8i+s2nNrOieNO6TM45MCGohQRHXmewuZAFgf"
    b"nLpnc7R5SATvtYK//edvWfxIeaWH+oeym3CTfTADft/kHoAPEh7s36Qvuudh+rVjEVL89o9/YtdFu+YWrTUvpcg+gg/tZvML"
    b"gMLIDpHbcZ+ScWfbKWs9XHWBdBVfapWvAxJjBCRbe9zZ8QDiWnxNHgCqQKKkCnJsFNxLJUpjvH2Oc/nQSkmmNJQSDEcDaMHX"
    b"jRl9GYKbrMj/etWbv/fDhbuvAegj+47k32wT/5vaACSAAllZgtpzxUfn1vKdzxlZvBKQF2VZb8fIDGEhBqRYgVNuXIfgT047"
    b"c9EQwqi6heho0oicVQPQLgvgpfBa0X3LfVNEIFQoVFLk81GEGpnWynIEWn5MBH8wovpTvbH39utvvCZ/MBB4zhuAbfQGyvLi"
    b"8r2V/m1dOdF7jBmpx0Gr51ja757v7XpMprrYGG1gYDYMIDkBBUKPKWEi0ZJizqYyHN3GZqxg+w0AgEg5bnMasJqmQ7SWPyTn"
    b"2IpKX7yUt9iHtCK0pLUKhIh0d87vglLAqbXjd4vwfQZ8fwZ+YKSzz7z6jd91V/n79vt9BQDfrKv+eQOQMAQ39aGPAapIGwIA"
    b"/uB1X7wSZvBdguypEDzOUl2ZZdmlvc4OjIzBMN9AbvOxOhhFMDEGDy4D0JYHkOTgz2AAXM/Au5O0JWoILdCZytDNuhBYrG+e"
    b"2tCZ+qzNR58H+CGIet+aOvH3P/umpQ0AuOH6w507jt8u+8cIvzk/4s8bgAaMAHIQgBycQsx/8NrPPwMW/5giLyDsYyGdzNBo"
    b"JUpPa8gI48hThvH/1mTHm0OHOgPgUoXrDQCiun/tDUB98U5Kz6+ult9F99WEICQgc4B2pEQbYnRCwPcR+KMBOn/z02+57l4A"
    b"6INq/+KqLK4s2gcTffe8AbiPjICUQWvxWFykXnzonRdtqMElJB6nRT3LkM9Xop8039uFkRlhkG/A0o4sFQVQJJUVKBbSC03q"
    b"Q2EPxJinYGfBEQLh0VkNAJLgHhNiHvUYgCs5VocBkCRk7OaP43tmmdZqvrcD1hpsDk7dIbC3COVmI8O/71Ddic2Nu1914/eu"
    b"V39HAc7LNZw3AFt59PtUBwD16TtvlVfd6Es+/cFPffkxBuaJI8vHiFL7QDyaIo/bOXfBhZAMg9E6RmaA3JjcylhCemwQSrZh"
    b"QzjwIDMAk+PETnwkKcXOxgAe1XjiE0Cnm3XR6/QgEKwP7gVoP6NEbrM2/6yC+gTIT50afP3Iz9+4dGKC3yyu6OMXXqkuPH67"
    b"Pb/ynzcA22wM+uoADqpdd0KuuQzGVYH57X/5xcuVqKcTOEDYZ0Kph1jLnRTu7mTzWkTBWANDC0sLS9JOtWalVN7zJihjocJ9"
    b"YwDQmN6LtciyST5+uG+JbIw1BkVKlp5WY7NgzAC0+ZoIToH2Xgo/pWnfZ8T+1eveet1t7u9SqkPdcdk1ZnnZafFz/nHeANyX"
    b"j5X+bd3N0xd3Tq9hx5wePskoHiDUcyl4eq+zU4/yIQwtcpqS3Tvhr9hxdjGAvdpN8trehqz2PwjPEUvh1SnpRgttpKZYx+ct"
    b"Fj17CAqpSAqsEiVKyzh3T2uQ2+FXFOTdsPl7Okbfmmfy1d0nN4ZfeOSB4fKy2POj7bwBuL9RAun3D8r+owcFi8DSklSQ5Rt+"
    b"6qtXAPkVBB+pVfdRI2MeD/DxBrJ/19zFHRGF3IwwskOM7AjG5oYUQ4xbzsBCKBBQxAISkpAalYsjSsh1BsDzD4RJ4xDtmVcY"
    b"AIKFGw/Hnbdld3elgU6WZci0RkfPQSvB2sY9sHb4RQE+Ya35pBb1aZJfgpUvnDi2/sXl1etOh2HZ/qOQI/vAgw8SYY7zBuBB"
    b"EB7sP3pQjuCIvuihXSkZhpPti+xe8rA7n6hs/gQSTyLlShF5KCEXkthlgQs62VxPZ3MAOA4XrAVpYEgYGhhrCwRNQDLE6cev"
    b"xQUYlWyTAZiiEbRQYWGzjBUXhVYgUFoUlBIoUVACiChkSsPaEQaD01bEnAB4Qou+l8zvgeLnhfiEMP84BtnHf+bGa+/07+2h"
    b"7KJ7NvTc4CF27ObLeQ3W8wbggXF/pyx2f5Xq9w/NXX7iqn2DnE8Uqsca4PEErgZwORW6sBzz4IQCqMl0hPgtxW2kHVqqa/HW"
    b"DYBL1fVdf1WeTY0X+6lElyXHHRrsuOOwANbeQ+AoaI4o4JNaqY8Nc3zs3/6XJx4PvatpJcD51f1sPrLzt+AsBgkgDvYPjsfx"
    b"sr9iLS9ftwngw7/yc8c+s7COXTkGOzta7cpz7FVUjzC0j6LgCoFcTuDhJB6+c+Hi+blehtwCxo5BBEPA0hTAoh1LWtHC0sDY"
    b"sSexlcVSRAAoaKWgx/AElOjx30pBiRTP8T5KAXm+ibX1u0Gbf5VQXwHMHRDcQSNfAPhplamvmxFOZ8TJEXj6kXc94eTSqkRJ"
    b"OQf7B4UHD5Y5vPNG4LwH8IA3BnKwDwFuUpffeUCAWxGmFd193/DqL1zazeYuMeTFlvpiS14CUZeA2EWxOyw4D2DBUi2IcN4S"
    b"CxSZJ7FAyDyAeUssQLADULrIs7X2AAAaATcFXBfBOmg3AW4IsCbABmA3BHYdgnUl3ADsBoCTQt6rgGMw5hvSkW9AjY5neu/X"
    b"f+6Nl63FQ6dDGY4eU9i3aPcfXeWRfUd4nqJ73gB8cxgFUpaWVtXi4iKOv/tWdcfxObn8wk0WcW5yEvR/7PNzOy/Zc6k1m3tH"
    b"m9lepfmQ3NiHQOtLjNhLhfoSkHspuNQClynRXUtLRrL7PtDntTjdFJh7ReHrpL1LE3db2ruVwtcE6piQd1tlv64lP9bdlLt+"
    b"/sZvPVH3Xa+//nDnssvm5PI7N3nHZae4/+ixcrKfj+fPP84/zj/OP84/zj/OP84/7tPH/we8ne1i4FnvngAAAABJRU5ErkJg"
    b"gg=="
)


def _application_icon():
    pixmap = QPixmap()
    pixmap.loadFromData(base64.b64decode(_LOGO_PNG), "PNG")
    return QIcon(pixmap)


def _configure_windows_identity():
    if os.name == "nt":
        try:
            api = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
            api.argtypes = [ctypes.c_wchar_p]
            api.restype = ctypes.c_long
            api(_WINDOWS_APP_ID)
        except (AttributeError, OSError):
            pass


# ------------------------------------------------------- process / window helpers
def detach_from_console():
    """Never show a terminal window: relaunch under pythonw, or hide the console."""
    if os.name != "nt":
        return
    try:
        exe = Path(sys.executable)
        if (not getattr(sys, "frozen", False) and exe.name.lower() == "python.exe"
                and not os.environ.get("DICTATE_CHILD")):
            pyw = exe.with_name("pythonw.exe")
            if pyw.exists():
                subprocess.Popen([str(pyw), str(Path(__file__).resolve())] + sys.argv[1:],
                                 env=dict(os.environ, DICTATE_CHILD="1"), close_fds=True,
                                 creationflags=0x08000000 | 0x00000008)   # NO_WINDOW | DETACHED
                sys.exit(0)
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))


def log_crash(exc_type, exc, tb):
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        (APP_DIR / "error.log").write_text("".join(traceback.format_exception(exc_type, exc, tb)),
                                           encoding="utf-8")
    except Exception:
        pass


def _foreground_api():
    """Use pointer-sized HWND types on 64-bit Windows."""
    if os.name != "nt":
        return None
    u = ctypes.windll.user32
    u.GetForegroundWindow.argtypes = []
    u.GetForegroundWindow.restype = ctypes.c_void_p
    u.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    u.SetForegroundWindow.restype = ctypes.c_int
    return u


def _is_passive_window(widget):
    return bool(widget is not None and widget.window().property("_dictate_passive_popup"))


class _InputFocusGuard:
    """Preserve the current editor/selection around our non-interactive popups.

    Restore only when Dictate's own passive popup took focus. Never activate an
    old app after the user deliberately switches to a different app or control.
    """
    def __init__(self):
        self.focus_widget = self.editor = self.window = None
        self.selection = None
        self.api = None
        self.foreground = 0
        try:
            self.api = _foreground_api()
            if self.api:
                self.foreground = int(self.api.GetForegroundWindow() or 0)
            widget = QApplication.focusWidget()
            if widget is None or _is_passive_window(widget):
                return
            # Qt can retain a last-focused widget while another app is active.
            # In that case preserve only the native target, never activate our
            # old settings window after repairing a popup's accidental focus.
            if self.api and self.foreground and int(widget.window().winId()) != self.foreground:
                return
            self.focus_widget, self.window = weakref.ref(widget), weakref.ref(widget.window())
            # Editable combo boxes report the combo as the focus widget even
            # while their child line edit owns the selected text.
            if isinstance(widget, QComboBox) and widget.isEditable():
                widget = widget.lineEdit()
            self.editor = weakref.ref(widget)
            if isinstance(widget, QPlainTextEdit):
                cursor = widget.textCursor()
                self.selection = ("plain", widget.document().revision(), cursor.anchor(), cursor.position())
            elif isinstance(widget, QLineEdit):
                self.selection = ("line", widget.text(), widget.selectionStart(),
                                  widget.selectionLength(), widget.cursorPosition())
        except (RuntimeError, OSError, AttributeError):
            pass

    def restore(self, deferred=False):
        stolen = False
        try:
            if self.api and self.foreground:
                foreground = int(self.api.GetForegroundWindow() or 0)
                if foreground and foreground != self.foreground:
                    passive = any(w.isVisible() and _is_passive_window(w)
                                  and int(w.winId()) == foreground
                                  for w in QApplication.topLevelWidgets())
                    if not passive:
                        return
                    self.api.SetForegroundWindow(self.foreground)
                    stolen = True
            widget = self.focus_widget() if self.focus_widget else None
            editor = self.editor() if self.editor else None
            window = self.window() if self.window else None
            if widget is None or window is None or not widget.isVisible() or not widget.isEnabled():
                return
            active = QApplication.activeWindow()
            if active is not None and _is_passive_window(active):
                window.activateWindow()  # repair only our own accidental activation
                stolen = True
            elif active is not window:
                return
            current = QApplication.focusWidget()
            if current is not widget:
                if current is not None and not _is_passive_window(current):
                    return
                widget.setFocus(Qt.FocusReason.OtherFocusReason)
                stolen = True
            # A deferred guard must not undo a later, intentional selection edit.
            if deferred and not stolen:
                return
            if not self.selection or editor is None:
                return
            if self.selection[0] == "plain":
                _, revision, anchor, position = self.selection
                if editor.document().revision() != revision:
                    return
                cursor = editor.textCursor()
                if cursor.anchor() != anchor or cursor.position() != position:
                    cursor.setPosition(anchor)
                    cursor.setPosition(position, QTextCursor.MoveMode.KeepAnchor)
                    editor.setTextCursor(cursor)
            else:
                _, text, start, length, position = self.selection
                if editor.text() != text:
                    return
                if length > 0:
                    if position == start:
                        editor.setSelection(start + length, -length)
                    else:
                        editor.setSelection(start, length)
                else:
                    editor.setCursorPosition(position)
        except (RuntimeError, OSError, AttributeError, ValueError):
            pass  # the target may have been closed while a native popup was shown


def _preserve_input_focus(method):
    @functools.wraps(method)
    def guarded(*args, **kwargs):
        guard = _InputFocusGuard()
        try:
            return method(*args, **kwargs)
        finally:
            guard.restore()
            QTimer.singleShot(0, lambda: guard.restore(deferred=True))
    return guarded


class _DictationShortcutFilter(QObject):
    """Do not let a global dictation key replace selected text in our editor.

    Some Qt/platform combinations also deliver the registered Alt+letter to the
    focused widget. The global hotkey still performs the action; this filter
    only prevents that duplicate key event from editing Dictate's own inputs.
    """
    def __init__(self, app):
        super().__init__(app.qt)
        self.app = weakref.ref(app)
        app.qt.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() not in (QEvent.Type.KeyPress, QEvent.Type.ShortcutOverride):
            return False
        app = self.app()
        if app is None or not getattr(app, "_dictation_shortcut_registered", False):
            return False
        # Keep the existing shortcut editor able to capture a replacement key.
        focus = QApplication.focusWidget()
        while focus is not None:
            if isinstance(focus, QKeySequenceEdit):
                return False
            focus = focus.parentWidget()
        sequence = app.settings.get("dict_hotkey", "")
        if sequence and QKeySequence(event.keyCombination()) == QKeySequence(sequence):
            event.accept()
            return True
        return False


class _NoActivatePopup(QWidget):
    """Keep the existing popup appearance but never take the keyboard cursor."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setProperty("_dictate_passive_popup", True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    def showEvent(self, event):
        # Do this after subclass construction has set translucency, but before
        # the native window is mapped. Creating the HWND in __init__ breaks alpha.
        if os.name == "nt":
            try:
                u = ctypes.windll.user32
                get_style = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
                set_style = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
                get_style.argtypes = [ctypes.c_void_p, ctypes.c_int]
                get_style.restype = ctypes.c_ssize_t
                set_style.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
                set_style.restype = ctypes.c_ssize_t
                hwnd = int(self.winId())
                style = get_style(hwnd, -20)  # GWL_EXSTYLE
                set_style(hwnd, -20, style | 0x08000000 | 0x00000080)  # NOACTIVATE | TOOLWINDOW
            except (AttributeError, OSError, TypeError, ValueError):
                pass
        super().showEvent(event)

    def nativeEvent(self, event_type, message):
        if os.name == "nt":
            try:
                from ctypes import wintypes
                msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
                if msg.message == 0x0021:  # WM_MOUSEACTIVATE
                    return True, 3  # MA_NOACTIVATE, but still deliver the icon click
            except (ValueError, TypeError, OSError):
                pass
        # QWidget's default native handler does not set a result on every
        # platform. Return an explicit unused result instead of reading it.
        return False, 0


def raise_topmost(widget):
    """Win32: put the window back above the taskbar without activating it."""
    if os.name != "nt":
        return
    try:
        f = ctypes.windll.user32.SetWindowPos
        f.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                      ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        f(int(widget.winId()), -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
    except Exception:
        pass


# ----------------------------------------------------------------- settings
def load_settings():
    s = dict(DEFAULTS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            # Preserve both original keys. A saved single-shortcut configuration
            # keeps its recording key and regains the default visibility key.
            if "dict_hotkey" not in saved and saved.get("hotkey"):
                saved["dict_hotkey"] = saved["hotkey"]
                saved["hotkey"] = DEFAULTS["hotkey"]
            s.update(saved)
    except Exception:
        pass
    return s


def save_settings(s):
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(s), encoding="utf-8")
    except Exception:
        pass


_WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _startup_command():
    """Use the installed executable, never PyInstaller's internal script path."""
    exe = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        command = f'"{exe}" --startup'
    else:
        pyw = exe.with_name("pythonw.exe")
        command = f'"{pyw if pyw.is_file() else exe}" "{Path(__file__).resolve()}" --startup'
    # Run-key commands have a documented 260-character Windows limit.
    if len(command.encode("utf-16-le")) // 2 > 260:
        raise ValueError("The Windows startup command is too long.")
    return command


def set_startup(enabled):
    """Register this user's startup preference and verify an enabled entry."""
    try:
        import winreg
        if enabled:
            command = _startup_command()
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WINDOWS_RUN_KEY,
                                    0, winreg.KEY_QUERY_VALUE) as key:
                    existing, kind = winreg.QueryValueEx(key, APP_NAME)
                if existing == command and kind == winreg.REG_SZ:
                    return True
            except FileNotFoundError:
                pass
            # A new Windows profile need not have a Run key yet. OpenKey alone
            # fails there; create only this current user's key when needed.
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _WINDOWS_RUN_KEY,
                                    0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
                existing, kind = winreg.QueryValueEx(key, APP_NAME)
                return existing == command and kind == winreg.REG_SZ
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WINDOWS_RUN_KEY,
                                0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, APP_NAME)
        except FileNotFoundError:
            pass  # Already disabled, including a profile without a Run key.
        return True
    except Exception:
        return False


def sync_startup(settings, *, started_at_login=False):
    """Repair a saved preference on manual launch/install, not during logon."""
    if os.name != "nt" or started_at_login:
        return True
    return set_startup(bool(settings.get("start_on_startup", False)))


def list_input_devices():
    """Names of the usable microphones (only the default host API, so no duplicates)."""
    try:
        import sounddevice as sd
        try:
            host = sd.query_devices(kind="input")["hostapi"]
        except Exception:
            host = None
        names = []
        for d in sd.query_devices():
            if d["max_input_channels"] > 0 and (host is None or d["hostapi"] == host) \
                    and d["name"] not in names:
                names.append(d["name"])
        return names
    except Exception:
        return []


# The Unified export uses transcribe.cpp's GGUF layout, not CrispASR's layout.
def _gguf_info(path):
    """Inspect only metadata; never guess the runtime from a renamed filename."""
    size = Path(path).stat().st_size
    info = {"runtime": "crispasr", "architecture": "unknown", "quantization": _model_quantization_name(path), "bytes": size}
    with open(path, "rb") as f:
        def read(n):
            if n < 0 or f.tell() + n > size:
                raise ValueError("Incomplete GGUF file. Please download it again.")
            data = f.read(n)
            if len(data) != n:
                raise ValueError("Incomplete GGUF file.")
            return data

        def uint32():
            return struct.unpack("<I", read(4))[0]

        def uint64():
            return struct.unpack("<Q", read(8))[0]

        def string(keep=False):
            n = uint64()
            if n > 16_000_000 or f.tell() + n > size:
                raise ValueError("Invalid GGUF metadata.")
            if keep:
                return read(n).decode("utf-8", errors="replace")
            f.seek(n, 1)

        widths = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1,
                  10: 8, 11: 8, 12: 8}

        def skip(kind, count=1):
            if kind in widths:
                n = widths[kind] * count
                if f.tell() + n > size:
                    raise ValueError("Truncated GGUF metadata.")
                f.seek(n, 1)
            elif kind == 8:
                for _ in range(count):
                    string()
            else:
                raise ValueError("Unsupported GGUF metadata type.")

        if read(4) != b"GGUF" or uint32() not in (2, 3):
            raise ValueError("Please select a valid GGUF v2/v3 model file.")
        tensors, pairs = uint64(), uint64()
        if tensors < 1 or pairs > 20000:
            raise ValueError("Invalid GGUF header.")
        for _ in range(pairs):
            if f.tell() > 64_000_000:
                raise ValueError("GGUF metadata is too large.")
            key, kind = string(True), uint32()
            if key.startswith("stt."):
                info["runtime"] = "transcribe_cpp"
            if key in ("general.architecture", "stt.variant", "general.name") and kind == 8:
                value = string(True)
                if key == "general.architecture":
                    info["architecture"] = value
                elif key == "stt.variant":
                    info["variant"] = value
                continue
            if key == "general.file_type" and kind == 4:
                file_type = uint32()
                info["file_type"] = file_type
                continue
            if kind == 9:
                element, count = uint32(), uint64()
                if count > 2_000_000:
                    raise ValueError("Invalid GGUF array.")
                skip(element, count)
            else:
                skip(kind)
    if info['quantization'] == 'unknown' and 'file_type' in info:
        # CrispASR writes ggml_ftype; Unified uses llama-style mixture codes.
        # Code 12 is Q4_K in CrispASR, NOT Unified's Q3_K_M.
        common = {0: 'F32', 1: 'F16', 2: 'Q4_0', 3: 'Q4_1', 7: 'Q8_0', 8: 'Q5_0', 9: 'Q5_1', 10: 'Q2_K'}
        specific = ({11: 'Q3_K_S', 12: 'Q3_K_M', 13: 'Q3_K_L', 14: 'Q4_K_S', 15: 'Q4_K_M',
                     16: 'Q5_K_S', 17: 'Q5_K_M', 18: 'Q6_K', 32: 'BF16'}
                    if info['runtime'] == 'transcribe_cpp' else
                    {11: 'Q3_K', 12: 'Q4_K', 13: 'Q5_K', 14: 'Q6_K'})
        info['quantization'] = (common | specific).get(info['file_type'], 'unknown')
    return info


def _gguf_runtime(path):
    return _gguf_info(path)["runtime"]


class _UnifiedSession:
    """Adapt the supported transcribe.cpp API to the existing Engine API."""
    def __init__(self, path, n_threads=4):
        import transcribe_cpp
        self._model = transcribe_cpp.Model(path, backend="cpu")
        try:
            self._session = self._model.session(n_threads=n_threads)
        except Exception:
            self._model.close()
            raise

    def transcribe(self, audio, sample_rate=16000):
        if sample_rate != SR:
            raise ValueError("Unified expects 16 kHz mono audio.")
        return self._session.run(np.ascontiguousarray(audio, dtype=np.float32)).text

    def close(self):
        try:
            self._session.close()
        finally:
            self._model.close()


# Both Windows runtimes ship different DLLs named ggml.dll/ggml-base.dll.
# Keep each native session in its own local process so model switching cannot
# accidentally bind one runtime against the other's incompatible DLL exports.
def _native_session_worker(connection, runtime, path, n_threads):
    model = None
    try:
        for name in ("stdout", "stderr"):
            if getattr(sys, name) is None:
                setattr(sys, name, open(os.devnull, "w"))
        if os.name == "nt":
            ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
        if runtime == "transcribe_cpp":
            model = _UnifiedSession(path, n_threads=n_threads)
        else:
            import crispasr
            model = crispasr.Session(path, n_threads=n_threads)
        connection.send(("ready", None))
        while True:
            command, audio = connection.recv()
            if command == "close":
                break
            try:
                if isinstance(audio, int):
                    if not 0 <= audio <= 120 * SR:
                        raise ValueError("Invalid audio packet length.")
                    raw = connection.recv_bytes(120 * SR * 4)
                    if len(raw) != audio * 4:
                        raise ValueError("Incomplete audio packet.")
                    audio = np.frombuffer(raw, dtype=np.float32)
                connection.send(("text", Engine._run(model, audio)))
            except Exception as exc:
                connection.send(("error", str(exc) or type(exc).__name__))
            finally:
                audio = raw = None
    except (EOFError, BrokenPipeError):
        pass
    except Exception as exc:
        try:
            connection.send(("error", str(exc) or type(exc).__name__))
        except (OSError, EOFError):
            pass
    finally:
        if model is not None:
            try:
                model.close()
            except Exception:
                pass
        connection.close()


class _WindowsNativeSession:
    """Isolated native DLLs; model memory belongs to a disposable child process."""
    def __init__(self, path, n_threads=4, runtime="crispasr", cancel_event=None):
        self._cancel_event = cancel_event
        context = multiprocessing.get_context("spawn")
        self._connection, child = context.Pipe()
        self._process = context.Process(target=_native_session_worker,
                                        args=(child, runtime, str(path), n_threads),
                                        name="DictateSpeech", daemon=True)
        try:
            self._process.start()
            child.close()
            status, message = self._receive()
            if status != "ready":
                raise RuntimeError(message)
        except Exception:
            self.close(force=bool(cancel_event is not None and cancel_event.is_set()))
            raise
        finally:
            child.close()

    def _receive(self):
        connection = self._connection
        if connection is None:
            raise RuntimeError("The local speech engine is closed.")
        try:
            if self._cancel_event is None:
                ready = connection.poll(180)
            else:
                deadline, ready = time.monotonic() + 180, False
                while not ready and time.monotonic() < deadline:
                    if self._cancel_event.is_set():
                        self.close(force=True)
                        raise RuntimeError("Speech engine loading was cancelled.")
                    ready = connection.poll(.2)
            if not ready:
                self.close(force=True)
                raise RuntimeError("The local speech engine did not respond.")
            return connection.recv()
        except (EOFError, OSError) as exc:
            self.close(force=True)
            raise RuntimeError("The local speech engine stopped unexpectedly.") from exc

    def transcribe(self, audio, sample_rate=16000):
        if sample_rate != SR:
            raise ValueError("The speech engine expects 16 kHz mono audio.")
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        if audio.ndim != 1:
            raise ValueError("The speech engine expects mono audio.")
        if audio.size == 0:
            return ""
        if audio.size > SR * 120:
            raise ValueError("The speech engine accepts at most 120 seconds per packet.")
        if self._connection is None:
            raise RuntimeError("The local speech engine is closed.")
        self._connection.send(("transcribe", int(audio.size)))
        self._connection.send_bytes(memoryview(audio).cast("B"))
        status, text = self._receive()
        if status != "text":
            raise RuntimeError(text)
        return text

    def close(self, force=False):
        connection, self._connection = self._connection, None
        process, self._process = self._process, None
        if force and process is not None and process.is_alive():
            process.terminate()
        if connection is not None:
            if not force:
                try:
                    connection.send(("close", None))
                except (OSError, EOFError):
                    pass
            connection.close()
        if process is not None and getattr(process, "pid", True) is not None:
            process.join(2)
            if process.is_alive():
                process.terminate()
                process.join(2)



# Spoken symbol names. These are output transformations, not keyboard shortcuts
# or UI controls. Ordinary words such as "at", "hash", "dollar", "plus" and
# "alpha" are deliberately left alone unless an explicit symbol name is used.
_SYMBOL_ROWS = [
    ("at sign|at symbol", "@"),
    ("hash sign|hash symbol|number sign|hashtag|hash tag", "#"),
    ("dollar sign|dollar symbol", "$"),
    ("percent sign|percentage sign|percent symbol", "%"),
    ("ampersand|ampersand symbol", "&"),
    ("asterisk|asterisk symbol|star symbol", "*"),
    ("plus sign|plus symbol", "+"),
    ("minus sign|hyphen|hyphen symbol", "-"),
    ("forward slash|slash symbol", "/"),
    ("backslash|back slash|backslash symbol", "\\"),
    ("equals sign|equal sign|equals symbol", "="),
    ("underscore|underscore symbol", "_"),
    ("tilde|tilde symbol", "~"),
    ("vertical bar|pipe symbol", "|"),
    ("caret|caret symbol", "^"),
    ("exclamation mark|exclamation point", "!"),
    ("question mark", "?"),
    ("full stop|period symbol|dot symbol|decimal point|dot", "."),
    ("comma|comma symbol", ","),
    ("semicolon|semi colon|semicolon symbol", ";"),
    ("colon|colon symbol", ":"),
    ("apostrophe|single quote|single quotation mark", "'"),
    ("double quote|double quotation mark|open double quote|close double quote", '"'),
    ("backtick|back tick|grave accent", "`"),
    ("open parenthesis|left parenthesis|open round bracket", "("),
    ("close parenthesis|right parenthesis|close round bracket", ")"),
    ("open square bracket|left square bracket|open bracket", "["),
    ("close square bracket|right square bracket|close bracket", "]"),
    ("open curly brace|left curly brace|open curly bracket|open brace", "{"),
    ("close curly brace|right curly brace|close curly bracket|close brace", "}"),
    ("less than sign|open angle bracket|left angle bracket", "<"),
    ("greater than sign|close angle bracket|right angle bracket", ">"),
    ("ellipsis|ellipsis symbol", "…"),
    ("en dash|en dash symbol", "–"),
    ("em dash|em dash symbol", "—"),
    ("bullet point|bullet symbol", "•"),
    ("middle dot|interpunct symbol", "·"),
    ("inverted exclamation mark", "¡"),
    ("inverted question mark", "¿"),
    ("section sign|section symbol", "§"),
    ("paragraph sign|paragraph symbol|pilcrow symbol", "¶"),
    ("degree sign|degree symbol", "°"),
    ("copyright sign|copyright symbol", "©"),
    ("registered trademark symbol|registered sign", "®"),
    ("trademark sign|trademark symbol", "™"),
    ("dagger symbol", "†"),
    ("double dagger symbol", "‡"),
    ("per mille sign|per mille symbol", "‰"),
    ("per ten thousand sign|basis point symbol", "‱"),
    ("prime symbol|single prime symbol", "′"),
    ("double prime symbol", "″"),
    ("left double quotation mark|opening curly double quote", "“"),
    ("right double quotation mark|closing curly double quote", "”"),
    ("left single quotation mark|opening curly single quote", "‘"),
    ("right single quotation mark|closing curly single quote", "’"),
    ("left guillemet|open guillemet", "«"),
    ("right guillemet|close guillemet", "»"),
    ("check mark|tick mark|checkmark symbol", "✓"),
    ("cross mark|ballot cross symbol", "✗"),
    ("black star symbol|filled star symbol", "★"),
    ("white star symbol|hollow star symbol", "☆"),
    ("heart symbol|heart suit symbol", "♥"),
    ("music note symbol|eighth note symbol", "♪"),
    ("double music note symbol|beamed note symbol", "♫"),
    ("recycling symbol|recycle symbol", "♻"),
    ("warning symbol|warning sign", "⚠"),
    ("euro sign|euro symbol", "€"),
    ("pound sign|pound sterling sign|sterling symbol", "£"),
    ("yen sign|yuan sign|yen symbol", "¥"),
    ("rupee sign|indian rupee sign|rupee symbol", "₹"),
    ("won sign|korean won sign", "₩"),
    ("ruble sign|rouble sign", "₽"),
    ("bitcoin sign|bitcoin symbol", "₿"),
    ("cent sign|cent symbol", "¢"),
    ("dong sign|vietnamese dong sign", "₫"),
    ("baht sign|thai baht sign", "฿"),
    ("shekel sign|shekel symbol", "₪"),
    ("peso sign|philippine peso sign", "₱"),
    ("naira sign|naira symbol", "₦"),
    ("lira sign|turkish lira sign", "₺"),
    ("hryvnia sign|hryvnia symbol", "₴"),
    ("colon currency sign|costa rican colon sign", "₡"),
    ("cedi sign|cedi symbol", "₵"),
    ("guarani sign|guarani symbol", "₲"),
    ("kip sign|lao kip sign", "₭"),
    ("tugrik sign|tugrik symbol", "₮"),
    ("tenge sign|tenge symbol", "₸"),
    ("manat sign|manat symbol", "₼"),
    ("lari sign|lari symbol", "₾"),
    ("generic rupee sign", "₨"),
    ("generic currency sign|currency symbol", "¤"),
    ("multiplication sign|times symbol", "×"),
    ("division sign|divide symbol", "÷"),
    ("plus minus sign|plus or minus sign", "±"),
    ("minus plus sign|minus or plus sign", "∓"),
    ("not equal sign|not equals sign|not equal to sign", "≠"),
    ("approximately equal sign|approximately equals sign|approximation symbol", "≈"),
    ("identical to sign|equivalent to sign", "≡"),
    ("less than or equal sign|less than or equal to sign", "≤"),
    ("greater than or equal sign|greater than or equal to sign", "≥"),
    ("infinity symbol|infinity sign", "∞"),
    ("square root symbol|square root sign", "√"),
    ("cube root symbol|cube root sign", "∛"),
    ("fourth root symbol|fourth root sign", "∜"),
    ("summation symbol|summation sign", "∑"),
    ("product symbol|product sign", "∏"),
    ("integral symbol|integral sign", "∫"),
    ("double integral symbol|double integral sign", "∬"),
    ("partial derivative symbol|partial differential symbol", "∂"),
    ("increment symbol|delta increment symbol", "∆"),
    ("nabla symbol|del operator symbol", "∇"),
    ("element of symbol|belongs to sign", "∈"),
    ("not an element of symbol|not element of symbol", "∉"),
    ("contains as member symbol", "∋"),
    ("empty set symbol|null set symbol", "∅"),
    ("union symbol|set union symbol", "∪"),
    ("intersection symbol|set intersection symbol", "∩"),
    ("subset symbol|subset of symbol", "⊂"),
    ("superset symbol|superset of symbol", "⊃"),
    ("subset or equal symbol|subset of or equal symbol", "⊆"),
    ("superset or equal symbol|superset of or equal symbol", "⊇"),
    ("logical and symbol|logical conjunction symbol", "∧"),
    ("logical or symbol|logical disjunction symbol", "∨"),
    ("logical not symbol|negation symbol", "¬"),
    ("for all symbol|universal quantifier symbol", "∀"),
    ("there exists symbol|existential quantifier symbol", "∃"),
    ("therefore symbol|therefore sign", "∴"),
    ("because symbol|because sign", "∵"),
    ("proportional to symbol|proportional sign", "∝"),
    ("angle symbol|angle sign", "∠"),
    ("perpendicular symbol|perpendicular sign", "⊥"),
    ("parallel symbol|parallel sign", "∥"),
    ("aleph symbol|aleph sign", "ℵ"),
    ("left arrow|left arrow symbol", "←"),
    ("right arrow|right arrow symbol", "→"),
    ("up arrow|up arrow symbol", "↑"),
    ("down arrow|down arrow symbol", "↓"),
    ("left right arrow|horizontal double arrow", "↔"),
    ("up down arrow|vertical double arrow", "↕"),
    ("north west arrow|upper left arrow", "↖"),
    ("north east arrow|upper right arrow", "↗"),
    ("south west arrow|lower left arrow", "↙"),
    ("south east arrow|lower right arrow", "↘"),
    ("double right arrow|right implication symbol", "⇒"),
    ("double left arrow|left implication symbol", "⇐"),
    ("double left right arrow|equivalence arrow", "⇔"),
    ("maps to symbol|maps to arrow", "↦"),
    ("one half symbol|half fraction symbol", "½"),
    ("one quarter symbol|quarter fraction symbol", "¼"),
    ("three quarters symbol|three quarter fraction symbol", "¾"),
    ("one third symbol|third fraction symbol", "⅓"),
    ("two thirds symbol|two third fraction symbol", "⅔"),
    ("one eighth symbol|eighth fraction symbol", "⅛"),
    ("superscript two|squared symbol", "²"),
    ("superscript three|cubed symbol", "³"),
    ("greek alpha symbol|alpha symbol", "α"),
    ("greek beta symbol|beta symbol", "β"),
    ("greek gamma symbol|gamma symbol", "γ"),
    ("greek delta symbol|delta symbol", "δ"),
    ("greek theta symbol|theta symbol", "θ"),
    ("greek lambda symbol|lambda symbol", "λ"),
    ("greek mu symbol|mu symbol", "μ"),
    ("greek pi symbol|pi symbol", "π"),
    ("greek sigma symbol|sigma symbol", "σ"),
    ("greek phi symbol|phi symbol", "φ"),
    ("greek omega symbol|lowercase omega symbol", "ω"),
    ("capital omega symbol|uppercase omega symbol", "Ω"),
]
SPOKEN_SYMBOLS = {name: symbol for aliases, symbol in _SYMBOL_ROWS for name in aliases.split("|")}
_SYMBOL_PATTERN = re.compile(
    r"(?<!\w)(?:" + "|".join(
        r"[\s-]+".join(re.escape(word) for word in name.split())
        for name in sorted(SPOKEN_SYMBOLS, key=len, reverse=True)
    ) + r")(?!\w)", re.IGNORECASE)


def expand_spoken_symbols(text):
    """Convert explicit spoken names; preserve ordinary prose and literal symbols."""
    matches = list(_SYMBOL_PATTERN.finditer(text))
    if not matches:
        return text
    names = [re.sub(r"[\s-]+", " ", match.group()).lower() for match in matches]
    symbols = [SPOKEN_SYMBOLS[name] for name in names]
    literals, last = [], 0
    for match in matches:
        literals.append(text[last:match.start()])
        last = match.end()
    literals.append(text[last:])
    # ASR often adds commas/full stops to a spoken list. In a symbols-only
    # utterance they are sentence punctuation, not extra requested characters.
    if all(re.fullmatch(r"[\s,.;:!?]*", literal) for literal in literals):
        return "".join(symbols)
    opening = set("([{<«“‘")
    closing = set(",.;:!?)]}>»”’…")
    currency = set("$€£¥₹₩₽₿¢₫฿₪₱₦₺₴₡₵₲₭₮₸₼₾₨¤")
    pieces = []
    for index, literal in enumerate(literals):
        if 0 < index < len(symbols) and re.fullmatch(r"[\s,;]*", literal):
            literal = ""  # consecutive symbol commands compose e.g. @#$
        else:
            if index < len(symbols) and (symbols[index] in closing or
                    (names[index].startswith(("close ", "closing ")) and "quote" in names[index])):
                literal = literal.rstrip(" \t")
            if index > 0:
                previous = symbols[index - 1]
                if previous in opening or (names[index - 1].startswith(("open ", "opening "))
                                           and "quote" in names[index - 1]):
                    literal = literal.lstrip(" \t")
                if previous in currency and re.match(r"[ \t]+[0-9]", literal):
                    literal = literal.lstrip(" \t")
                if index == len(symbols) and previous in ".!?…" and re.fullmatch(r"\s*\.\s*", literal):
                    literal = ""
        pieces.append(literal)
        if index < len(symbols):
            pieces.append(symbols[index])
    return "".join(pieces).strip()


# Natural amounts and clear mathematical/email contexts need no "sign" command.
_SMALL_NUMBERS = {name: value for value, name in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_SMALL_NUMBERS.update({name: value for value, name in zip(
    range(20, 100, 10), "twenty thirty forty fifty sixty seventy eighty ninety".split())})
_NUMBER_SCALES = {"hundred": 100, "thousand": 1000, "lakh": 100000, "lakhs": 100000,
                  "lac": 100000, "lacs": 100000, "million": 1000000,
                  "crore": 10000000, "crores": 10000000, "billion": 1000000000,
                  "trillion": 1000000000000}
# Standard English short-scale names; digit-by-digit IDs do not depend on
# this vocabulary and can be as long as the recognized transcript.
_NUMBER_SCALES.update({name: 10 ** exponent for exponent, name in zip(
    range(15, 64, 3),
    "quadrillion quintillion sextillion septillion octillion nonillion decillion "
    "undecillion duodecillion tredecillion quattuordecillion quindecillion "
    "sexdecillion septendecillion octodecillion novemdecillion vigintillion".split())})
_NUMBER_SCALES.update({"googol": 10 ** 100, "centillion": 10 ** 303})
_NUMBER_NAMES = sorted([*_SMALL_NUMBERS, *_NUMBER_SCALES, "a", "an", "oh", "point", "half", "quarter"], key=len, reverse=True)
_DIGIT_AMOUNT = r"\d+(?:,\d{2,3})*(?:\.\d+)?"
_NUMBER_ATOM = r"(?:" + _DIGIT_AMOUNT + "|" + "|".join(map(re.escape, _NUMBER_NAMES)) + r")"
_NUMBER_PHRASE = r"(?:(?:minus|negative)[ -]+|-[ \t]*)?" + _NUMBER_ATOM + r"(?:(?:[ \t-]+(?:and[ \t-]+)?)" + _NUMBER_ATOM + r"){0,23}"


def _number_value(phrase):
    """Parse recognized number words exactly, without a digit-length cutoff."""
    with localcontext() as context:
        scale_digits = sum(len(str(_NUMBER_SCALES.get(word, 0))) for word in phrase.lower().replace('-', ' ').split())
        context.prec = max(64, len(phrase) * 4 + scale_digits)
        context.Emax = max(context.Emax, context.prec * 2)
        return _number_value_precise(phrase)


def _number_value_precise(phrase):
    text = phrase.strip().lower().replace(",", "")
    negative = text.startswith(("minus ", "negative ", "-"))
    text = re.sub(r"^(?:minus|negative)\s+|^-", "", text).replace("-", " ")
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return ("-" if negative else "") + text
    if text in ("half a", "half an"):
        text = "half"
    tokens = text.split()
    if not tokens:
        return None
    fraction = Decimal(0)
    if len(tokens) >= 2 and tokens[-2:] in (["a", "half"], ["a", "quarter"]):
        fraction = Decimal(".5" if tokens[-1] == "half" else ".25")
        tokens = tokens[:-2]
        if tokens and tokens[-1] == "and":
            tokens.pop()
    elif tokens == ["half"] or tokens == ["quarter"]:
        return ("-" if negative else "") + ("0.5" if tokens[0] == "half" else "0.25")
    decimals = ""
    if "point" in tokens:
        position = tokens.index("point")
        tail = tokens[position + 1:]
        tokens = tokens[:position]
        if not tail:
            return None
        digits = []
        for word in tail:
            block = _digit_block(word)
            if block is None:
                return None
            digits.append(block)
        decimals = "".join(digits)
    unit_tokens = [word for word in tokens if word != "and"]
    digit_blocks = [_digit_block(word) for word in unit_tokens]
    if unit_tokens and "and" not in tokens and not fraction and all(block is not None for block in digit_blocks):
        result = "".join(digit_blocks)
        return ("-" if negative else "") + result + (("." + decimals) if decimals else "")
    else:
        total, group, previous = Decimal(0), Decimal(0), None
        previous_number, group_seen, last_scale = None, False, None
        for word in tokens:
            if word == "and":
                if previous != "scale":
                    return None
                continue
            if word in ("a", "an"):
                number = 1
            elif word == "oh":
                number = 0
            elif word in _SMALL_NUMBERS:
                number = _SMALL_NUMBERS[word]
            elif re.fullmatch(r"\d+(?:\.\d+)?", word):
                number = Decimal(word)
            else:
                number = None
            if number is not None:
                # Do not turn unrelated successive number words into an amount.
                if previous == "unit" and not (previous_number >= 20 and previous_number % 10 == 0 and 0 < number < 10):
                    return None
                group += number
                previous, previous_number, group_seen = "unit", number, True
            elif word in _NUMBER_SCALES:
                scale = _NUMBER_SCALES[word]
                if scale == 100:
                    group = (group if group_seen else Decimal(1)) * scale
                    group_seen = True
                else:
                    if last_scale is not None and scale >= last_scale:
                        total = (total + group) * scale
                    else:
                        total += (group if group_seen else Decimal(1)) * scale
                    group, group_seen, last_scale = Decimal(0), False, scale
                previous = "scale"
            else:
                return None
        value = total + group
    if fraction:
        value += fraction
    if decimals:
        if value != value.to_integral_value():
            return None
        result = f"{value:.0f}." + decimals
    else:
        result = format(value, "f")
        if "." in result:
            result = result.rstrip("0").rstrip(".")
    return ("-" if negative else "") + (result or "0")


# Plain spoken numbers are independent of currency/symbol commands. Digit
# strings stay strings: leading zeros and arbitrarily long IDs are not rounded.
_DIGIT_WORDS = {word: str(value) for word, value in _SMALL_NUMBERS.items() if value < 10}
_DIGIT_WORDS['oh'] = '0'
_DIGIT_WORD_ALTERNATIVES = '|'.join(sorted(_DIGIT_WORDS, key=len, reverse=True))
_DIGIT_WORD_PART = re.compile(r'[0-9]+|' + _DIGIT_WORD_ALTERNATIVES, re.I)
_DIGIT_WORD_BLOCK = re.compile(r'(?:[0-9]|' + _DIGIT_WORD_ALTERNATIVES + r')+', re.I)
_PLAIN_NUMBER_NAMES = sorted([*_SMALL_NUMBERS, *_NUMBER_SCALES, 'oh', 'point'], key=len, reverse=True)
_NUMBER_ARTICLE = r'(?:a|an)(?=[ \t]+(?:' + '|'.join(map(re.escape, _NUMBER_SCALES)) + r')(?!\w))'
_PLAIN_NUMBER_ATOM = r'(?:' + _DIGIT_AMOUNT + '|' + _NUMBER_ARTICLE + '|' + '|'.join(map(re.escape, _PLAIN_NUMBER_NAMES)) + ')'
_PLAIN_NUMBER_SEPARATOR = r'(?:[ \t]+|(?<=[A-Za-z])-(?=[A-Za-z]))'
_PLAIN_NUMBER_PATTERN = re.compile(
    r'(?<![\w@])(?:(?:minus|negative)[ \t]+)?' + _PLAIN_NUMBER_ATOM +
    r'(?:' + _PLAIN_NUMBER_SEPARATOR + r'(?:and[ \t]+)?' + _PLAIN_NUMBER_ATOM + r')*(?![\w@])', re.I)
_PLAIN_NUMBER_WORD = re.compile(r'(?<!\w)(?:' + '|'.join(map(re.escape, _SMALL_NUMBERS)) + r')(?!\w)', re.I)
_PROTECTED_NUMBER_TEXT = re.compile(
    r'(?:https?://|www\.)[^\s<>]+|(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?!\w)', re.I)


def _digit_block(word):
    if re.fullmatch(r'[0-9]+', word):
        return word
    if _DIGIT_WORD_BLOCK.fullmatch(word):
        return ''.join(match[0] if match[0].isdigit() else _DIGIT_WORDS[match[0].lower()] for match in _DIGIT_WORD_PART.finditer(word))
    return None


def convert_spoken_numbers(text):
    """Convert standalone cardinals and digit sequences without a length cap."""
    def convert_segment(segment):
        def compound(match):
            word = match[0]
            digits = _digit_block(word.lower())
            # Do not change a word containing a number (someone, stone, etc.).
            # A complete compound such as zerozerofive is an explicit digit ID.
            return digits if digits is not None and len(digits) > 1 else word
        segment = re.sub(r'(?<!\w)[A-Za-z0-9]+(?!\w)', compound, segment)

        def number(match):
            phrase = match[0]
            words = phrase.lower().replace('-', ' ').split()
            if not re.search(r'[A-Za-z]', phrase):
                # Already-numeric dates, grouped phone numbers and IDs stay as
                # dictated; mixed spoken/literal digits are handled below.
                return phrase
            if words in (['a'], ['an'], ['oh'], ['point']):
                return phrase
            if words == ['one']:
                before = segment[max(0, match.start() - 80):match.start()].lower()
                after = segment[match.end():match.end() + 40].lower()
                if re.search(r'\b(?:no|any|every|some|the|which|another|this|that)\s+$', before) or re.match(r"(?:\s+(?:of|another)\b|['’]s\b)", after):
                    return phrase  # pronouns, not a numeric quantity
            # Keep a binary "minus" for the formula pass; a standalone signed
            # number ("negative five" / "minus five") becomes -5.
            binary_minus = words[0] == 'minus' and re.search(
                r'(?:\d|\b(?:' + '|'.join([*_SMALL_NUMBERS, *_NUMBER_SCALES]) + r'))\s+$', segment[max(0, match.start() - 80):match.start()], re.I)
            prefix = ''
            candidate = phrase
            if binary_minus:
                prefix, candidate = phrase.split(None, 1)
                prefix += ' '
            value = _number_value(candidate)
            if value is not None:
                return prefix + value
            # Invalid combinations are separate numbers, not a guessed sum.
            # Preserve conjunctions rather than turning "one and two" into 12.
            return _PLAIN_NUMBER_WORD.sub(lambda item: str(_SMALL_NUMBERS[item[0].lower()]), phrase)
        return _PLAIN_NUMBER_PATTERN.sub(number, segment)

    pieces, previous = [], 0
    for match in _PROTECTED_NUMBER_TEXT.finditer(text):
        pieces.append(convert_segment(text[previous:match.start()]))
        pieces.append(match[0])
        previous = match.end()
    pieces.append(convert_segment(text[previous:]))
    return ''.join(pieces)


_CURRENCY_ROWS = [
    (r"(?:u\.?\s?s\.?\s+dollars?|american dollars?|us dollars?|usd|dollars?|bucks)", "$", "cents?"),
    (r"(?:indian rupees?|inr|rupees?|rs\.?)", "₹", r"(?:paise|paisa)"),
    (r"(?:british pounds?|pounds? sterling|gbp|pounds?)", "£", r"(?:pence|penny|pennies)"),
    (r"(?:euros?|eur)", "€", "cents?"),
    (r"(?:japanese yen|yen|jpy|chinese yuan|yuan|rmb|cny)", "¥", "cents?"),
    (r"(?:korean won|won|krw)", "₩", "cents?"),
    (r"(?:rubles?|roubles?|rub)", "₽", "cents?"),
    (r"(?:bitcoins?|btc)", "₿", "cents?"),
    (r"(?:vietnamese dong|dong|vnd)", "₫", "cents?"),
    (r"(?:thai baht|baht|thb)", "฿", "cents?"),
    (r"(?:shekels?|ils)", "₪", "cents?"),
    (r"(?:philippine pesos?|php)", "₱", "cents?"),
    (r"(?:naira|ngn)", "₦", "cents?"),
    (r"(?:turkish lira|lira)", "₺", "cents?"),
    (r"(?:hryvnias?|uah)", "₴", "cents?"),
]
_CURRENCY_NAMES = {symbol: pattern for pattern, symbol, minor in _CURRENCY_ROWS}
_CURRENCY_PATTERN = r"(?:" + "|".join(row[0] for row in _CURRENCY_ROWS) + ")"
_CURRENCY_LOOKUP = [(re.compile(r"^(?:" + name + r")$", re.I), symbol) for name, symbol, minor in _CURRENCY_ROWS]
_CURRENCY_SUFFIX = re.compile(r"(?<![\w.])(?P<number>" + _NUMBER_PHRASE + r")[ \t]+(?P<currency>" + _CURRENCY_PATTERN + r")(?!\w)", re.I)
_CURRENCY_PREFIX = re.compile(r"(?<!\w)(?P<currency>" + _CURRENCY_PATTERN + r")[ \t]+(?P<number>" + _NUMBER_PHRASE + r")(?!\w)", re.I)
_CURRENCY_LITERAL_PREFIX = re.compile(r"(?P<currency>[$€£¥₹₩₽₿₫฿₪₱₦₺₴])[ \t]*(?P<number>" + _NUMBER_PHRASE + r")(?!\w)", re.I)
_CURRENCY_LITERAL_SUFFIX = re.compile(r"(?<![\w.])(?P<number>" + _NUMBER_PHRASE + r")[ \t]+(?P<currency>[$€£¥₹₩₽₿₫฿₪₱₦₺₴])(?!\w)", re.I)
_MINOR_AMOUNTS = re.compile(r"(?<![\w.])(?P<number>" + _NUMBER_PHRASE + r")[ \t]+(?P<currency>" + _CURRENCY_PATTERN + r")[ \t]+(?:and[ \t]+)?(?P<minor>" + _NUMBER_PHRASE + r")[ \t]+(?P<unit>cents?|paise|paisa|pence|penny|pennies)(?!\w)", re.I)
_PERCENT_AMOUNT = re.compile(r"(?<![\w.])(?P<number>" + _NUMBER_PHRASE + r")[ \t]*(?:percent(?:age)?|per[ \t]+cent|%)(?!\w)", re.I)
_CENT_AMOUNT = re.compile(r"(?<![\w.])(?P<number>" + _NUMBER_PHRASE + r")[ \t]+cents?(?!\w)", re.I)
_TEMPERATURE = re.compile(r"(?<![\w.])(?P<number>" + _NUMBER_PHRASE + r")[ \t]+degrees?[ \t]+(?P<unit>celsius|centigrade|fahrenheit)(?!\w)", re.I)
_MATH_OPERATORS = {"plus": "+", "minus": "-", "times": "×", "multiplied by": "×", "divided by": "÷",
                   "equals": "=", "equal to": "=", "is equal to": "=", "less than": "<", "greater than": ">",
                   "less than or equal to": "≤", "greater than or equal to": "≥", "not equal to": "≠", "is greater than": ">", "is less than": "<", "is not equal to": "≠"}
_MATH_OPERATOR = "|".join(re.escape(name) for name in sorted(_MATH_OPERATORS, key=len, reverse=True))
_MATH_EXPRESSION = re.compile(r"(?<!\w)(?P<left>" + _NUMBER_PHRASE + r")[ \t]+(?P<operator>" + _MATH_OPERATOR + r")[ \t]+(?P<right>" + _NUMBER_PHRASE + r")(?!\w)", re.I)
_EMAIL_ATOM = r"[A-Za-z0-9]+"
_EMAIL_LOCAL = _EMAIL_ATOM + r"(?:[ \t]*(?:\.|_|\+|-|dot|underscore|hyphen|dash|plus)[ \t]*" + _EMAIL_ATOM + r")*"
_EMAIL_DOMAIN = r"[A-Za-z0-9-]+(?:[ \t]*(?:\.|dot)[ \t]*[A-Za-z0-9-]+)+"
_EMAIL_SPOKEN = re.compile(r"(?<![\w@])(?P<local>" + _EMAIL_LOCAL + r")[ \t]*(?:@|\bat\b)[ \t]*(?P<domain>" + _EMAIL_DOMAIN + r")(?![\w@])", re.I)


def _currency_symbol(name):
    for pattern, symbol in _CURRENCY_LOOKUP:
        if pattern.fullmatch(name):
            return symbol
    return name


def _format_currency(symbol, amount):
    if amount.startswith("-"):
        return "-" + symbol + amount[1:]
    return symbol + amount


# User mappings are data, not Python or regular expressions. Replacements are
# shielded while built-in number/symbol commands run, so literal text stays literal.
class CustomSymbols:
    MAX_BYTES = 4 * 1024 * 1024

    def __init__(self, path=""):
        self.path, self.error = "", ""
        self._signature, self._pattern, self._values = None, None, {}
        self._compiled = (None, {})
        if path:
            self.configure(path)

    @staticmethod
    def parse(text):
        values = {}
        for line_number, line in enumerate(text.lstrip('\ufeff').splitlines(), 1):
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            phrase, separator, replacement = line.partition('=')
            phrase = re.sub(r'[ \t]+', ' ', phrase.strip()).casefold()
            if not separator or not phrase:
                raise ValueError(f"Custom symbols line {line_number}: use phrase = replacement.")
            replacement = replacement.strip()
            if replacement.startswith('"'):
                try:
                    replacement = json.loads(replacement)
                    if not isinstance(replacement, str):
                        raise ValueError()
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"Custom symbols line {line_number}: invalid quoted text.") from exc
            else:
                replacement = re.sub(r'\\([ntr\\])', lambda item: {'n': '\n', 't': '\t', 'r': '\r', '\\': '\\'}[item[1]], replacement)
            if '\x00' in phrase or '\x00' in replacement:
                raise ValueError(f"Custom symbols line {line_number}: NUL characters are not allowed.")
            if len(phrase) > 256 or (len(values) >= 5000 and phrase not in values):
                raise ValueError('Use at most 5,000 phrases, each at most 256 characters.')
            values[phrase] = replacement.replace('\r\n', '\n').replace('\r', '\n')
        return values

    def configure(self, path):
        path = str(Path(path).expanduser().resolve()) if path else ''
        if not path:
            self.path, self._signature, self._pattern, self._values, self.error = '', None, None, {}, ''
            self._compiled = (None, {})
            return
        source = Path(path)
        if source.suffix.lower() != '.txt':
            raise ValueError('Choose a UTF-8 .txt custom-symbols file.')
        stat = source.stat()
        if stat.st_size > self.MAX_BYTES:
            raise ValueError('The custom-symbols file exceeds the 4 MiB safety limit.')
        with source.open('rb') as stream:
            raw = stream.read(self.MAX_BYTES + 1)
        if len(raw) > self.MAX_BYTES:
            raise ValueError('The custom-symbols file exceeds the 4 MiB safety limit.')
        values = self.parse(raw.decode('utf-8-sig'))
        alternatives = []
        for phrase in sorted(values, key=len, reverse=True):
            escaped = r'[ \t]+'.join(re.escape(word) for word in phrase.split(' '))
            left = r'(?<!\w)' if phrase[0].isalnum() or phrase[0] == '_' else ''
            right = r'(?!\w)' if phrase[-1].isalnum() or phrase[-1] == '_' else ''
            alternatives.append(left + escaped + right)
        pattern = re.compile('|'.join(alternatives)) if alternatives else None
        self.path, self._signature = path, (stat.st_mtime_ns, stat.st_size)
        self._pattern, self._values, self.error = pattern, values, ''
        self._compiled = (pattern, values)

    def refresh(self):
        if self.path:
            try:
                stat = Path(self.path).stat()
                if (stat.st_mtime_ns, stat.st_size) != self._signature:
                    self.configure(self.path)
            except (OSError, ValueError) as exc:
                self.error = 'Custom symbols: ' + str(exc) + ' Last valid mappings remain active.'

    def protect(self, text, reserve):
        self.refresh()
        pattern, values = self._compiled
        if pattern is None:
            return text
        # Unicode casefold can expand one character (ß -> ss). Keep offsets into
        # the original text rather than indexing a differently-sized string.
        folded, positions = [], []
        for index, char in enumerate(text):
            value = char.casefold()
            folded.append(value)
            positions.extend([index] * len(value))
        pieces, cursor = [], 0
        for match in pattern.finditer(''.join(folded)):
            start, end = positions[match.start()], positions[match.end() - 1] + 1
            if (match.start() and positions[match.start() - 1] == start) or (match.end() < len(positions) and positions[match.end()] == end - 1):
                continue
            pieces.extend((text[cursor:start], reserve(values[re.sub(r'[ \t]+', ' ', match[0])])))
            cursor = end
        pieces.append(text[cursor:])
        return ''.join(pieces)


def _prepare_text_commands(text, custom_symbols=None):
    nonce = os.urandom(12).hex()
    protected = {}

    def reserve(value):
        token = '\ue000' + nonce + '_' + str(len(protected)) + '\ue001'
        protected[token] = value
        return token
    text = _PROTECTED_NUMBER_TEXT.sub(lambda match: reserve(match[0]), text)
    if custom_symbols is not None:
        text = custom_symbols.protect(text, reserve)
    # Say "literal enter" when the actual word is intended. Custom mappings can
    # also override enter. No key event is generated by either form.
    text = re.sub(r'(?<!\w)literal[ \t]+enter(?!\w)', lambda _: reserve('enter'), text, flags=re.I)
    text = re.sub(r'[ \t]*(?:,[ \t]*)?(?<![\w/\\@-])enter(?!\w|[./\\@-]\w)(?:[ \t]*[,;:.!?])?[ \t]*',
                  lambda _: reserve('\n'), text, flags=re.I)
    return text, protected


def _restore_text_commands(text, protected):
    for token, value in protected.items():
        text = text.replace(token, value)
    return text


def _model_quantization_name(path):
    match = re.search(r'(?i)(?:^|[-_.])((?:i?q)[2-8](?:_[a-z0-9]+)*|bf16|f16|f32)(?=[-.]|$)', Path(path).name)
    return match[1].upper() if match else 'unknown'


def select_model_file(files, quant):
    files = sorted(name for name in files if name.lower().endswith('.gguf'))
    if not files:
        raise ValueError('This repository has no GGUF files.')
    if not quant:
        # Any means a known low-memory quantization first, not an accidental F32.
        for preferred in ('q4_k_m', 'q4_k', 'q5_k_m', 'q5_0', 'q8_0', 'f16'):
            found = [name for name in files if _model_quantization_name(name).lower() == preferred]
            if found:
                return found[0]
        return files[0]
    choices = ('q4_k_m', 'q4_k') if quant == 'q4_k' else (quant,)
    for choice in choices:
        found = [name for name in files if _model_quantization_name(name).lower() == choice]
        if found:
            return found[0]
    available = ', '.join(sorted({_model_quantization_name(name) for name in files}))
    raise ValueError(f'{quant.upper()} is not available in this repository. Available: {available}. Choose another quantization.')


def normalize_dictation_text(text, custom_symbols=None):
    """Explicit names remain supported; natural symbols require clear context."""
    text, protected = _prepare_text_commands(text, custom_symbols)
    text = convert_spoken_numbers(expand_spoken_symbols(text))

    def money(match):
        amount = _number_value(match['number'])
        if amount is None:
            return match[0]
        name = match['currency']
        if match.re is _CURRENCY_PREFIX and name.lower() in ('pound', 'pounds', 'won'):
            return match[0]  # ambiguous verbs, e.g. "I won four prizes"
        # Pounds can be a unit of weight. Do not change obvious weight phrases.
        if name.lower() in ('pound', 'pounds'):
            before = text[max(0, match.start() - 45):match.start()].lower()
            after = text[match.end():match.end() + 20].lower()
            if re.search(r"\b(?:weighs?|weighed|weight|heavy|lift|lifting)\b", before) or re.match(r"\s+of\b", after):
                return match[0]
        return _format_currency(_currency_symbol(name), amount)

    def minor_money(match):
        symbol = _currency_symbol(match['currency'])
        unit = match['unit'].lower()
        if (symbol == '₹' and unit not in ('paisa', 'paise')) or (symbol == '£' and unit not in ('pence', 'penny', 'pennies')):
            return match[0]
        major, minor = _number_value(match['number']), _number_value(match['minor'])
        if major is None or minor is None:
            return match[0]
        cents = Decimal(minor)
        if cents < 0 or cents >= 100 or cents != cents.to_integral_value():
            return match[0]
        with localcontext() as context:
            context.prec = max(64, len(major) + len(minor) + 8)
            amount = abs(Decimal(major)) + cents / 100
            if major.startswith('-'):
                amount = -amount
            return _format_currency(symbol, f"{amount:.2f}")

    text = _MINOR_AMOUNTS.sub(minor_money, text)
    text = _CURRENCY_SUFFIX.sub(money, text)
    text = _CURRENCY_PREFIX.sub(money, text)
    text = _CURRENCY_LITERAL_PREFIX.sub(money, text)
    text = _CURRENCY_LITERAL_SUFFIX.sub(money, text)
    text = _PERCENT_AMOUNT.sub(lambda match: (_number_value(match['number']) + '%') if _number_value(match['number']) is not None else match[0], text)
    text = _CENT_AMOUNT.sub(lambda match: (_number_value(match['number']) + '¢') if _number_value(match['number']) is not None else match[0], text)
    text = _TEMPERATURE.sub(lambda match: (_number_value(match['number']) + '°' + ('F' if match['unit'].lower() == 'fahrenheit' else 'C')) if _number_value(match['number']) is not None else match[0], text)

    # Only numeric formula contexts, not the word "plus" in normal prose.
    def equation(match):
        if match['left'].lower() in ('a', 'an') or match['right'].lower() in ('a', 'an'):
            return match[0]
        left, right = _number_value(match['left']), _number_value(match['right'])
        if left is None or right is None:
            return match[0]
        return left + ' ' + _MATH_OPERATORS[match['operator'].lower()] + ' ' + right
    for _ in range(3):
        revised = _MATH_EXPRESSION.sub(equation, text)
        if revised == text:
            break
        text = revised

    def email(match):
        local = re.sub(r"[ \t]+(?P<word>dot|underscore|hyphen|dash|plus)[ \t]+|[ \t]*(?P<char>[._+\-])[ \t]*",
                       lambda item: {'dot': '.', 'underscore': '_', 'hyphen': '-', 'dash': '-', 'plus': '+'}[item['word'].lower()] if item['word'] else item['char'],
                       match['local'], flags=re.I)
        domain = re.sub(r"\s*(?:\.|\bdot\b)\s*", '.', match['domain'], flags=re.I)
        if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}", domain):
            return match[0]
        return local + '@' + domain.lower()
    return _restore_text_commands(_EMAIL_SPOKEN.sub(email, text), protected)


# Pre-transcribe a recording in the background, but never insert a preview.
# Eight-second pieces keep the work left after Stop small even for long speech.
ASR_PIECE_SAMPLES = 8 * SR
ASR_OVERLAP_SAMPLES = int(.6 * SR)


def _join_transcript_parts(parts):
    """Join overlapping inference pieces without repeating their shared words."""
    result = ""
    for text, overlaps in parts:
        words = text.strip().split()
        if not words:
            continue
        if result and overlaps:
            previous = result.split()
            norm = lambda word: re.sub(r"[^\w]", "", word).casefold()
            left, right = list(map(norm, previous)), list(map(norm, words))
            cut = 0
            for count in range(min(18, len(left), len(right)), 0, -1):
                if any(right[:count]) and left[-count:] == right[:count]:
                    cut = count
                    break
            if cut:
                words = words[cut:]
                if words and words[0][:1].islower() and result.endswith("."):
                    result = result[:-1]
            elif left and right and len(left[-1]) >= 4 and right[0].startswith(left[-1]):
                # The first inference can end in the middle of a word.
                result = " ".join(previous[:-1])
        if words:
            result = (result + " " + " ".join(words)).strip()
    return result


class _RecordingSession:
    def __init__(self, token, generation):
        self.token, self.generation = token, generation
        self.parts = []                 # written only by the inference worker
        self.cancelled = False
        self.error = None


class _RecordingBuffer:
    """Conservative silence compaction and ordered, overlapping ASR pieces."""
    def __init__(self, engine, token, generation):
        self.engine = engine
        self.session = _RecordingSession(token, generation)
        self.blocks = []
        self.samples = self.fresh_voice = 0
        self.overlaps = False
        self.quiet = self.kept_quiet = 0
        self.pre = collections.deque(maxlen=7)  # retain ~224 ms before an onset

    def _submit(self, audio):
        self.engine._seg_q.put(("stage", self.session.generation, self.session,
                                ([audio], self.overlaps)))

    def _flush(self):
        if self.blocks and self.fresh_voice:
            self._submit(np.concatenate(self.blocks).astype(np.float32, copy=False))
        self.blocks, self.samples, self.fresh_voice = [], 0, 0
        self.overlaps = False

    def push(self, block, rms=None):
        if self.session.cancelled:
            return
        if rms is None:
            rms = float(np.sqrt(np.mean(block * block))) if len(block) else 0.0
        # Much lower than the existing automatic-mode VAD threshold. Retain
        # quiet speech, and compact only very low-energy, sustained silence.
        quiet = rms < .0005 and (not len(block) or float(np.max(np.abs(block))) < .003)
        if quiet:
            self.quiet += len(block)
            self.pre.append(block)
            if not self.blocks:
                return
            keep = min(len(block), max(0, int(.224 * SR) - self.kept_quiet))
            if keep:
                self.blocks.append(block[:keep])
                self.samples += keep
                self.kept_quiet += keep
            # Small utterances stay together; longer ones can be processed at
            # a natural pause without submitting anything to the target app.
            if self.quiet >= int(.55 * SR) and self.samples >= 3 * SR:
                self._flush()
            return
        if not self.blocks:
            self.blocks = list(self.pre)
            self.samples = sum(map(len, self.blocks))
        self.pre.clear()
        self.quiet = self.kept_quiet = 0
        self.blocks.append(block)
        self.samples += len(block)
        self.fresh_voice += len(block)
        if self.samples >= ASR_PIECE_SAMPLES:
            audio = np.concatenate(self.blocks).astype(np.float32, copy=False)
            self._submit(audio[:ASR_PIECE_SAMPLES])
            extra = len(audio) - ASR_PIECE_SAMPLES
            # A little context prevents a hard boundary from losing a word.
            tail = audio[ASR_PIECE_SAMPLES - ASR_OVERLAP_SAMPLES:].copy()
            self.blocks, self.samples = [tail], len(tail)
            self.fresh_voice = extra
            self.overlaps = True

    def finish(self):
        self._flush()
        # Same FIFO as all preview work: this is the ONLY manual job that may
        # emit text, and it always comes after all the session's ASR pieces.
        self.engine._seg_q.put(("finish", self.session.generation, self.session, None))


class _ClipboardBridge(QObject):
    """Prepare one bulk paste on Qt's GUI thread, without changing input focus."""
    requested = pyqtSignal(object)
    _MIME = "application/x-dictate-temporary-paste"

    def __init__(self, application):
        super().__init__(application)
        self.requested.connect(self._prepare, Qt.ConnectionType.QueuedConnection)
        self._token = None
        self._backup = None

    @staticmethod
    def _copy_mime(original):
        if original is None:
            return QMimeData()
        clone, total = QMimeData(), 0
        for format_ in original.formats():
            data = original.data(format_)
            total += data.size()
            if total > 8 * 1024 * 1024:
                return None  # don't retain an unbounded clipboard image/object
            clone.setData(format_, data)
        return clone

    @pyqtSlot(object)
    def _prepare(self, request):
        try:
            if request.get("cancelled"):
                return
            clipboard = QApplication.clipboard()
            original = clipboard.mimeData()
            if original is not None and bytes(original.data(self._MIME)) == self._token:
                backup = self._backup  # consecutive quick pastes share the original
            else:
                backup = self._copy_mime(original)
            token = str(time.monotonic_ns()).encode("ascii")
            data = QMimeData()
            data.setText(request["text"])
            data.setData(self._MIME, token)
            clipboard.setMimeData(data)
            self._token, self._backup = token, backup
            request["ok"] = clipboard.text() == request["text"]
            # The target consumes the paste asynchronously. Restore later, and
            # only while the clipboard still contains our private marker.
            QTimer.singleShot(1200, lambda: self._restore(token))
        except (RuntimeError, AttributeError, OSError):
            request["ok"] = False
        finally:
            request["ready"].set()

    def _restore(self, token):
        try:
            clipboard = QApplication.clipboard()
            current = clipboard.mimeData()
            if (token == self._token and self._backup is not None and current is not None
                    and bytes(current.data(self._MIME)) == token):
                backup, self._backup, self._token = self._backup, None, None
                clipboard.setMimeData(backup)
        except (RuntimeError, AttributeError, OSError):
            pass


# ------------------------------------------------------------------- engine
# Updates use only stable releases from this project's HTTPS GitHub endpoint.
# The release digest AND its checksum manifest must agree; this is integrity
# verification over TLS, not Authenticode or an independent signing authority.
_UPDATE_REPOSITORY = 'Comet-Suite/dictate-desktop'
_UPDATE_API = f'https://api.github.com/repos/{_UPDATE_REPOSITORY}/releases/latest'
_UPDATE_MAX_BYTES = 512 * 1024 * 1024
_UNINSTALL_KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{FCFDEEC6-10A9-41D4-9263-FEFD00F42E66}_is1'


def _release_version(value):
    match = re.fullmatch(r'v?(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})', str(value))
    if not match:
        raise ValueError('Only stable x.y.z release versions are supported.')
    return tuple(map(int, match.groups()))


def _approved_update_url(url, redirects=False):
    from urllib.parse import urlsplit
    parsed = urlsplit(str(url))
    if (parsed.scheme != 'https' or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment):
        raise ValueError('Update download did not use a trusted HTTPS URL.')
    host = (parsed.hostname or '').lower()
    if host == 'github.com' and parsed.path.startswith(f'/{_UPDATE_REPOSITORY}/releases/download/'):
        return str(url)
    if redirects and host in ('release-assets.githubusercontent.com', 'objects.githubusercontent.com',
                              'github-releases.githubusercontent.com'):
        return str(url)
    raise ValueError('Update download is outside the official release repository.')


def _update_open(url):
    import urllib.request
    if url != _UPDATE_API:
        _approved_update_url(url)

    class TrustedRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, response, code, message, headers, new_url):
            _approved_update_url(new_url, redirects=True)
            return super().redirect_request(request, response, code, message, headers, new_url)

    request = urllib.request.Request(url, headers={
        'User-Agent': f'Dictate/{VERSION}', 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'})
    response = urllib.request.build_opener(TrustedRedirect()).open(request, timeout=20)
    final = response.geturl()
    if url == _UPDATE_API:
        if final != _UPDATE_API:
            response.close()
            raise ValueError('The release API redirected unexpectedly.')
    else:
        _approved_update_url(final, redirects=True)
    return response


def _update_json():
    with _update_open(_UPDATE_API) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('The release metadata is too large.')
    result = json.loads(raw.decode('utf-8'))
    if not isinstance(result, dict):
        raise ValueError('Invalid release metadata.')
    return result


def _update_candidate(release, current=VERSION):
    if release.get('draft') or release.get('prerelease'):
        return None
    version_tuple = _release_version(release.get('tag_name', ''))
    if version_tuple <= _release_version(current):
        return None
    version = '.'.join(map(str, version_tuple))
    filename = f'Dictate-Setup-{version}-x64.exe'
    selected = {}
    for name in (filename, 'SHA256SUMS.txt'):
        matches = [item for item in release.get('assets', []) if isinstance(item, dict) and item.get('name') == name]
        if len(matches) != 1:
            raise ValueError(f'The official release must contain exactly one {name}.')
        asset = matches[0]
        expected_url = f'https://github.com/{_UPDATE_REPOSITORY}/releases/download/{release["tag_name"]}/{name}'
        if asset.get('browser_download_url') != expected_url:
            raise ValueError('The release asset has an unexpected download URL.')
        _approved_update_url(expected_url)
        selected[name] = asset
    installer = selected[filename]
    size = installer.get('size')
    if type(size) is not int or not 0 < size <= _UPDATE_MAX_BYTES:
        raise ValueError('The installer size is invalid or exceeds the 512 MiB limit.')
    digest = installer.get('digest', '')
    if not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest):
        raise ValueError('The official release is missing its SHA-256 asset digest.')
    return {'version': version, 'name': filename, 'size': size, 'sha256': digest[7:].lower(),
            'url': installer['browser_download_url'],
            'checksums_url': selected['SHA256SUMS.txt']['browser_download_url']}


def _file_sha256(path):
    import hashlib
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _manifest_checksum(text, filename):
    matches = []
    for line in text.lstrip('\ufeff').splitlines():
        match = re.fullmatch(r'([0-9a-fA-F]{64})[ \t]+\*?([^\r\n]+)', line.strip())
        if match and match[2] == filename:
            matches.append(match[1].lower())
    if len(matches) != 1:
        raise ValueError('The installer is missing or duplicated in SHA256SUMS.txt.')
    return matches[0]


def _download_update(candidate, directory, cancelled=None, progress=None):
    if cancelled is not None and cancelled.is_set():
        raise InterruptedError('Update download cancelled.')
    expected_name = f'Dictate-Setup-{candidate["version"]}-x64.exe'
    if candidate['name'] != expected_name or not re.fullmatch(r'Dictate-Setup-[0-9]+\.[0-9]+\.[0-9]+-x64\.exe', expected_name):
        raise ValueError('Unexpected update installer filename.')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / candidate['name']
    partial = target.with_suffix('.exe.partial')
    with _update_open(candidate['checksums_url']) as response:
        manifest = response.read(128 * 1024 + 1)
    if len(manifest) > 128 * 1024:
        raise ValueError('The checksum manifest is too large.')
    expected = _manifest_checksum(manifest.decode('utf-8-sig'), candidate['name'])
    if expected != candidate['sha256']:
        raise ValueError('GitHub asset digest and checksum manifest disagree. Installation was blocked.')
    if target.is_file() and target.stat().st_size == candidate['size'] and _file_sha256(target) == expected:
        return target
    try:
        with _update_open(candidate['url']) as response, partial.open('wb') as output:
            header = response.headers.get('Content-Length')
            if header is not None and int(header) != candidate['size']:
                raise ValueError('The download length does not match the release asset.')
            downloaded, last_percent = 0, -1
            while True:
                if cancelled is not None and cancelled.is_set():
                    raise InterruptedError('Update download cancelled.')
                block = response.read(1024 * 1024)
                if not block:
                    break
                downloaded += len(block)
                if downloaded > candidate['size']:
                    raise ValueError('The download exceeds the advertised installer size.')
                output.write(block)
                percent = downloaded * 100 // candidate['size']
                if progress and percent != last_percent:
                    last_percent = percent
                    progress(percent)
        if downloaded != candidate['size'] or _file_sha256(partial) != expected:
            raise ValueError('Installer SHA-256/length verification failed. Installation was blocked.')
        partial.replace(target)
        return target
    finally:
        partial.unlink(missing_ok=True)


def _installed_update_directory():
    if os.name != 'nt':
        raise RuntimeError('The built-in installer updater is available on Windows only.')
    import winreg
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, _UNINSTALL_KEY, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                directory, _ = winreg.QueryValueEx(key, 'InstallLocation')
            path = Path(directory).resolve()
            if path.is_dir() and (path / 'Dictate.exe').is_file():
                if any(part.casefold() == 'windowsapps' for part in path.parts):
                    raise RuntimeError('Microsoft Store/WindowsApps installations must be updated through their store.')
                return path
        except FileNotFoundError:
            continue
    if getattr(sys, 'frozen', False):
        path = Path(sys.executable).resolve().parent
        if any(part.casefold() == 'windowsapps' for part in path.parts):
            raise RuntimeError('Microsoft Store/WindowsApps installations must be updated through their store.')
        if (path / 'Dictate.exe').is_file() and any(path.glob('unins*.exe')):
            return path
    raise RuntimeError('No installed Windows copy was found. Install the verified installer manually.')


def _update_install_safe(engine):
    return not (engine.recording or engine.pending or engine.busy or engine.loading or engine.speaking)


def _launch_update_helper(candidate, installer, directory):
    import shutil
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        raise RuntimeError('Automatic installation requires the packaged Windows app. Run the verified installer manually.')
    installer, directory = Path(installer).resolve(), Path(directory).resolve()
    if _release_version(candidate['version']) <= _release_version(VERSION):
        raise ValueError('Same-version installs and downgrades are blocked.')
    if (installer.name != f'Dictate-Setup-{candidate["version"]}-x64.exe'
            or not re.fullmatch('[0-9a-f]{64}', candidate['sha256'])
            or installer.stat().st_size != candidate['size'] or _file_sha256(installer) != candidate['sha256']):
        raise ValueError('The downloaded installer changed. Installation was blocked.')
    if not (directory / 'Dictate.exe').is_file() or any(part.casefold() == 'windowsapps' for part in directory.parts):
        raise ValueError('The existing installation directory is not safe to upgrade.')
    source = Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent)) / 'Dictate-Updater.exe'
    if not source.is_file():
        raise RuntimeError('The installer helper is missing. Reinstall Dictate or run the verified installer manually.')
    launch_dir = APP_DIR / 'updates' / ('launch-' + os.urandom(12).hex())
    launch_dir.mkdir(parents=True, exist_ok=False)
    helper = launch_dir / source.name
    shutil.copy2(source, helper)
    # Native helper is outside the installed tree: no Python/Qt DLL locks survive
    # app exit. It independently verifies SHA-256 and waits for THIS process only.
    return subprocess.Popen([str(helper), str(os.getpid()), str(installer), str(directory), candidate['sha256']],
                            cwd=str(launch_dir), close_fds=True,
                            creationflags=0x00000008 | 0x00000200)


class ReleaseUpdater(QObject):
    status_changed = pyqtSignal(str)
    download_ready = pyqtSignal()

    def __init__(self, directory=None):
        super().__init__()
        self.directory = Path(directory) if directory is not None else APP_DIR / 'updates'
        self.candidate, self.installer = None, None
        self.busy, self.manual, self.status = False, False, 'Checks stable GitHub releases; asks before installing.'
        self.deferred_version = ''
        self._cancel = threading.Event()
        self._closed = False
        self._thread = None

    def _status(self, text):
        self.status = text
        self.status_changed.emit(text)

    def check(self, manual=False):
        if self.busy or self._closed:
            return
        self.busy, self.manual = True, bool(manual)
        self._cancel = threading.Event()
        if manual:
            self.deferred_version = ''
        self._status('Checking official GitHub releases...')
        self._thread = threading.Thread(target=self._check, args=(self._cancel,), daemon=True)
        self._thread.start()

    def _check(self, cancelled):
        try:
            candidate = _update_candidate(_update_json())
            if cancelled.is_set():
                return
            if candidate is None:
                self.candidate, self.installer = None, None
                self._status(f'Dictate {VERSION} is up to date.')
                return
            self._status(f'Downloading {candidate["version"]} automatically...')
            installer = _download_update(candidate, self.directory, cancelled,
                                         lambda n: self._status(f'Downloading {candidate["version"]}: {n}%'))
            if cancelled.is_set():
                return
            self.candidate, self.installer = candidate, installer
            self._status(f'{candidate["version"]} verified and ready. Installation requires your confirmation.')
            self.download_ready.emit()
        except InterruptedError:
            self._status('Update download cancelled.')
        except Exception as exc:
            self._status(f'Update check failed: {exc}')
        finally:
            self.busy = False

    def cancel(self):
        self._cancel.set()

    def shutdown(self):
        self._closed = True
        self.cancel()


_CUSTOM_SYMBOLS_TEMPLATE = '''# Dictate custom symbols: UTF-8, phrase = replacement
# Blank lines and lines beginning with # are ignored. Longest phrase wins.
# Replacements are literal; use \\n for a newline, \\t for a tab, \\\\ for a backslash.
# JSON-quoted replacements preserve leading/trailing spaces.
# Uncomment examples below, save, then click Reload (or just dictate again).
# happy face = :)\n# work email = name@example.com\n# next paragraph = \\n\\n\n# my signature = "Best regards,\\nYour name"\n'''


def _custom_symbols_file(settings):
    configured = settings.get('custom_symbols_path', '')
    if configured:
        return Path(configured)
    path = APP_DIR / 'custom_symbols.txt'
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('x', encoding='utf-8') as output:
            output.write(_CUSTOM_SYMBOLS_TEMPLATE)
    except FileExistsError:
        pass
    settings['custom_symbols_path'] = str(path)
    save_settings(settings)
    return path


class Engine(QObject):
    model_ready = pyqtSignal()
    model_error = pyqtSignal(str)
    message = pyqtSignal(str)
    heard = pyqtSignal(str)
    unloaded = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.model = None
        self.model_info = {}
        self.custom_symbols = CustomSymbols()
        self._custom_error = ""
        self.loading = False
        self._loading_model = None
        self._loading_thread = None
        self._unloading = False
        self.unloaded_idle = False
        self.auto_enter = False
        self.silence_blocks = 8
        self.manual = False          # manual mode: record until the shortcut is pressed again
        self.recording = False
        # Capture commands and audio share one FIFO. A stop cannot overtake audio,
        # and two quick shortcut presses cannot overwrite each other's command.
        self._capture_lock = threading.RLock()
        self._capture_id = 0
        self._model_generation = 0
        self._pending_jobs = 0
        self._shortcut_mode = False
        self.last_used = time.time()
        self.listening = False
        self.paused = False
        self.speaking = False
        self.busy = False
        self.monitor = False            # settings window open: keep mic open for the level meter
        self.level = 0.0
        self.mon_level = 0.0
        self.device_name = ""
        self._native = SR
        self._carry = np.zeros(0, dtype=np.float32)
        self._stream = None
        self._audio_q = queue.Queue()
        self._seg_q = queue.Queue()
        self._lock = threading.Lock()
        self._quit = threading.Event()
        self._kb = None
        self.shortcut = DEFAULTS["dict_hotkey"]
        application = QApplication.instance()
        self._clipboard_bridge = _ClipboardBridge(application) if application is not None else None
        self._segment_thread = threading.Thread(target=self._segmenter, daemon=True)
        self._transcribe_thread = threading.Thread(target=self._transcriber, daemon=True)
        self._segment_thread.start()
        self._transcribe_thread.start()

    @property
    def active(self):
        return self.listening and not self.paused and self.model is not None

    # ---- model
    def _import_engine(self, runtime="crispasr"):
        module = "transcribe_cpp" if runtime == "transcribe_cpp" else "crispasr"
        package = "transcribe-cpp==0.4.1" if runtime == "transcribe_cpp" else "crispasr"

        def get_engine():
            if os.name == "nt":
                if importlib.util.find_spec(module) is None:
                    raise ImportError(module)
                return functools.partial(_WindowsNativeSession, runtime=runtime)
            imported = importlib.import_module(module)
            return _UnifiedSession if runtime == "transcribe_cpp" else imported.Session

        try:
            return get_engine()
        except (ImportError, OSError):
            pass
        if getattr(sys, "frozen", False):
            raise RuntimeError(f'The bundled speech engine "{package}" could not be loaded. Reinstall Dictate.')
        self.message.emit(f"Installing the speech engine ({package})...")
        exe = Path(sys.executable)
        if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
            exe = exe.with_name("python.exe")
        flags = 0x08000000 if os.name == "nt" else 0
        try:
            subprocess.run([str(exe), "-m", "pip", "install", package], capture_output=True,
                           text=True, creationflags=flags, timeout=900)
        except Exception:
            pass
        importlib.invalidate_caches()
        try:
            return get_engine()
        except (ImportError, OSError) as exc:
            raise RuntimeError(f'Could not load "{package}". In a terminal run: '
                               f'"{exe}" -m pip install {package}\n{exc}') from exc

    @staticmethod
    def _run(model, audio):
        """Call the model and always return plain text, whatever the result type is."""
        try:
            res = model.transcribe(audio, sample_rate=SR)
        except TypeError:
            res = model.transcribe(audio)
        if isinstance(res, str):
            return res.strip()
        t = getattr(res, "text", None)
        if isinstance(t, str):
            return t.strip()
        return " ".join(str(getattr(s, "text", s)).strip() for s in res).strip()

    def configure_custom_symbols(self, path):
        self.custom_symbols.configure(path)
        self._custom_error = ""

    def _normalize_text(self, text):
        result = normalize_dictation_text(text, self.custom_symbols)
        if self.custom_symbols.error != self._custom_error:
            self._custom_error = self.custom_symbols.error
            if self._custom_error:
                self.message.emit(self._custom_error)
        return result

    def load_model(self, path):
        if self.loading or self._quit.is_set():
            return
        self.set_listening(False)
        with self._capture_lock:
            self._model_generation += 1
        self.loading = True
        self._loading_thread = threading.Thread(target=self._load, args=(path,), daemon=True)
        self._loading_thread.start()

    @staticmethod
    def _close_model(model, force=False):
        if model is not None:
            try:
                if isinstance(model, _WindowsNativeSession):
                    model.close(force=force)
                else:
                    model.close()
            except Exception:
                pass

    def _clear_audio_queues(self):
        for pending in (self._audio_q, self._seg_q):
            try:
                while True:
                    item = pending.get_nowait()
                    if pending is self._seg_q and isinstance(item[2], _RecordingSession):
                        item[2].cancelled = True
                        item[2].parts.clear()
            except queue.Empty:
                pass
        self._audio_q.put(("cancel", self._capture_id))

    def unload(self):
        """Manual and idle unload share the same no-recording/no-pending guard."""
        with self._capture_lock:
            if (self.recording or self.speaking or self._pending_jobs or self.busy or self.loading
                    or self._unloading or self._quit.is_set()):
                return False
            self._unloading = True
        try:
            self.set_listening(False)
            self.unloaded_idle = True
            with self._capture_lock:
                self._model_generation += 1
                self._carry = np.zeros(0, dtype=np.float32)
            with self._lock:
                old, self.model = self.model, None
                # An idle native worker has no result to preserve. Terminating
                # it releases its entire address space, including allocator caches.
                self._close_model(old, force=True)
            self._clear_audio_queues()
            del old
            gc.collect()
            self.unloaded.emit()
            return True
        finally:
            self._unloading = False

    def _load(self, path):
        model = None
        try:
            if self._quit.is_set():
                return
            p = Path(path)
            if p.is_dir():
                found = sorted(p.glob("*.gguf"))
                if not found:
                    raise FileNotFoundError("No .gguf file found in that folder.")
                if len(found) > 1:
                    raise ValueError("This folder contains several models. Select the exact .gguf file/quantization.")
                p = found[0]
            if p.suffix.lower() != ".gguf" or not p.exists():
                raise FileNotFoundError("Please select a .gguf model file.")
            info = _gguf_info(p)
            Session = self._import_engine(info["runtime"])
            with self._lock:
                old, self.model = self.model, None
                self._close_model(old, force=True)
            del old
            gc.collect()
            if self._quit.is_set():
                return
            threads = max(2, min(8, (os.cpu_count() or 4) - 2))
            kwargs = {"n_threads": threads}
            if isinstance(Session, functools.partial) and Session.func is _WindowsNativeSession:
                kwargs["cancel_event"] = self._quit
            try:
                model = Session(str(p), **kwargs)
            except TypeError:
                model = Session(str(p))
            self._loading_model = model
            if self._quit.is_set():
                return
            try:   # warm-up so the first real phrase is fast
                self._run(model, np.zeros(SR, dtype=np.float32))
            except Exception:
                pass
            with self._lock:
                if self._quit.is_set():
                    return
                self.model = model
                self.model_info = info
                model = None  # ownership transferred to the engine
                self._loading_model = None
            gc.collect()
            self.last_used = time.time()
            self.loading = False
            self.unloaded_idle = False
            self.model_ready.emit()
        except Exception as exc:
            if not self._quit.is_set():
                self.model_error.emit(str(exc))
        finally:
            self._loading_model = None
            self._close_model(model, force=self._quit.is_set())
            self.loading = False

    # ---- microphone
    def set_listening(self, on):
        if on and self.model is None:
            return
        with self._capture_lock:
            self.listening, self.paused = bool(on), False
            if not on:
                self.recording = self.speaking = False
                self._shortcut_mode = False
                self._capture_id += 1
                self._carry = np.zeros(0, dtype=np.float32)
                self._audio_q.put(("cancel", self._capture_id))
        self._sync_stream()

    def toggle_pause(self):
        if self.listening:
            with self._capture_lock:
                self.paused = not self.paused
                # Pausing a manual recording must NOT discard or submit it.
                if not self.recording:
                    self._capture_id += 1
                    self._audio_q.put(("cancel", self._capture_id))
                self.speaking = self.recording and not self.paused
            self._sync_stream()

    def set_manual(self, on):
        with self._capture_lock:
            self.manual = bool(on)
            self._shortcut_mode = False
            self.recording = self.speaking = False
            self._capture_id += 1
            self._carry = np.zeros(0, dtype=np.float32)
            self._audio_q.put(("cancel", self._capture_id))

    @property
    def pending(self):
        with self._capture_lock:
            return self._pending_jobs

    def _job_done(self):
        with self._capture_lock:
            self._pending_jobs = max(0, self._pending_jobs - 1)

    def toggle_record(self):
        """Shortcut = start / COMMIT, regardless of the automatic-pause setting."""
        if self.model is None or self.loading or self._unloading or self._quit.is_set():
            return None
        with self._capture_lock:
            if self._unloading or self._quit.is_set():
                return None
            self._shortcut_mode = True
            if self.recording:
                # Flush even a partial audio block. All callback data already
                # captured is ahead of this commit marker in the SAME queue.
                if self._carry.size:
                    self._audio_q.put(("record", self._capture_id, self._carry.copy()))
                self._carry = np.zeros(0, dtype=np.float32)
                self.recording = self.speaking = False
                self._pending_jobs += 1
                self._audio_q.put(("commit", self._capture_id, self._model_generation))
            else:
                self._capture_id += 1
                self.listening, self.paused = True, False
                self.recording = self.speaking = True
                self._carry = np.zeros(0, dtype=np.float32)
                self._audio_q.put(("start", self._capture_id, self._model_generation))
            recording = self.recording
            self.last_used = time.time()
        self._sync_stream()
        if recording and not self.active:
            return None
        return recording

    def set_monitor(self, on):
        self.monitor = on
        self._sync_stream()

    def set_device(self, name):
        self.device_name = name or ""
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
            self._sync_stream()

    def refresh_devices(self):
        """Re-scan microphones (picks up devices plugged in after start)."""
        try:
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            import sounddevice as sd
            sd._terminate()
            sd._initialize()
        except Exception:
            pass
        self._sync_stream()

    def _resolve_device(self):
        if not self.device_name:
            return None
        try:
            import sounddevice as sd
            try:
                host = sd.query_devices(kind="input")["hostapi"]
            except Exception:
                host = None
            for i, d in enumerate(sd.query_devices()):
                if d["name"] == self.device_name and d["max_input_channels"] > 0 \
                        and (host is None or d["hostapi"] == host):
                    return i
        except Exception:
            pass
        return None

    def _open_stream(self):
        import sounddevice as sd
        dev = self._resolve_device()
        self._carry = np.zeros(0, dtype=np.float32)
        try:
            self._native = SR
            return sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=BLOCK,
                                  device=dev, callback=self._on_audio)
        except Exception:
            # device refuses 16 kHz: open at its own rate and resample ourselves
            info = sd.query_devices(dev, "input")
            self._native = int(info["default_samplerate"])
            return sd.InputStream(samplerate=self._native, channels=1, dtype="float32",
                                  device=dev, callback=self._on_audio)

    def _sync_stream(self):
        want = self.active or self.monitor
        try:
            if want:
                if self._stream is None:
                    self._stream = self._open_stream()
                if not self._stream.active:
                    self._stream.start()
            elif self._stream is not None:
                if self._stream.active:
                    self._stream.stop()
                if not self.listening:
                    self._stream.close()
                    self._stream = None
        except Exception as e:
            stream, self._stream = self._stream, None
            with self._capture_lock:
                self.listening = self.recording = self.speaking = False
                self._capture_id += 1
                self._audio_q.put(("cancel", self._capture_id))
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
            self.message.emit(f"Microphone error: {e}")

    def _on_audio(self, indata, frames, t, status):
        # The lock is held only for copying/queueing, never for opening/stopping
        # PortAudio or for inference. This makes the commit boundary atomic.
        with self._capture_lock:
            x = indata[:, 0].copy()
            if self._native != SR:
                n = max(1, int(len(x) * SR / self._native))
                x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
            self._carry = np.concatenate((self._carry, x))
            while len(self._carry) >= BLOCK:
                blk, self._carry = self._carry[:BLOCK], self._carry[BLOCK:]
                rms = float(np.sqrt(np.mean(blk * blk)))
                if self.active:
                    self.level = self.level * 0.6 + rms * 0.4
                    if self.manual or self._shortcut_mode:
                        if self.recording:
                            self._audio_q.put(("record", self._capture_id, blk, rms))
                            self.last_used = time.time()
                    else:
                        self._audio_q.put(("auto", self._capture_id, blk))
                elif self.monitor:
                    self.mon_level = self.mon_level * 0.6 + rms * 0.4

    # ---- phrase detection (energy based, adaptive noise floor)
    def _segmenter(self):
        pre = collections.deque(maxlen=10)
        buf, speaking, silence, voiced, floor = [], False, 0, 0, 0.002
        manual_buffer, manual_id, auto_id = None, None, None

        def reset_auto():
            nonlocal buf, speaking, silence, voiced
            buf, speaking, silence, voiced = [], False, 0, 0
            pre.clear()
            if not self.recording:
                self.speaking = False

        while not self._quit.is_set():
            try:
                item = self._audio_q.get(timeout=0.2)
            except queue.Empty:
                # A pause or a stopped stream is NOT a manual-session boundary.
                if not self.active:
                    reset_auto()
                continue
            kind, token = item[:2]
            if kind == "start":
                reset_auto()
                if manual_buffer is not None:
                    manual_buffer.session.cancelled = True
                manual_buffer, manual_id = _RecordingBuffer(self, token, item[2]), token
                continue
            if kind == "cancel":
                if manual_buffer is not None:
                    manual_buffer.session.cancelled = True
                manual_buffer, manual_id = None, None
                reset_auto()
                auto_id = None
                continue
            if kind == "record":
                if token == manual_id and manual_buffer is not None:
                    manual_buffer.push(item[2], item[3] if len(item) > 3 else None)
                continue
            if kind == "commit":
                if token == manual_id and manual_buffer is not None:
                    manual_buffer.finish()
                else:
                    self._job_done()
                manual_buffer, manual_id = None, None
                continue
            if kind != "auto":
                continue
            with self._capture_lock:
                usable = (token == self._capture_id and self.active
                          and not (self.manual or self._shortcut_mode))
            if not usable:
                reset_auto()
                continue
            if auto_id != token:
                reset_auto()
                auto_id = token
            blk = item[2]
            rms = float(np.sqrt(np.mean(blk * blk)))
            loud = rms > max(0.006, floor * 3.0)
            if not speaking:
                pre.append(blk)
                if not loud:
                    floor, voiced = min(0.02, floor * 0.98 + rms * 0.02), 0
                else:
                    voiced += 1
                    if voiced >= 2:
                        speaking, buf, silence = True, list(pre), 0
                        self.speaking = True
                        self.last_used = time.time()
            else:
                buf.append(blk)
                silence = 0 if loud else silence + 1
                if silence >= self.silence_blocks or len(buf) >= MAX_BLOCKS:
                    voiced_len = len(buf) - silence
                    keep = len(buf) - max(0, silence - 3)
                    audio = np.concatenate(buf[:keep])
                    reset_auto()
                    if voiced_len >= MIN_BLOCKS:
                        with self._capture_lock:
                            if (token == self._capture_id and self.active
                                    and not (self.manual or self._shortcut_mode)):
                                self._pending_jobs += 1
                                self._seg_q.put(("auto", self._model_generation, token, [audio]))

    @staticmethod
    def _audio_chunks(blocks, samples=25 * SR):
        """Bound native inference to 25 s, without submitting partial messages."""
        pieces, length = [], 0
        for block in blocks:
            offset = 0
            while offset < len(block):
                count = min(samples - length, len(block) - offset)
                pieces.append(block[offset:offset + count])
                length += count
                offset += count
                if length == samples:
                    yield np.concatenate(pieces).astype(np.float32, copy=False)
                    pieces, length = [], 0
        if pieces:
            yield np.concatenate(pieces).astype(np.float32, copy=False)

    # ---- transcribe + type (audio is never written to disk)
    def _transcriber(self):
        while not self._quit.is_set():
            try:
                kind, generation, token, payload = self._seg_q.get(timeout=.3)
            except queue.Empty:
                continue
            session = token if kind in ("stage", "finish") else None
            self.busy = True
            try:
                if (self._quit.is_set() or generation != self._model_generation
                        or (session is not None and session.cancelled)):
                    continue
                if kind == "finish":
                    if session.error is not None:
                        self.message.emit(f"Transcription error: {session.error}")
                        continue
                    text = self._normalize_text(_join_transcript_parts(session.parts))
                else:
                    if session is not None and session.error is not None:
                        continue
                    blocks, overlaps = payload if kind == "stage" else (payload, False)
                    parts = []
                    with self._lock:
                        model = self.model
                        if model is None or generation != self._model_generation:
                            continue
                        for audio in self._audio_chunks(blocks):
                            if (self._quit.is_set() or generation != self._model_generation
                                    or (session is not None and session.cancelled)):
                                break
                            peak = float(np.max(np.abs(audio))) if audio.size else 0.0
                            if not peak:
                                continue
                            if 0.0 < peak < .5:
                                audio = audio * min(8.0, .7 / peak)
                            part = self._run(model, audio.astype(np.float32, copy=False))
                            if part:
                                parts.append(part)
                            del audio
                    text = " ".join(parts).strip()
                    if kind == "stage":
                        if text and not session.cancelled and generation == self._model_generation:
                            session.parts.append((text, overlaps))
                        continue  # NEVER type or emit a preview
                    text = self._normalize_text(text)
                with self._capture_lock:
                    valid = (not self._quit.is_set() and generation == self._model_generation
                             and (kind == "finish" or
                                  (token == self._capture_id and not (self.manual or self._shortcut_mode))))
                if text and valid:
                    self.heard.emit(text)
                    self._type(text + " ", generation)  # one insertion, optional one Enter
            except Exception as exc:
                if session is not None and kind == "stage":
                    session.error = str(exc) or type(exc).__name__  # report once at commit, never paste a partial message
                else:
                    self.message.emit(f"Transcription error: {exc}")
            finally:
                self.busy = False
                self.last_used = time.time()
                if kind != "stage":
                    self._job_done()
                if kind == "finish" and session is not None:
                    session.parts.clear()
                blocks = audio = model = parts = None  # do not pin the last NumPy chunk/model while idle
                del payload

    def _type(self, text, generation=None):
        def valid():
            return not self._quit.is_set() and (generation is None or generation == self._model_generation)
        try:
            # Do not inject Ctrl+V/Enter while the user's shortcut modifiers are held.
            if os.name == "nt":
                keys = (0x10, 0x11, 0x12, 0x5B, 0x5C)
                while any(ctypes.windll.user32.GetAsyncKeyState(key) & 0x8000 for key in keys):
                    if self._quit.wait(.01) or not valid():
                        return
            if not valid():
                return
            if self._kb is None:
                from pynput.keyboard import Controller
                self._kb = Controller()
            pasted = False
            if self._clipboard_bridge is not None:
                request = {"text": text, "ready": threading.Event(), "ok": False, "cancelled": False}
                bridge = self._clipboard_bridge
                if QThread.currentThread() == bridge.thread():
                    bridge._prepare(request)
                else:
                    bridge.requested.emit(request)
                deadline = time.monotonic() + .8
                while not request["ready"].wait(.01) and valid() and time.monotonic() < deadline:
                    pass
                if not request["ready"].is_set() or not valid():
                    request["cancelled"] = True
                elif request["ok"]:
                    from pynput.keyboard import Key
                    modifier = Key.cmd if sys.platform == "darwin" else Key.ctrl
                    key = "v"
                    if self.shortcut.replace(" ", "").lower() == "ctrl+v":
                        modifier, key = Key.shift, Key.insert
                    if not valid():
                        return
                    self._kb.press(modifier)
                    try:
                        self._kb.press(key)
                        self._kb.release(key)
                    finally:
                        self._kb.release(modifier)
                    pasted = True
            if not valid():
                return
            if not pasted:
                # Accessibility/clipboard-restricted targets still have a fallback.
                if "\n" in text or "\r" in text:
                    raise RuntimeError("Multiline dictation needs clipboard access; no Enter key was sent.")
                self._kb.type(text)
            if self.auto_enter and valid():
                # Let asynchronous paste handlers finish before sending once.
                if pasted and self._quit.wait(.08):
                    return
                if valid():
                    from pynput.keyboard import Key
                    self._kb.press(Key.enter)
                    self._kb.release(Key.enter)
        except Exception as exc:
            self.message.emit(f"Typing error: {exc}  (Python: {sys.executable})")

    def shutdown(self):
        if self._quit.is_set():
            return
        self._quit.set()
        with self._capture_lock:
            self._model_generation += 1
            self.listening = self.recording = self.speaking = self.monitor = False
            self._carry = np.zeros(0, dtype=np.float32)
        stream, self._stream = self._stream, None
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass
        model, self.model = self.model, None
        loading = self._loading_model
        # Windows model workers can be stopped even while native inference is
        # blocked. Do not race close() against an in-process native call elsewhere.
        for item in (model, loading):
            if isinstance(item, _WindowsNativeSession):
                self._close_model(item, force=True)
        for worker in (self._segment_thread, self._transcribe_thread, self._loading_thread):
            if worker is not None and worker is not threading.current_thread():
                worker.join(.6)
        def finish_close():
            with self._lock:
                if model is not None and not isinstance(model, _WindowsNativeSession):
                    self._close_model(model)
                # The loader owns an in-process warmup model until publication;
                # its finally block closes it after warmup, never concurrently.
                self._clear_audio_queues()
        if self._transcribe_thread.is_alive():
            threading.Thread(target=finish_close, daemon=True).start()
        else:
            finish_close()
        gc.collect()


# -------------------------------------------------------------------- toast
class Toast(_NoActivatePopup):
    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint |
                         Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus |
                         Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.label = QLabel(self)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(self.label)
        self.fade = QPropertyAnimation(self, b"windowOpacity", self)
        self.fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.fade.finished.connect(self._after_fade)
        self.timer = QTimer(self, singleShot=True)
        self.timer.timeout.connect(self._fade_out)
        self._hiding = False

    def show_text(self, text, anchor, dark, ms=2800):
        bg, fg = ("#26262b", "#ffffff") if dark else ("#ffffff", "#111111")
        self.label.setStyleSheet(f"background:{bg};color:{fg};border:1px solid rgba(128,128,128,90);"
                                 f"border-radius:12px;padding:8px 16px;font:10pt 'Segoe UI';")
        self.label.setText(text)
        self.adjustSize()
        g = anchor.frameGeometry()
        screen = (anchor.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        x = max(screen.left(), min(g.center().x() - self.width() // 2, screen.right() - self.width()))
        y = g.top() - self.height() - 2
        if y < screen.top():
            y = g.bottom() + 2
        self.move(x, y)
        self._hiding = False
        self.fade.stop()
        if not self.isVisible():
            self.setWindowOpacity(0.0)
            self.show()
        raise_topmost(self)
        self.fade.setDuration(180)
        self.fade.setStartValue(self.windowOpacity())
        self.fade.setEndValue(1.0)
        self.fade.start()
        self.timer.start(ms)

    def _fade_out(self):
        self._hiding = True
        self.fade.stop()
        self.fade.setDuration(300)
        self.fade.setStartValue(self.windowOpacity())
        self.fade.setEndValue(0.0)
        self.fade.start()

    def _after_fade(self):
        if self._hiding:
            self.hide()


# ------------------------------------------------------------------- popup
class Pill(_NoActivatePopup):
    PW, PH, M = 232, 60, 10            # pill size and transparent margin (room for the shadow)
    settings_requested = pyqtSignal()
    quit_requested = pyqtSignal()
    dragged = pyqtSignal()
    notify = pyqtSignal(str)
    reload_requested = pyqtSignal()

    def __init__(self, engine, settings):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint |
                         Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.k = SIZES.get(settings.get("size", "medium"), 1.0)
        self.setFixedSize(int((self.PW + 2 * self.M) * self.k), int((self.PH + 2 * self.M) * self.k))
        self.engine, self.settings = engine, settings
        self._press, self._moved = None, False
        self.blend = 0.0                       # 0 = mic, 1 = speaking bars
        self.bars = [6.0] * 5
        self.hov = [0.0] * 4
        self.hov_t = [0.0] * 4
        self.xh, self.inside, self.user_hidden = 0.0, False, False
        self._sig = None
        self.glide = QPropertyAnimation(self, b"pos", self)
        self.glide.setDuration(380)
        self.glide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.fade = QPropertyAnimation(self, b"windowOpacity", self)
        self.fade.setDuration(350)
        self.fade.finished.connect(self._fade_done)
        self.timer = QTimer(self, interval=16)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._t = 0.0
        self.guard = QTimer(self, interval=600)
        self.guard.timeout.connect(self.keep_visible)
        self.guard.start()

    def set_scale(self, k):
        self.k = k
        self.setFixedSize(int((self.PW + 2 * self.M) * k), int((self.PH + 2 * self.M) * k))
        self.update()

    def visible_bounds(self):
        screen = self.screen() or QGuiApplication.primaryScreen()
        g = screen.availableGeometry()
        m = int(self.M * self.k)
        return g.left() - m, g.top() - m, g.right() - self.width() + m + 1, g.bottom() - self.height() + m + 1

    def clamped(self, pt):
        l, t, r, b = self.visible_bounds()
        return QPoint(max(l, min(pt.x(), r)), max(t, min(pt.y(), b)))

    def keep_visible(self):
        if self.user_hidden:
            return
        if not self.isVisible():
            self.show()
        raise_topmost(self)
        if self._press is None and self.glide.state() != QPropertyAnimation.State.Running:
            fixed = self.clamped(self.pos())
            if fixed != self.pos():
                self.glide_to(fixed)

    def hide_popup(self):
        """Close button: hide the pop-up, keep Dictate running in the background."""
        self.user_hidden = True
        self.fade.stop()
        self.fade.setStartValue(self.windowOpacity())
        self.fade.setEndValue(0.0)
        self.fade.start()

    def show_popup(self):
        self.move(self.clamped(self.pos()))
        self.fade_in()
        raise_topmost(self)

    def _fade_done(self):
        if self.user_hidden:
            self.hide()
            self.setWindowOpacity(1.0)

    def fade_in(self):
        self.user_hidden = False
        self.fade.stop()
        self.setWindowOpacity(0.0)
        self.show()
        self.fade.setStartValue(0.0)
        self.fade.setEndValue(1.0)
        self.fade.start()

    def glide_to(self, pt: QPoint):
        self.glide.stop()
        self.glide.setStartValue(self.pos())
        self.glide.setEndValue(pt)
        self.glide.start()

    def _centers(self):
        """pause, mic, settings, close - the close button sits fully inside the rounded end."""
        cy = self.M + self.PH / 2
        return (QPointF(self.M + 38, cy), QPointF(self.M + 90, cy),
                QPointF(self.M + 142, cy), QPointF(self.M + 194, cy))

    def _tick(self):
        eng = self.engine
        self._t += 0.016
        speaking = eng.speaking and eng.active
        old = self.blend
        self.blend += ((1.0 if speaking else 0.0) - self.blend) * 0.22
        if abs(self.blend - (1.0 if speaking else 0.0)) < 0.01:
            self.blend = 1.0 if speaking else 0.0
        dirty = self.blend != old
        if self.blend > 0:
            amp = min(1.0, eng.level * 22)
            for i in range(5):
                tgt = 7 + amp * 24 * (0.4 + 0.6 * abs(math.sin(self._t * 9 + i * 1.4)))
                self.bars[i] += (tgt - self.bars[i]) * 0.35
            dirty = True
        tx = 1.0 if self.inside else 0.0
        if abs(tx - self.xh) > 0.01:
            self.xh += (tx - self.xh) * 0.3
            dirty = True
        for i in range(4):
            d = self.hov_t[i] - self.hov[i]
            if abs(d) > 0.005:
                self.hov[i] += d * 0.3
                dirty = True
        sig = (eng.listening, eng.paused, eng.active, self.settings["dark_mode"])
        if sig != self._sig:
            self._sig = sig
            dirty = True
        if dirty:
            self.update()

    def paintEvent(self, e):
        dark = self.settings["dark_mode"]
        bg = QColor("#1f1f24") if dark else QColor("#ffffff")
        fg = QColor("#f2f2f2") if dark else QColor("#111111")
        dim = QColor("#777777")
        eng = self.engine
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(self.k, self.k)
        M, PW, PH = self.M, self.PW, self.PH

        # soft shadow
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(6):
            p.setBrush(QColor(0, 0, 0, 16))
            p.drawRoundedRect(QRectF(M - i + 1, M - i + 4, PW + 2 * i - 2, PH + 2 * i - 2), PH / 2 + i, PH / 2 + i)
        p.setPen(QPen(QColor(128, 128, 128, 80), 1))
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(M, M, PW, PH), PH / 2, PH / 2)
        pc, mc, gc_, cc = self._centers()
        p.setPen(Qt.PenStyle.NoPen)

        # hover halos
        for c, h in zip((pc, mc, gc_), self.hov):
            if h > 0.01:
                p.setBrush(QColor(128, 128, 128, int(55 * h)))
                p.drawEllipse(c, 19, 19)

        # pause / play
        p.setBrush(fg if eng.listening else dim)
        p.drawEllipse(pc, 12, 12)
        p.setBrush(bg)
        if eng.paused:
            p.drawPolygon(QPolygonF([QPointF(pc.x() - 3, pc.y() - 5), QPointF(pc.x() - 3, pc.y() + 5),
                                     QPointF(pc.x() + 5, pc.y())]))
        else:
            p.drawRoundedRect(QRectF(pc.x() - 4.5, pc.y() - 5, 3, 10), 1, 1)
            p.drawRoundedRect(QRectF(pc.x() + 1.5, pc.y() - 5, 3, 10), 1, 1)

        # centre: mic <-> purple bars (cross-fade)
        if self.blend < 0.99:
            p.setOpacity(1.0 - self.blend)
            col = fg if eng.active else dim
            p.setBrush(col)
            p.drawRoundedRect(QRectF(mc.x() - 4.5, mc.y() - 13, 9, 15), 4.5, 4.5)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(col, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(QRectF(mc.x() - 8.5, mc.y() - 6, 17, 14), 180 * 16, 180 * 16)
            p.drawLine(QPointF(mc.x(), mc.y() + 8), QPointF(mc.x(), mc.y() + 12))
            p.drawLine(QPointF(mc.x() - 5, mc.y() + 12), QPointF(mc.x() + 5, mc.y() + 12))
            p.setPen(Qt.PenStyle.NoPen)
        if self.blend > 0.01:
            p.setOpacity(self.blend)
            p.setBrush(PURPLE)
            for i, h in enumerate(self.bars):
                p.drawRoundedRect(QRectF(mc.x() - 27 + i * 12, mc.y() - h / 2, 6, h), 3, 3)
        p.setOpacity(1.0)

        # settings gear
        p.save()
        p.translate(gc_)
        p.setBrush(fg)
        for i in range(8):
            p.save()
            p.rotate(i * 45)
            p.drawRoundedRect(QRectF(-2.2, -12, 4.4, 6), 1, 1)
            p.restore()
        p.drawEllipse(QPointF(0, 0), 8, 8)
        p.setBrush(bg)
        p.drawEllipse(QPointF(0, 0), 3.2, 3.2)
        p.restore()

        # close button: a proper round badge, fully inside the pill
        hot = self.hov[3] > 0.5
        p.setOpacity(0.55 + 0.45 * self.xh)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#ff453a") if hot else QColor(128, 128, 140, 70))
        p.drawEllipse(cc, 11, 11)
        p.setPen(QPen(QColor("white") if hot else (fg if dark else QColor("#555555")), 1.8,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        d = 3.6
        p.drawLine(QPointF(cc.x() - d, cc.y() - d), QPointF(cc.x() + d, cc.y() + d))
        p.drawLine(QPointF(cc.x() - d, cc.y() + d), QPointF(cc.x() + d, cc.y() - d))
        p.setOpacity(1.0)

    def _hit(self, pos):
        pos = QPointF(pos.x() / self.k, pos.y() / self.k)
        for i, (c, r) in enumerate(zip(self._centers(), (20, 24, 20, 15))):
            if (pos.x() - c.x()) ** 2 + (pos.y() - c.y()) ** 2 <= r * r:
                return i
        return -1

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.glide.stop()
            self._press = e.globalPosition().toPoint()
            self._origin = self.pos()
            self._moved = False

    def mouseMoveEvent(self, e):
        if self._press is not None:
            d = e.globalPosition().toPoint() - self._press
            if self._moved or d.manhattanLength() > 5:
                self._moved = True
                self.move(self._origin + d)
            return
        h = self._hit(e.position())
        self.inside = True
        self.hov_t = [1.0 if i == h else 0.0 for i in range(4)]
        self.setCursor(Qt.CursorShape.PointingHandCursor if h >= 0 else Qt.CursorShape.ArrowCursor)

    def enterEvent(self, e):
        self.inside = True

    def leaveEvent(self, e):
        self.inside = False
        self.hov_t = [0.0] * 4

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or self._press is None:
            return
        self._press = None
        if self._moved:
            self.glide_to(self.clamped(self.pos()))
            self.dragged.emit()
            return
        h = self._hit(e.position())
        eng = self.engine
        if h == 0:
            if eng.listening:
                eng.toggle_pause()
                self.notify.emit("Paused" if eng.paused else "Listening - speak now")
            else:
                self.notify.emit("Microphone is off - click the mic to start")
        elif h == 1:
            if eng.model is None:
                mp = self.settings["model_path"]
                if mp and Path(mp).exists():
                    self.reload_requested.emit()
                else:
                    self.notify.emit("Load a model first")
                    self.settings_requested.emit()
            else:
                eng.set_listening(not eng.listening)
                if eng.listening:
                    self.notify.emit("Listening - speak now")
                else:
                    self.notify.emit("Microphone off")
        elif h == 2:
            self.settings_requested.emit()
        elif h == 3:
            self.hide_popup()

    def contextMenuEvent(self, e):
        m = QMenu(self)
        a1, a2 = QAction("Settings", m), QAction("Quit Dictate", m)
        a1.triggered.connect(self.settings_requested.emit)
        a2.triggered.connect(self.quit_requested.emit)
        m.addAction(a1)
        m.addAction(a2)
        m.exec(e.globalPos())


# ------------------------------------------------------------- global shortcut
_SPECIAL_KEYS = {"space": "<space>", "tab": "<tab>", "return": "<enter>", "enter": "<enter>",
                 "esc": "<esc>", "escape": "<esc>", "backspace": "<backspace>", "del": "<delete>",
                 "delete": "<delete>", "ins": "<insert>", "insert": "<insert>", "home": "<home>",
                 "end": "<end>", "pgup": "<page_up>", "pgdown": "<page_down>", "up": "<up>",
                 "down": "<down>", "left": "<left>", "right": "<right>"}

_VK_SPECIAL = {"space": 0x20, "tab": 0x09, "return": 0x0D, "enter": 0x0D, "esc": 0x1B,
               "escape": 0x1B, "backspace": 0x08, "del": 0x2E, "delete": 0x2E, "ins": 0x2D,
               "insert": 0x2D, "home": 0x24, "end": 0x23, "pgup": 0x21, "pgdown": 0x22,
               "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27}


def to_pynput(seq):
    """'Ctrl+Alt+D' -> '<ctrl>+<alt>+d'. Returns None if it isn't a usable global shortcut."""
    mods, key, is_f = [], None, False
    for part in [x.strip() for x in seq.split("+") if x.strip()]:
        low = part.lower()
        if low in ("ctrl", "alt", "shift"):
            mods.append(f"<{low}>")
        elif low == "meta":
            mods.append("<cmd>")
        elif low.startswith("f") and low[1:].isdigit():
            key, is_f = f"<{low}>", True
        elif low in _SPECIAL_KEYS:
            key = _SPECIAL_KEYS[low]
        elif len(part) == 1:
            key = low
        else:
            return None
    if key is None or (not mods and not is_f):
        return None
    return "+".join(mods + [key])


def parse_win_hotkey(seq):
    """'Ctrl+Alt+D' -> (modifier flags, virtual key) for the Win32 RegisterHotKey call."""
    mods, vk = 0, None
    for part in [x.strip() for x in seq.split("+") if x.strip()]:
        low = part.lower()
        if low == "alt":
            mods |= 0x1
        elif low == "ctrl":
            mods |= 0x2
        elif low == "shift":
            mods |= 0x4
        elif low == "meta":
            mods |= 0x8
        elif low.startswith("f") and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
            vk = 0x6F + int(low[1:])
        elif low in _VK_SPECIAL:
            vk = _VK_SPECIAL[low]
        elif len(part) == 1:
            r = ctypes.windll.user32.VkKeyScanW(ord(part)) & 0xFF
            if r == 0xFF:
                return None
            vk = r
        else:
            return None
    return None if vk is None else (mods, vk)


class Hotkeys(QObject):
    """Windows: native RegisterHotKey (reliable). Other systems: pynput fallback."""
    triggered = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._listener = None
        self._tid = None

    def set(self, seq):
        self.stop()
        if not seq:
            return False
        return self._set_win(seq) if os.name == "nt" else self._set_pynput(seq)

    def _set_win(self, seq):
        parsed = parse_win_hotkey(seq)
        if not parsed:
            return False
        mods, vk = parsed
        from ctypes import wintypes
        ready, result = threading.Event(), {"ok": False, "tid": 0}

        def run():
            u, k = ctypes.windll.user32, ctypes.windll.kernel32
            msg = wintypes.MSG()
            u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)       # create this thread's queue
            result["tid"] = k.GetCurrentThreadId()
            result["ok"] = bool(u.RegisterHotKey(None, 1, mods | 0x4000, vk))   # | NOREPEAT
            ready.set()
            if not result["ok"]:
                return
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312:                           # WM_HOTKEY
                    self.triggered.emit()
            u.UnregisterHotKey(None, 1)

        threading.Thread(target=run, daemon=True).start()
        ready.wait(2.0)
        if result["ok"]:
            self._tid = result["tid"]
        return result["ok"]

    def _set_pynput(self, seq):
        combo = to_pynput(seq)
        if not combo:
            return False
        try:
            from pynput import keyboard
            self._listener = keyboard.GlobalHotKeys({combo: self.triggered.emit})
            self._listener.daemon = True
            self._listener.start()
            return True
        except Exception:
            return False

    def stop(self):
        try:
            if self._tid:
                ctypes.windll.user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)   # WM_QUIT
        except Exception:
            pass
        self._tid = None
        try:
            if self._listener is not None:
                self._listener.stop()
        except Exception:
            pass
        self._listener = None


# ------------------------------------------------------------------ settings UI
ACCENT = "#c93ff2"


def palette(dark):
    if dark:
        return dict(bg="#16161a", card="#202026", text="#f3f3f5", sub="#8d8d98", border="#2d2d35",
                    input="#2a2a32", hover="#3a3a45")
    return dict(bg="#f3f3f7", card="#ffffff", text="#15151a", sub="#6d6d78", border="#e2e2ea",
                input="#f0f0f5", hover="#d8d8e2")


class Switch(QAbstractButton):
    """Animated iOS-style toggle."""

    def __init__(self, checked=False):
        super().__init__()
        self.setCheckable(True)
        self.setFixedSize(46, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._k = 1.0 if checked else 0.0
        self.setChecked(checked)
        self.anim = QPropertyAnimation(self, b"knob", self)
        self.anim.setDuration(170)
        self.anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._go)

    def _go(self, on):
        self.anim.stop()
        self.anim.setStartValue(self._k)
        self.anim.setEndValue(1.0 if on else 0.0)
        self.anim.start()

    def set_state(self, on):
        self.blockSignals(True)
        self.setChecked(on)
        self.blockSignals(False)
        self._k = 1.0 if on else 0.0
        self.update()

    def get_knob(self):
        return self._k

    def set_knob(self, v):
        self._k = v
        self.update()

    knob = pyqtProperty(float, get_knob, set_knob)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        k, on = self._k, QColor(ACCENT)
        col = QColor(int(128 + (on.red() - 128) * k), int(128 + (on.green() - 128) * k),
                     int(140 + (on.blue() - 140) * k), int(120 + 135 * k))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(0, 0, 46, 26), 13, 13)
        p.setBrush(QColor("white"))
        p.drawEllipse(QRectF(3 + 20 * k, 3, 20, 20))


class CloseButton(QAbstractButton):
    """Round close button with a hand-drawn, centred cross."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(32, 32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._h = False

    def enterEvent(self, e):
        self._h = True
        self.update()

    def leaveEvent(self, e):
        self._h = False
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#ff453a") if self._h else QColor(128, 128, 140, 60))
        p.drawEllipse(QRectF(1, 1, 30, 30))
        p.setPen(QPen(QColor("white") if self._h else QColor(140, 140, 150), 2,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        c, d = 16.0, 5.0
        p.drawLine(QPointF(c - d, c - d), QPointF(c + d, c + d))
        p.drawLine(QPointF(c - d, c + d), QPointF(c + d, c - d))


class TitleBar(QWidget):
    """Drag the frameless settings window by its title bar."""

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            h = self.window().windowHandle()
            if h is not None:
                h.startSystemMove()


class PositionPicker(QWidget):
    """Mini screen: click a spot to dock the pop-up there."""
    chosen = pyqtSignal(str)
    KEYS = [["top_left", "top_center", "top_right"], ["middle_left", "middle_center", "middle_right"],
            ["bottom_left", "bottom_center", "bottom_right"]]

    def __init__(self, current):
        super().__init__()
        self.setFixedSize(252, 148)
        self.setMouseTracking(True)
        self.current, self.hover, self.colors = current, None, palette(True)

    def set_theme(self, dark):
        self.colors = palette(dark)
        self.update()

    def set_current(self, key):
        self.current = key
        self.update()

    def spots(self):
        scr = QRectF(6, 6, 240, 136)
        pw, ph, m = 52, 18, 14
        xs = [scr.left() + m, scr.center().x() - pw / 2, scr.right() - m - pw]
        ys = [scr.top() + m, scr.center().y() - ph / 2, scr.bottom() - m - ph]
        return {self.KEYS[r][c]: QRectF(xs[c], ys[r], pw, ph) for r in range(3) for c in range(3)}

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = self.colors
        p.setPen(QPen(QColor(c["border"]), 1.5))
        p.setBrush(QColor(c["input"]))
        p.drawRoundedRect(QRectF(6, 6, 240, 136), 12, 12)
        p.setPen(Qt.PenStyle.NoPen)
        for key, r in self.spots().items():
            if key == self.current:
                p.setBrush(QColor(ACCENT))
            elif key == self.hover:
                p.setBrush(QColor(128, 128, 140, 150))
            else:
                p.setBrush(QColor(128, 128, 140, 70))
            p.drawRoundedRect(r, 9, 9)

    def _at(self, pos):
        for key, r in self.spots().items():
            if r.adjusted(-8, -8, 8, 8).contains(pos):
                return key
        return None

    def mouseMoveEvent(self, e):
        k = self._at(e.position())
        if k != self.hover:
            self.hover = k
            self.setCursor(Qt.CursorShape.PointingHandCursor if k else Qt.CursorShape.ArrowCursor)
            self.update()

    def leaveEvent(self, e):
        self.hover = None
        self.update()

    def mousePressEvent(self, e):
        k = self._at(e.position())
        if k and e.button() == Qt.MouseButton.LeftButton:
            self.current = k
            self.update()
            self.chosen.emit(k)


def _text_block(title, sub=None):
    box = QVBoxLayout()
    box.setSpacing(2)
    t = QLabel(title)
    t.setObjectName("ttl")
    box.addWidget(t)
    if sub:
        s = QLabel(sub)
        s.setObjectName("sub")
        s.setWordWrap(True)
        box.addWidget(s)
    return box


def _card():
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(18, 16, 18, 16)
    lay.setSpacing(12)
    return f, lay


def _short(text, n=34):
    return text if len(text) <= n else text[:n - 1] + "\u2026"


class SettingsDialog(QDialog):
    download_done = pyqtSignal(str)
    download_failed = pyqtSignal(str)
    theme_changed = pyqtSignal()
    model_chosen = pyqtSignal(str)
    position_chosen = pyqtSignal(str)
    hotkey_changed = pyqtSignal(str)
    size_changed = pyqtSignal(str)
    dict_hotkey_changed = pyqtSignal(str)
    stop_mode_changed = pyqtSignal(str)
    update_check_requested = pyqtSignal()
    updates_changed = pyqtSignal(bool)

    def __init__(self, settings, engine):
        super().__init__(None, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)   # rounded window corners
        self.settings, self.engine = settings, engine
        self.setWindowTitle(f"{APP_NAME} - Settings")
        self.resize(1000, 720)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        win = QFrame()
        win.setObjectName("win")
        outer.addWidget(win)
        root = QVBoxLayout(win)
        root.setContentsMargins(22, 16, 22, 22)
        root.setSpacing(14)

        # --- title bar
        bar = TitleBar()
        bar.setObjectName("bar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(0, 0, 0, 0)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        h1 = QLabel("Settings")
        h1.setObjectName("h1")
        ver = QLabel(f"{APP_NAME} {VERSION}  -  offline dictation for any app")
        ver.setObjectName("sub")
        titles.addWidget(h1)
        titles.addWidget(ver)
        bl.addLayout(titles)
        bl.addStretch()
        btn_close = CloseButton()
        btn_close.clicked.connect(self.close)
        bl.addWidget(btn_close, 0, Qt.AlignmentFlag.AlignTop)
        root.addWidget(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setObjectName("inner")
        cols = QHBoxLayout(inner)
        cols.setContentsMargins(0, 0, 8, 0)
        cols.setSpacing(14)
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)
        c1, c2, c3 = QVBoxLayout(), QVBoxLayout(), QVBoxLayout()
        for c in (c1, c2, c3):
            c.setSpacing(14)
            cols.addLayout(c, 1)

        # ================= column 1: general, shortcut, position
        card, cl = _card()
        row = QHBoxLayout()
        row.addLayout(_text_block("Dark mode", "Dark look for the pop-up and settings"))
        row.addStretch()
        self.dark = Switch(settings["dark_mode"])
        self.dark.toggled.connect(self._dark)
        row.addWidget(self.dark)
        cl.addLayout(row)
        row = QHBoxLayout()
        row.addLayout(_text_block("Start on startup", "Launch when you sign in to Windows"))
        row.addStretch()
        self.startup = Switch(settings["start_on_startup"])
        self.startup.toggled.connect(self._startup)
        row.addWidget(self.startup)
        cl.addLayout(row)
        row = QHBoxLayout()
        row.addLayout(_text_block("Auto Enter", "Press Enter after each phrase is typed"))
        row.addStretch()
        self.auto_enter = Switch(settings.get("auto_enter", False))
        self.auto_enter.toggled.connect(self._auto_enter)
        row.addWidget(self.auto_enter)
        cl.addLayout(row)
        c1.addWidget(card)

        card, cl = _card()
        cl.addLayout(_text_block("Show / hide shortcut",
                                 "Click the box, then press your keys. Works from any app."))
        row = QHBoxLayout()
        self.keys = QKeySequenceEdit(QKeySequence(settings["hotkey"]))
        try:
            self.keys.setMaximumSequenceLength(1)
        except Exception:
            pass
        self.keys.keySequenceChanged.connect(self._keys)
        btn_reset = QPushButton("Reset")
        btn_reset.setToolTip(f"Back to {DEFAULTS['hotkey']}")
        btn_reset.clicked.connect(lambda: self._apply_hotkey(DEFAULTS["hotkey"]))
        row.addWidget(self.keys, 1)
        row.addWidget(btn_reset)
        cl.addLayout(row)
        self.key_note = QLabel("")
        self.key_note.setObjectName("sub")
        self.key_note.setWordWrap(True)
        self.key_note.hide()
        cl.addWidget(self.key_note)
        cl.addLayout(_text_block("Dictation shortcut",
                                 "Manual mode: press to start, press again to stop and type. "
                                 "Auto mode: turns the mic on / off."))
        row = QHBoxLayout()
        self.keys2 = QKeySequenceEdit(QKeySequence(settings.get("dict_hotkey", DEFAULTS["dict_hotkey"])))
        try:
            self.keys2.setMaximumSequenceLength(1)
        except Exception:
            pass
        self.keys2.keySequenceChanged.connect(self._keys2)
        btn_reset2 = QPushButton("Reset")
        btn_reset2.setToolTip(f"Back to {DEFAULTS['dict_hotkey']}")
        btn_reset2.clicked.connect(lambda: self._apply_hotkey2(DEFAULTS["dict_hotkey"]))
        row.addWidget(self.keys2, 1)
        row.addWidget(btn_reset2)
        cl.addLayout(row)
        c1.addWidget(card)

        card, cl = _card()
        cl.addLayout(_text_block("Pop-up position", "Pick a spot, or just drag the pop-up."))
        self.picker = PositionPicker(settings["position"])
        self.picker.chosen.connect(self._pos)
        cl.addWidget(self.picker, 0, Qt.AlignmentFlag.AlignHCenter)
        c1.addWidget(card)
        c1.addStretch()

        # ================= column 2: microphone + try it
        card, cl = _card()
        cl.addLayout(_text_block("Microphone", "Choose the input device. The bar moves when it hears you."))
        self.mic = QComboBox()
        cl.addWidget(self.mic)
        row = QHBoxLayout()
        self.meter = QProgressBar()
        self.meter.setObjectName("meter")
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        self.meter.setFixedHeight(8)
        btn_ref = QPushButton("Refresh")
        btn_ref.clicked.connect(self._refresh_mics)
        row.addWidget(self.meter, 1)
        row.addWidget(btn_ref)
        cl.addLayout(row)
        self.mic_note = QLabel("")
        self.mic_note.setObjectName("sub")
        self.mic_note.setWordWrap(True)
        cl.addWidget(self.mic_note)
        c2.addWidget(card)

        card, cl = _card()
        cl.addLayout(_text_block("Try it",
                                 "1. Load a model   2. Click in the box below   3. Speak, then pause. "
                                 "The text is typed at your cursor - in any app."))
        self.test = QPlainTextEdit()
        self.test.setPlaceholderText("Click here and speak...")
        self.test.setFixedHeight(120)
        cl.addWidget(self.test)
        self.state = QLabel("")
        self.state.setObjectName("ttl")
        self.state.setWordWrap(True)
        cl.addWidget(self.state)
        self.last = QLabel("Last heard: -")
        self.last.setObjectName("sub")
        self.last.setWordWrap(True)
        cl.addWidget(self.last)
        c2.addWidget(card)

        card, cl = _card()
        cl.addLayout(_text_block("Response speed", "How long a pause before your words are typed. Shorter = faster."))
        self.speed = QComboBox()
        for label, blocks in SPEEDS:
            self.speed.addItem(label, blocks)
        self.speed.setCurrentIndex(max(0, self.speed.findData(settings.get("pause_blocks", 8))))
        self.speed.currentIndexChanged.connect(self._speed)
        cl.addWidget(self.speed)
        cl.addLayout(_text_block("Stop mode", "Auto stops when you pause. Manual records until you press the dictation shortcut again."))
        self.stopmode = QComboBox()
        self.stopmode.addItem("Auto  -  stops when you pause", "auto")
        self.stopmode.addItem("Manual  -  press shortcut again to stop", "manual")
        self.stopmode.setCurrentIndex(max(0, self.stopmode.findData(settings.get("stop_mode", "auto"))))
        self.stopmode.currentIndexChanged.connect(self._stop_mode)
        cl.addWidget(self.stopmode)
        self.speed.setEnabled(settings.get("stop_mode", "auto") != "manual")
        c2.addWidget(card)

        card, cl = _card()
        cl.addLayout(_text_block("Custom symbols (.txt)", "phrase = replacement. Saved edits reload on the next dictation. Say enter for a new line; literal enter for the word."))
        self.symbol_path = QLineEdit(settings.get("custom_symbols_path", ""))
        self.symbol_path.setReadOnly(True)
        self.symbol_path.setPlaceholderText("Default custom_symbols.txt")
        self.symbol_path.setToolTip(settings.get("custom_symbols_path", ""))
        row = QHBoxLayout()
        row.addWidget(self.symbol_path, 1)
        button = QPushButton("Select...")
        button.clicked.connect(self._pick_symbols)
        row.addWidget(button)
        cl.addLayout(row)
        row = QHBoxLayout()
        for text, callback in (("Edit", self._edit_symbols), ("Reload", self._reload_symbols)):
            button = QPushButton(text)
            button.clicked.connect(callback)
            row.addWidget(button)
        cl.addLayout(row)
        self.symbol_status = QLabel("UTF-8 text; built-in symbols and numbers remain available.")
        self.symbol_status.setObjectName("sub")
        self.symbol_status.setWordWrap(True)
        cl.addWidget(self.symbol_status)
        c2.addWidget(card)
        c2.addStretch()

        # ================= column 3: model
        card, cl = _card()
        cl.addLayout(_text_block("Speech model", "CrispASR or true Parakeet Unified GGUF. Quantizations depend on the native engine."))
        self.status = QLabel()
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.RichText)
        cl.addWidget(self.status)
        self.repo = QComboBox()
        self.repo.setEditable(True)
        self.repo.addItems(PRESET_REPOS)
        self.repo.setToolTip("Hugging Face repo id for a supported CrispASR or transcribe.cpp speech model")
        self.combo = QComboBox()
        self.combo.addItems(list(QUANTS))
        cl.addWidget(self.repo)
        cl.addWidget(self.combo)

        cl.addLayout(_text_block("Unload model when idle", "Frees RAM. Click the mic to reload it."))
        self.unload = QComboBox()
        for label, mins in UNLOAD_CHOICES:
            self.unload.addItem(label, mins)
        i = self.unload.findData(settings.get("unload_min", 0))
        self.unload.setCurrentIndex(i if i >= 0 else 0)
        self.unload.currentIndexChanged.connect(self._unload)
        row = QHBoxLayout()
        row.addWidget(self.unload, 1)
        self.btn_unload = QPushButton("Unload now")
        self.btn_unload.setToolTip("Release model RAM now. Disabled while recording, transcribing, or loading.")
        self.btn_unload.clicked.connect(self._unload_now)
        row.addWidget(self.btn_unload)
        cl.addLayout(row)

        cl.addLayout(_text_block("Download folder"))
        row = QHBoxLayout()
        self.dir_lbl = QLabel()
        self.dir_lbl.setObjectName("path")
        btn_dir = QPushButton("Change...")
        btn_dir.clicked.connect(self._pick_dir)
        row.addWidget(self.dir_lbl, 1)
        row.addWidget(btn_dir)
        cl.addLayout(row)
        self.show_dir()

        row = QHBoxLayout()
        self.btn_dl = QPushButton("Download")
        self.btn_dl.setObjectName("primary")
        self.btn_dl.clicked.connect(self._download)
        self.btn_pick = QPushButton("Select .gguf")
        self.btn_pick.clicked.connect(self._pick)
        row.addWidget(self.btn_dl, 1)
        row.addWidget(self.btn_pick, 1)
        cl.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(5)
        self.bar.hide()
        cl.addWidget(self.bar)
        c3.addWidget(card)

        card, cl = _card()
        cl.addLayout(_text_block("Pop-up size", "Small, medium or large pop-up."))
        self.size = QComboBox()
        for label, key in ("Small", "small"), ("Medium", "medium"), ("Large", "large"):
            self.size.addItem(label, key)
        self.size.setCurrentIndex(max(0, self.size.findData(settings.get("size", "medium"))))
        self.size.currentIndexChanged.connect(self._size)
        cl.addWidget(self.size)
        c3.addWidget(card)

        card, cl = _card()
        row = QHBoxLayout()
        row.addLayout(_text_block("Release updates", "Download automatically; always ask before closing/installing."), 1)
        self.updates_toggle = Switch(settings.get("check_updates", True))
        self.updates_toggle.toggled.connect(self._updates)
        row.addWidget(self.updates_toggle)
        cl.addLayout(row)
        self.update_status = QLabel("Checks stable GitHub releases; retains this folder and settings.")
        self.update_status.setObjectName("sub")
        self.update_status.setWordWrap(True)
        cl.addWidget(self.update_status)
        button = QPushButton("Check for updates")
        button.clicked.connect(lambda: self.update_check_requested.emit())
        cl.addWidget(button)
        c3.addWidget(card)
        c3.addStretch()

        self.download_done.connect(self._dl_ok)
        self.download_failed.connect(self._dl_fail)
        self.engine.heard.connect(lambda t: self.last.setText("Last heard: " + t))
        self.tick = QTimer(self, interval=60)
        self.tick.timeout.connect(self._tick)
        self.populate_mics()
        self.mic.currentIndexChanged.connect(self._mic)
        for combo in self.findChildren(QComboBox):
            combo.setMinimumWidth(0)
            combo.setMinimumContentsLength(8)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.apply_theme()
        self.refresh_status()
        self.refresh_position()

    # ---- show / hide: keep the mic open for the level meter only while visible
    def showEvent(self, e):
        super().showEvent(e)
        self.engine.set_monitor(True)
        self.tick.start()

    def hideEvent(self, e):
        super().hideEvent(e)
        self.tick.stop()
        self.engine.set_monitor(False)

    def _tick(self):
        e = self.engine
        self.btn_unload.setEnabled(e.model is not None and _update_install_safe(e))
        lvl = e.level if e.active else e.mon_level
        self.meter.setValue(min(100, int(lvl * 22 * 100)))
        if e.model is None:
            s = ("Model unloaded - click the mic in the pop-up to reload" if e.unloaded_idle
                 else "Load a speech model first (right side)")
        elif not e.listening:
            s = "Microphone is OFF - click the mic in the pop-up"
        elif e.paused:
            s = "Paused"
        elif e.busy:
            s = "Transcribing..."
        elif e.speaking:
            s = "Hearing you..."
        else:
            s = "Listening - speak now"
        if self.state.text() != s:
            self.state.setText(s)

    # ---- look
    def apply_theme(self):
        dark = self.settings["dark_mode"]
        c = palette(dark)
        qss = """
        QDialog {background:transparent;}
        QFrame#win {background:@bg@; border:1px solid @border@; border-radius:20px;}
        QWidget#bar {background:transparent;}
        QScrollArea {background:transparent; border:none;}
        QScrollArea > QWidget > QWidget {background:transparent;}
        QLabel {background:transparent; color:@text@; font:10pt 'Segoe UI';}
        QLabel#h1 {font:600 22pt 'Segoe UI';}
        QLabel#ttl {font:600 10.5pt 'Segoe UI';}
        QLabel#sub {color:@sub@; font:9pt 'Segoe UI';}
        QLabel#path {color:@sub@; font:9pt 'Segoe UI'; background:@input@; border-radius:9px; padding:8px 10px;}
        QFrame#card {background:@card@; border:1px solid @border@; border-radius:16px;}
        QLineEdit {background:@input@; color:@text@; border:1px solid @border@; border-radius:10px;
                   padding:8px 10px; font:10pt 'Segoe UI'; selection-background-color:@accent@;}
        QLineEdit:focus {border:1px solid @accent@;}
        QPlainTextEdit {background:@input@; color:@text@; border:1px solid @border@; border-radius:12px;
                   padding:8px 10px; font:10pt 'Segoe UI'; selection-background-color:@accent@;}
        QPlainTextEdit:focus {border:1px solid @accent@;}
        QComboBox {background:@input@; color:@text@; border:1px solid @border@; border-radius:10px;
                   padding:6px 10px; font:10pt 'Segoe UI';}
        QComboBox QLineEdit {border:none; background:transparent; padding:2px;}
        QComboBox::drop-down {border:none; width:26px;}
        QComboBox QAbstractItemView {background:@card@; color:@text@; border:1px solid @border@;
                   selection-background-color:@accent@; selection-color:white; outline:none;}
        QPushButton {background:@input@; color:@text@; border:1px solid @border@; border-radius:10px;
                   padding:9px 14px; font:600 10pt 'Segoe UI';}
        QPushButton:hover {background:@hover@;}
        QPushButton#primary {background:@accent@; color:white; border:none;}
        QPushButton#primary:hover {background:#d764f6;}
        QPushButton:disabled {color:@sub@;}
        QProgressBar {background:@input@; border:none; border-radius:2px;}
        QProgressBar::chunk {background:@accent@; border-radius:2px;}
        QProgressBar#meter {border-radius:4px;}
        QProgressBar#meter::chunk {background:#34c759; border-radius:4px;}
        QScrollBar:vertical {background:transparent; width:10px; margin:2px; border:none; border-radius:5px;}
        QScrollBar::handle:vertical {background:@hover@; border-radius:5px; min-height:30px;}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {height:0; width:0;}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {background:transparent;}
        QScrollBar:horizontal {background:@card@; height:10px; margin:2px; border:none; border-radius:5px;}
        QScrollBar::handle:horizontal {background:@hover@; border-radius:5px; min-width:30px;}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {height:0; width:0;}
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {background:transparent;}
        """
        for k, v in dict(c, accent=ACCENT).items():
            qss = qss.replace(f"@{k}@", v)
        self.setStyleSheet(qss)
        self.picker.set_theme(dark)

    def refresh_position(self):
        self.picker.set_current(self.settings["position"])

    def refresh_status(self, extra=None):
        p = self.settings["model_path"]
        name = _short(Path(p).name) if p else ""
        if extra:
            text = extra
        elif self.engine.model is not None:
            info = self.engine.model_info
            details = " / ".join(str(info[key])[:80] for key in ("architecture", "quantization") if info.get(key))
            size = f" / {info['bytes'] / (1024 * 1024):.0f} MiB file" if info.get('bytes') else ''
            text = f"Loaded: {name}" + (f"\n{details}{size}" if details or size else "")
        elif p and self.engine.unloaded_idle:
            text = f"Unloaded: {name} - click the mic to reload"
        elif p:
            text = f"Selected: {name}"
        else:
            text = "No model selected yet"
        low = text.lower()
        if low.startswith(("load failed", "download failed", "could not", "enter a")):
            color = "#ff453a"
        elif low.startswith(("loading", "downloading", "installing", "downloaded")):
            color = ACCENT
        elif low.startswith("loaded"):
            color = "#34c759"
        else:
            color = "#8d8d98"
        self.status.setToolTip(p)
        self.status.setText(f"<span style='color:{color}; font-size:12pt;'>\u25cf</span>&nbsp; "
                            f"{html.escape(text).replace(chr(10), '<br>')}")
        self.btn_unload.setEnabled(self.engine.model is not None and _update_install_safe(self.engine))

    # ---- microphone
    def populate_mics(self):
        devs = list_input_devices()
        self.mic.blockSignals(True)
        self.mic.clear()
        self.mic.addItem("System default", "")
        for name in devs:
            self.mic.addItem(name, name)
        i = self.mic.findData(self.settings["mic"])
        self.mic.setCurrentIndex(i if i >= 0 else 0)
        self.mic.blockSignals(False)
        if devs:
            self.mic_note.setText(f"{len(devs)} microphone(s) detected.")
        else:
            self.mic_note.setText("No microphone detected. Plug one in and press Refresh.")

    def _mic(self, i):
        name = self.mic.itemData(i) or ""
        self.settings["mic"] = name
        save_settings(self.settings)
        self.engine.set_device(name)

    def _refresh_mics(self):
        self.engine.refresh_devices()
        self.populate_mics()

    # ---- handlers
    def _pos(self, key):
        self.settings["position"] = key
        save_settings(self.settings)
        self.position_chosen.emit(key)

    def _dark(self, on):
        self.settings["dark_mode"] = on
        save_settings(self.settings)
        self.apply_theme()
        self.theme_changed.emit()

    def _startup(self, on):
        ok = set_startup(on)
        self.settings["start_on_startup"] = on and ok
        save_settings(self.settings)
        if on and not ok:
            self.startup.set_state(False)
            self.refresh_status("Could not enable startup (Windows only).")

    def _set_keys(self, text):
        self.keys.blockSignals(True)
        self.keys.setKeySequence(QKeySequence(text))
        self.keys.blockSignals(False)

    def _apply_hotkey(self, text):
        self._set_keys(text)
        self.settings["hotkey"] = text
        save_settings(self.settings)
        self.key_note.hide()
        self.hotkey_changed.emit(text)

    def _keys(self, seq):
        text = seq.toString(QKeySequence.SequenceFormat.PortableText).split(",")[0].strip()
        if not text:
            return
        if to_pynput(text) is None:
            self.key_note.setText("Use a modifier (Ctrl, Alt, Shift) with a key, or an F-key.")
            self.key_note.show()
            self._set_keys(self.settings["hotkey"])
            return
        if text == self.settings.get("dict_hotkey"):
            self.key_note.setText("That shortcut is already used for dictation.")
            self.key_note.show()
            self._set_keys(self.settings["hotkey"])
            return
        self._apply_hotkey(text)

    def _set_keys2(self, text):
        self.keys2.blockSignals(True)
        self.keys2.setKeySequence(QKeySequence(text))
        self.keys2.blockSignals(False)

    def _apply_hotkey2(self, text):
        self._set_keys2(text)
        self.settings["dict_hotkey"] = text
        save_settings(self.settings)
        self.key_note.hide()
        self.dict_hotkey_changed.emit(text)

    def _keys2(self, seq):
        text = seq.toString(QKeySequence.SequenceFormat.PortableText).split(",")[0].strip()
        if not text:
            return
        if to_pynput(text) is None:
            self.key_note.setText("Use a modifier (Ctrl, Alt, Shift) with a key, or an F-key.")
            self.key_note.show()
            self._set_keys2(self.settings.get("dict_hotkey", DEFAULTS["dict_hotkey"]))
            return
        if text == self.settings["hotkey"]:
            self.key_note.setText("That shortcut is already used to show / hide the pop-up.")
            self.key_note.show()
            self._set_keys2(self.settings.get("dict_hotkey", DEFAULTS["dict_hotkey"]))
            return
        self._apply_hotkey2(text)

    def _stop_mode(self, i):
        key = self.stopmode.itemData(i) or "auto"
        self.settings["stop_mode"] = key
        save_settings(self.settings)
        self.speed.setEnabled(key != "manual")
        self.stop_mode_changed.emit(key)

    def _speed(self, i):
        n = int(self.speed.itemData(i) or 8)
        self.settings["pause_blocks"] = n
        save_settings(self.settings)
        self.engine.silence_blocks = n

    def _auto_enter(self, on):
        self.settings["auto_enter"] = on
        save_settings(self.settings)
        self.engine.auto_enter = on

    def _size(self, i):
        key = self.size.itemData(i) or "medium"
        self.settings["size"] = key
        save_settings(self.settings)
        self.size_changed.emit(key)

    def _unload_now(self):
        if self.engine.unload():
            self.refresh_status()
        else:
            self.refresh_status("Finish recording/transcription before unloading the model.")

    def _pick_symbols(self):
        start = str(Path(self.settings.get('custom_symbols_path') or APP_DIR).parent)
        path, _ = QFileDialog.getOpenFileName(self, "Select custom symbols", start, "UTF-8 text (*.txt)")
        if path:
            self._set_symbols(path)

    def _set_symbols(self, path):
        try:
            self.engine.configure_custom_symbols(path)
            self.settings['custom_symbols_path'] = str(Path(path).resolve())
            save_settings(self.settings)
            self.symbol_path.setText(self.settings['custom_symbols_path'])
            self.symbol_path.setToolTip(self.settings['custom_symbols_path'])
            self.symbol_status.setText(f"Loaded {len(self.engine.custom_symbols._values)} custom phrase(s). Edits reload automatically.")
            return True
        except (OSError, ValueError) as exc:
            self.symbol_status.setText('Custom symbols: ' + str(exc) + ' Last valid mappings remain active.')
            return False

    def _reload_symbols(self):
        try:
            return self._set_symbols(_custom_symbols_file(self.settings))
        except (OSError, ValueError) as exc:
            self.symbol_status.setText('Custom symbols: ' + str(exc))
            return False

    def _edit_symbols(self):
        if self._reload_symbols():
            path = self.settings['custom_symbols_path']
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
                self.symbol_status.setText('Open this file in your text editor: ' + path)

    def _updates(self, on):
        self.settings['check_updates'] = bool(on)
        save_settings(self.settings)
        self.updates_changed.emit(bool(on))

    def set_update_status(self, text):
        self.update_status.setText(text)

    def _unload(self, i):
        self.settings["unload_min"] = int(self.unload.itemData(i) or 0)
        save_settings(self.settings)

    def show_dir(self):
        d = self.settings["download_dir"]
        self.dir_lbl.setToolTip(d)
        self.dir_lbl.setText(self.dir_lbl.fontMetrics().elidedText(d, Qt.TextElideMode.ElideMiddle, 170))

    def _pick_dir(self):
        cur = self.settings["download_dir"]
        start = cur if Path(cur).exists() else str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "Choose the model download folder", start)
        if d:
            self.settings["download_dir"] = d
            save_settings(self.settings)
            self.show_dir()

    def _pick(self):
        cur = self.settings["model_path"]
        start = str(Path(cur).parent) if cur else self.settings["download_dir"]
        if not Path(start).exists():
            start = str(Path.home())
        f, _ = QFileDialog.getOpenFileName(self, "Select a GGUF speech model", start, "GGUF model (*.gguf)")
        if f:
            self.model_chosen.emit(f)
            self.refresh_status(f"Loading: {_short(Path(f).name)}")

    def _download(self):
        quant = QUANTS[self.combo.currentText()]
        repo = self.repo.currentText().strip()
        if "/" not in repo:
            self.refresh_status("Enter a Hugging Face repo id like  user/model-GGUF")
            return
        dest = Path(self.settings["download_dir"])
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.refresh_status(f"Download failed: cannot use that folder ({e})")
            return
        self.btn_dl.setEnabled(False)
        self.btn_pick.setEnabled(False)
        self.bar.show()
        self.refresh_status("Downloading... this can take a few minutes.")

        def run():
            try:
                from huggingface_hub import hf_hub_download, list_repo_files
                files = [f for f in list_repo_files(repo) if f.lower().endswith(".gguf")]
                selected = select_model_file(files, quant)
                out = hf_hub_download(repo, selected, local_dir=str(dest / repo.split("/")[-1]))
                self.download_done.emit(str(out))
            except Exception as e:
                self.download_failed.emit(str(e))
        threading.Thread(target=run, daemon=True).start()

    def _dl_ok(self, path):
        self.bar.hide()
        self.btn_dl.setEnabled(True)
        self.btn_pick.setEnabled(True)
        self.model_chosen.emit(path)
        self.refresh_status(f"Downloaded. Loading: {_short(Path(path).name)}")

    def _dl_fail(self, err):
        self.bar.hide()
        self.btn_dl.setEnabled(True)
        self.btn_pick.setEnabled(True)
        self.refresh_status(f"Download failed: {err}")


# --------------------------------------------------------------------- main
class App:
    MARGIN = 14

    def __init__(self, *, started_at_login=False):
        _configure_windows_identity()
        self.qt = QApplication(sys.argv)
        self.qt.setWindowIcon(_application_icon())
        self.qt.setQuitOnLastWindowClosed(False)
        self._single = QSharedMemory("Dictate-1.0-single-instance")
        if not self._single.create(1):      # already running: don't open a second copy
            sys.exit(0)
        self.settings = load_settings()
        startup_ok = sync_startup(self.settings, started_at_login=started_at_login)
        self._dictation_shortcut_registered = False
        self._shortcut_input_filter = _DictationShortcutFilter(self)
        self.engine = Engine()
        custom_error = ""
        try:
            self.engine.configure_custom_symbols(_custom_symbols_file(self.settings))
        except (OSError, ValueError) as exc:
            custom_error = 'Custom symbols: ' + str(exc)
        self.engine.device_name = self.settings["mic"]
        self.engine.auto_enter = self.settings.get("auto_enter", False)
        self.engine.silence_blocks = int(self.settings.get("pause_blocks", 8))
        self.pill = Pill(self.engine, self.settings)
        self.toast = Toast()
        self.dialog = None
        self._dictation_after_load = False
        self._quitting = False
        self._update_prompt_open = False
        self.updater = ReleaseUpdater()
        self.updater.status_changed.connect(self._update_status)
        self.updater.download_ready.connect(self.consider_update)
        self._last_update_check = time.monotonic()

        self.pill.move(self.preset_point(self.settings["position"]))
        self.pill.fade_in()
        raise_topmost(self.pill)
        QGuiApplication.primaryScreen().availableGeometryChanged.connect(lambda *_: self.pill.keep_visible())

        self.pill.settings_requested.connect(self.open_settings)
        self.pill.quit_requested.connect(self.quit)
        self.pill.dragged.connect(self.on_dragged)
        self.pill.notify.connect(lambda m: self.say(m, 1800))
        self.engine.model_ready.connect(self.on_ready)
        self.engine.model_error.connect(self.on_error)
        self.engine.unloaded.connect(self.on_unloaded)
        self.pill.reload_requested.connect(self.reload_model)
        self.engine.message.connect(lambda m: self.say(m, 4000))

        self.tray = QSystemTrayIcon(self.qt.windowIcon())
        self.tray.setToolTip(f"{APP_NAME} {VERSION}")
        menu = QMenu()
        a0, a1, a2 = QAction("Show / hide pop-up", menu), QAction("Settings", menu), QAction("Quit", menu)
        a0.triggered.connect(self.toggle_popup)
        a1.triggered.connect(self.open_settings)
        a2.triggered.connect(self.quit)
        unload_action = QAction("Unload model now", menu)
        unload_action.triggered.connect(self.unload_model)
        update_action = QAction("Check for updates", menu)
        update_action.triggered.connect(lambda: self.check_updates(manual=True))
        for a in (a0, a1, unload_action, update_action, a2):
            menu.addAction(a)
        menu.aboutToShow.connect(lambda: unload_action.setEnabled(
            self.engine.model is not None and _update_install_safe(self.engine)))
        self.tray.setContextMenu(menu)
        self._menu, self._acts = menu, (a0, a1, a2)
        self.tray.activated.connect(
            lambda r: self.toggle_popup() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()
        self.hotkeys = Hotkeys()
        self.hotkeys.triggered.connect(self.toggle_popup)
        self.set_hotkey(self.settings["hotkey"])
        self.engine.manual = self.settings.get("stop_mode", "auto") == "manual"
        self.dict_hotkeys = Hotkeys()
        self.dict_hotkeys.triggered.connect(self.on_dictation_key)
        self.set_dict_hotkey(self.settings.get("dict_hotkey", DEFAULTS["dict_hotkey"]))

        self.idle_timer = QTimer(interval=15000)
        self.idle_timer.timeout.connect(self.check_idle)
        self.idle_timer.start()
        self.update_timer = QTimer(interval=3000)
        self.update_timer.timeout.connect(self._update_tick)
        self.update_timer.start()
        if os.name == 'nt' and self.settings.get('check_updates', True):
            QTimer.singleShot(8000, self.check_updates)

        path = self.settings["model_path"]
        if path and Path(path).exists():
            self.engine.load_model(path)
            self.say("Loading model...")
        else:
            self.say("Open Settings to download or select a model")
        if custom_error:
            QTimer.singleShot(2500, lambda: self.say(custom_error, 6000))
        if not startup_ok:
            QTimer.singleShot(1500, lambda: self.say(
                "Could not update Windows startup. Check Start on startup in Settings.", 6000))

    # ---- positions
    def preset_point(self, key):
        g = (self.pill.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        w, h, m = self.pill.width(), self.pill.height(), self.MARGIN
        if key == "custom" and self.settings.get("pos"):
            x, y = self.settings["pos"]
            return self.pill.clamped(QPoint(x, y))
        row, col = (key.split("_") + ["center"])[:2] if "_" in key else ("bottom", "center")
        x = {"left": g.left() + m, "center": g.center().x() - w // 2, "right": g.right() - w - m + 1}.get(col, 0)
        y = {"top": g.top() + m, "middle": g.center().y() - h // 2, "bottom": g.bottom() - h - m + 1}.get(row, 0)
        return self.pill.clamped(QPoint(x, y))

    def on_size(self, key):
        self.pill.set_scale(SIZES.get(key, 1.0))
        self.go_to_preset(self.settings["position"])

    def go_to_preset(self, key):
        self.pill.glide_to(self.preset_point(key))

    def on_dragged(self):
        self.settings["position"] = "custom"
        end = self.pill.clamped(self.pill.pos())
        self.settings["pos"] = [end.x(), end.y()]
        save_settings(self.settings)
        if self.dialog:
            self.dialog.refresh_position()

    # ---- model / ui events
    @_preserve_input_focus
    def say(self, text, ms=2800):
        if self.pill.isVisible() and not self.pill.user_hidden:
            self.toast.show_text(text, self.pill, self.settings["dark_mode"], ms)
        else:   # pop-up is closed: use a tray notification instead
            self.tray.showMessage(APP_NAME, text, QSystemTrayIcon.MessageIcon.NoIcon, ms)

    def toggle_popup(self):
        if self.pill.isVisible() and not self.pill.user_hidden:
            self.pill.hide_popup()
        else:
            self.pill.show_popup()



    def set_dict_hotkey(self, seq):
        ok = self.dict_hotkeys.set(seq) if seq else False
        self._dictation_shortcut_registered = bool(ok)
        self.engine.shortcut = seq or DEFAULTS["dict_hotkey"]
        if seq and not ok:
            self.say("Dictation shortcut could not be registered - pick a different one.", 4500)

    def on_stop_mode(self, key):
        self.engine.set_manual(key == "manual")
        self.say("Manual: press the dictation shortcut to start / stop" if key == "manual"
                 else "Auto: stops when you pause", 3000)

    @_preserve_input_focus
    def on_dictation_key(self):
        e = self.engine
        if e.model is None:
            mp = self.settings["model_path"]
            if mp and Path(mp).exists():
                self._dictation_after_load = not self._dictation_after_load
                if self._dictation_after_load:
                    if not e.loading:
                        self.reload_model()
                    else:
                        self.say("Loading model - recording will start when ready", 2500)
                else:
                    self.say("Recording start cancelled", 1800)
            else:
                self.say("Load a model first")
            return
        # The dictation shortcut is always explicit start/commit, never mic-off.
        # Pop-up visibility remains available in the existing tray menu.
        r = e.toggle_record()
        if r is True:
            self.say("Recording - press the shortcut again to stop", 2500)
        elif r is False:
            self.say("Typing...", 1200)
        else:
            self.say("Microphone could not start - check the selected input", 3000)

    def set_hotkey(self, seq):
        ok = self.hotkeys.set(seq) if seq else False
        tip = f"{APP_NAME} {VERSION}" + (f"  -  {seq} shows/hides the pop-up" if ok else "")
        self.tray.setToolTip(tip)
        if seq and not ok:
            self.say("Shortcut could not be registered - another app may use it. Pick a different one.", 4500)

    def on_ready(self):
        # Loading a model must not start automatic, pause-based submissions.
        if self._dictation_after_load:
            self._dictation_after_load = False
            self.on_dictation_key()
        else:
            self.say("Model loaded - press the dictation shortcut to record.", 3500)
        if self.dialog:
            self.dialog.refresh_status()

    def on_error(self, err):
        self._dictation_after_load = False
        self.say("Model failed to load", 4000)
        if self.dialog:
            self.dialog.refresh_status(f"Load failed: {err}")

    def check_idle(self):
        e, mins = self.engine, int(self.settings.get("unload_min", 0) or 0)
        if (mins > 0 and e.model is not None and not e.loading and not e.busy
                and not e.recording and not e.pending and not e.speaking
                and time.time() - e.last_used > mins * 60):
            e.unload()

    def unload_model(self):
        if not self.engine.unload():
            self.say("Finish recording/transcription before unloading the model.", 3500)

    def _update_status(self, text):
        if self.dialog is not None:
            self.dialog.set_update_status(text)
        if self.updater.manual and text.startswith(('Update check failed', 'Dictate ')):
            self.say(text, 5000)

    def check_updates(self, manual=False):
        if self._quitting or (not manual and not self.settings.get('check_updates', True)):
            return
        if os.name != 'nt':
            self.updater._status('The installer updater is available in the Windows app.')
            return
        self._last_update_check = time.monotonic()
        self.updater.check(manual=manual)

    def set_updates_enabled(self, enabled):
        if enabled:
            self.check_updates()
        elif not self.updater.manual:
            self.updater.cancel()

    def _update_tick(self):
        if (os.name == 'nt' and self.settings.get('check_updates', True)
                and time.monotonic() - self._last_update_check >= 6 * 60 * 60):
            self.check_updates()
        self.consider_update()

    def consider_update(self):
        updater, engine = self.updater, self.engine
        candidate = updater.candidate
        if (self._quitting or self._update_prompt_open or candidate is None
                or candidate['version'] == updater.deferred_version
                or not (self.settings.get('check_updates', True) or updater.manual)
                or not _update_install_safe(engine) or self._dictation_after_load):
            return
        # A continuously-listening legacy auto session can start receiving speech
        # without a shortcut. Require mic-off for that mode; manual Stop sessions
        # may retain their harmless open input stream after the bulk insertion.
        if engine.listening and not (engine.manual or engine._shortcut_mode):
            return
        self._update_prompt_open = True
        try:
            directory = _installed_update_directory()
            question = QMessageBox(QMessageBox.Icon.Question, 'Dictate update ready',
                f"Dictate {candidate['version']} has been downloaded and SHA-256 verified.\n\n"
                f"Close Dictate, install into this existing folder, and restart?\n{directory}\n\n"
                "Your settings, shortcuts, custom symbols, and downloaded models are retained.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self.dialog)
            question.setTextFormat(Qt.TextFormat.PlainText)
            question.setDefaultButton(QMessageBox.StandardButton.No)
            question.button(QMessageBox.StandardButton.Yes).setText('Install and restart')
            question.button(QMessageBox.StandardButton.No).setText('Later')
            if question.exec() != QMessageBox.StandardButton.Yes:
                updater.deferred_version = candidate['version']
                return
            # Modal dialogs process shortcut events: consent must not override a
            # recording/transcription that started while the question was open.
            if (not _update_install_safe(engine) or self._dictation_after_load
                    or (engine.listening and not (engine.manual or engine._shortcut_mode))):
                self.say('Update postponed until dictation is finished. Nothing was closed.', 5000)
                return
            _launch_update_helper(candidate, updater.installer, directory)
            self.quit()
        except Exception as exc:
            updater.deferred_version = candidate['version']
            updater._status('Update installation blocked: ' + str(exc))
            self.say(updater.status, 6000)
        finally:
            self._update_prompt_open = False

    def on_unloaded(self):
        self.say("Model unloaded to free memory - click the mic to reload", 4000)
        if self.dialog:
            self.dialog.refresh_status()

    def reload_model(self):
        path = self.settings["model_path"]
        if path and Path(path).exists():
            self.say("Loading model...")
            self.engine.load_model(path)

    def choose_model(self, path):
        self._dictation_after_load = False
        self.engine.set_listening(False)
        self.settings["model_path"] = path
        save_settings(self.settings)
        self.engine.load_model(path)
        self.say("Loading model...")

    def open_settings(self):
        if self.dialog is None:
            self.dialog = SettingsDialog(self.settings, self.engine)
            self.dialog.theme_changed.connect(self.pill.update)
            self.dialog.model_chosen.connect(self.choose_model)
            self.dialog.position_chosen.connect(self.go_to_preset)
            self.dialog.hotkey_changed.connect(self.set_hotkey)
            self.dialog.size_changed.connect(self.on_size)
            self.dialog.dict_hotkey_changed.connect(self.set_dict_hotkey)
            self.dialog.stop_mode_changed.connect(self.on_stop_mode)
            self.dialog.update_check_requested.connect(lambda: self.check_updates(manual=True))
            self.dialog.updates_changed.connect(self.set_updates_enabled)
        self.dialog.set_update_status(self.updater.status)
        self.dialog.refresh_status()
        self.dialog.refresh_position()
        if not self.dialog.isVisible():
            g = (self.pill.screen() or QGuiApplication.primaryScreen()).availableGeometry()
            self.dialog.resize(min(1000, g.width() - 40), min(720, g.height() - 40))
            self.dialog.move(g.center() - self.dialog.rect().center())
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def quit(self):
        if self._quitting:
            return
        self._quitting = True
        self.idle_timer.stop()
        self.update_timer.stop()
        self.updater.shutdown()
        if self.dialog is not None:
            self.dialog.tick.stop()
        self.engine.shutdown()
        self.hotkeys.stop()
        self.dict_hotkeys.stop()
        self.tray.hide()
        self.qt.quit()

    def run(self):
        sys.exit(self.qt.exec())


def _packaging_smoke_test(arguments):
    """Non-interactive installer verification; never opens a microphone."""
    import argparse
    import hashlib
    import importlib.metadata
    import wave
    parser = argparse.ArgumentParser()
    parser.add_argument("--packaging-smoke-test", required=True)
    parser.add_argument("--model", action="append", default=[])
    parser.add_argument("--audio")
    options = parser.parse_args(arguments)
    output = Path(options.packaging_smoke_test)
    report = {"version": VERSION, "frozen": bool(getattr(sys, "frozen", False)),
              "app_id": _WINDOWS_APP_ID, "native_models": []}
    engine = None
    def stage(name):
        report["stage"] = name
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    stage("entered smoke test")
    try:
        stage("initializing Qt")
        _configure_windows_identity()
        application = QApplication.instance() or QApplication([APP_NAME])
        application.setWindowIcon(_application_icon())
        assert not application.windowIcon().isNull(), "Embedded logo did not load"
        report["logo_sha256"] = hashlib.sha256(base64.b64decode(_LOGO_PNG)).hexdigest()
        report["dependencies"] = {}
        for package in ("PyQt6", "numpy", "sounddevice", "pynput", "huggingface-hub", "crispasr", "transcribe-cpp", "transcribe-cpp-native"):
            report["dependencies"][package] = importlib.metadata.version(package)
        assert normalize_dictation_text("four hundred dollars") == "$400"
        assert normalize_dictation_text("400 rupees") == "₹400"
        assert normalize_dictation_text("five zero") == "50"
        assert normalize_dictation_text("twenty") == "20"
        assert normalize_dictation_text("zerozerofive") == "005"
        assert normalize_dictation_text("hello enter world enter five zero") == "hello\nworld\n50"
        report['spoken_enter'] = {'newlines_are_text': True, 'implicit_enter_key': False}
        if getattr(sys, 'frozen', False):
            helper = Path(sys._MEIPASS) / 'Dictate-Updater.exe'
            assert helper.is_file(), 'Native update helper was not packaged'
            with helper.open('rb') as stream:
                assert stream.read(2) == b'MZ', 'Native update helper is not a Windows executable'
            report['updater'] = {'helper_sha256': _file_sha256(helper),
                                 'download_verification': 'GitHub digest + SHA256SUMS.txt',
                                 'install_policy': 'confirmation_required'}
        long_digits = "005123456789" * 834
        long_words = " ".join(tuple(_SMALL_NUMBERS)[int(char)] for char in long_digits)
        assert normalize_dictation_text(long_words) == long_digits
        report["number_conversion"] = {"five_zero": "50", "twenty": "20", "zerozerofive": "005",
                                       "verified_digit_count": len(long_digits), "leading_zeros_preserved": True}
        assert len(set(SPOKEN_SYMBOLS.values())) >= 100
        stage("initializing engine and settings")
        engine = Engine()
        dialog = SettingsDialog(dict(DEFAULTS), engine)
        assert len(dialog.findChildren(QKeySequenceEdit)) == 2
        dialog.tick.stop()
        dialog.close()
        # Import both runtime wrappers even if no model is supplied. Native
        # model runs below also catch missing/incompatible DLL dependencies.
        stage("loading native runtime wrappers")
        import crispasr
        import transcribe_cpp
        assert callable(crispasr.Session), "CrispASR wrapper unavailable"
        report["native_backends"] = [str(value) for value in transcribe_cpp.backends()]
        if options.model:
            assert options.audio, "An audio file is required with --model"
            with wave.open(options.audio, "rb") as audio_file:
                assert audio_file.getframerate() == SR and audio_file.getnchannels() == 1
                assert audio_file.getsampwidth() == 2
                pcm = np.frombuffer(audio_file.readframes(audio_file.getnframes()), "<i2").astype(np.float32) / 32768
            def native_memory(process):
                if os.name != 'nt' or process is None:
                    return None
                from ctypes import wintypes
                class Counters(ctypes.Structure):
                    _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                        (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                        'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                        'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage', 'PrivateUsage')]
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                kernel.OpenProcess.restype = wintypes.HANDLE
                kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                handle = kernel.OpenProcess(0x410, False, process.pid)
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    counters = Counters()
                    counters.cb = ctypes.sizeof(counters)
                    psapi = ctypes.WinDLL('psapi', use_last_error=True)
                    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
                    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                        raise ctypes.WinError(ctypes.get_last_error())
                    return {'working_set_bytes': counters.WorkingSetSize, 'private_bytes': counters.PrivateUsage}
                finally:
                    kernel.CloseHandle(handle)

            for filename in options.model:
                info = _gguf_info(filename)
                runtime = info['runtime']
                stage("loading native model: " + runtime + ' / ' + info['quantization'])
                session_class = engine._import_engine(runtime)
                session = session_class(str(filename), n_threads=4)
                process = getattr(session, '_process', None)
                result = {'runtime': runtime, 'metadata': info}
                try:
                    stage("running inference: " + runtime)
                    text = engine._run(session, pcm)
                    assert "country" in text.lower(), text
                    result['transcript'] = text
                    result['native_memory_before_unload'] = native_memory(process)
                    # Exercise the actual manual/idle unload implementation, not
                    # merely a direct adapter close; no audio/typing is pending.
                    engine.model, engine.model_info = session, info
                    started = time.monotonic()
                    assert engine.unload(), 'Idle model unload was unexpectedly blocked'
                    result['unload_seconds'] = round(time.monotonic() - started, 4)
                    assert engine.model is None
                    if process is not None:
                        assert not process.is_alive(), 'Native model worker survived unloading'
                        assert process.exitcode is not None
                        result['worker_pid'] = process.pid
                        result['worker_exited'] = True
                        result['memory_released_by_process_exit'] = True
                    report['native_models'].append(result)
                finally:
                    session.close()
        report["status"] = "ok"
    except Exception:
        report["status"] = "error"
        report["error"] = traceback.format_exc()
    finally:
        if engine is not None:
            engine.shutdown()
            engine._segment_thread.join(2)
            engine._transcribe_thread.join(2)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.excepthook = log_crash
    if "--sync-startup" in sys.argv:
        sys.exit(0 if sync_startup(load_settings()) else 1)
    started_at_login = "--startup" in sys.argv
    if started_at_login:
        sys.argv.remove("--startup")
    if "--packaging-smoke-test" in sys.argv:
        sys.exit(_packaging_smoke_test(sys.argv[1:]))
    detach_from_console()
    App(started_at_login=started_at_login).run()
