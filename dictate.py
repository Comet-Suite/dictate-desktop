# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Comet-Suite
# Distributed under GPL-3.0; see LICENSE. No warranty.
"""
Dictate 1.2.2  -  system-wide offline dictation
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

APP_NAME, VERSION = "Dictate", "1.2.2"
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
    b"iVBORw0KGgoAAAANSUhEUgAAAQAAAAEACAYAAABccqhmAABsjUlEQVR42u29aZAd13Um+J17M99SG1AACiAIgADBfZVJqiVL"
    b"sk1Zlux22+62PabaMREd7nH0yJ52x/yc7n8UI3r+zO/2RFsx4el1wm26bfVYnrYsa6El2dZCURIpLiKJfS2gUCjU8rbMe+ZH"
    b"5nsvl3sz7823VYkuR8lFVOXLPDfv8p1zvvMdwvi+BAAJIASgEv++IOfkR4j8x6HwEAinGDhGwAogFkCogyCIAICiK6h/KYES"
    b"PyPzNzT8JZD6Ob428TMyPxNlr6HEj/nPGj5C4rMy11P2s4rsSD1LkR3xPbN2xH9HsHgOi2v0tqevN/9OZ7vmMxOXDJ6l4DNT"
    b"z6q71sUO7buwuSYxX6C3o/Q5KPdCAUARUYcVtkjgBoMuC8I5gN5iqNfmDi19/fX/88WtwQc9/7x45urn5MtHfzHECy+ocS1a"
    b"GsP1Iv7/weBf6zglufY4CTwUQpwQxHcT0QoYB0C0BPAiiOYA8gFIEskRKxr0xJ+JgomavSb58gfbFZVMxuzGk/zcxGcaJ6PB"
    b"jsy9rRZV9nfCtMg1m17RNTYLP7afyGKR6hYAKJohLpsNCNS/hvvXU/Z1GJ5FM19E9t8LniOzQZNmHhTanrCXOGl7bhNiMEIw"
    b"9yDEDoBNEN8BxC0SuAHQFYAvgvGWgnrt/H/5/LnBLZ591sPhw4wXX1QAeFYbgMic9HOo1+/2lXpaSXyIQR8jEk8SETg5Rpw/"
    b"Pclm4Tic/oUbQNFEttkAyHYRFNhBjhuA5nqnDYAcTmnd2JQuOtsT0MH2IuQH23doQG6V0IIDCskhN8M87G8yggASkd1SQEgB"
    b"EhKq10PYDb5PEl9ihb+RYfCdneXalauf+dzOEBlA4AWoWWwAMt4AeHERB7d7/sfB4pME/DQEFmOLpXYAC+GhadCpFIam5/6M"
    b"YH9qMtraXmCHlatQsAmNBPvNcLnU9tRJ6mi77v2l/tMSbmsX4ngWf972ob1kOLTSB0KB7X34QwCYQ5BkQG2C8GXB6g/95fpf"
    b"vvm7n10Dg/BJCLyIcJobgIxP/x4AoFb7BUnq15nkU8Q4DYFm9OAxzAGpwX2Snj45wn6qeFomYwFlC0A3ebQnis2iqrjgNIvX"
    b"/hrHk7tsQynYwKw2FKq62Wg2/oLnTC/+glPfAink5qFL7IEsTn29y8OJ/+R4I2GABAlBwvOioEHQa0HIMwjVK1DiD878wf/7"
    b"ZwCAT33Kx/q6wosvhpPcAJIgHmg0TgqEHwLwmwB9giAAYgDoJk5/So1aFbhcthhNG4Al7P+R3ADIbuFYw/4qG8BYYL/lBkD6"
    b"QOlUNwBb2O9mO8frLQSBSYiarNegegE4DL8A4t9Xiv/m3P/z/50fXMyD/53IBhB9fL1+mqB+i0D/lAgr8YMqRLsAmfxeZ7is"
    b"hVVFsF8TxLGGvlXQxpgWv84OJ1fBNBmnCPurwO1ckNGA/HYt7E9ulpawX5sFKrI9uyGwghACBILiGyTw7wjy9975D589A4DB"
    b"IJcNQDpG+tlrNj8I5n8tgF8mwqHBU1Icm6fM7ikonSpJTuacz0dp5zdeiKbFTzmfPxmkI8cF4LD4C+wgAcPvKL2QEz+n7Ci6"
    b"Rmt7YkIlTm472ylxPdktfp3tgymQeH+2tovMfKHsxm+2Xbf4c8+RSmEYbI//TjsPqWD+CkovUtJnEVIIVOh+Z7Kd8sGg6Ech"
    b"hAQRzUOKR1iFjxx68uEzt77/1mUAAh99nvDSS2NBAP07KwCQUn6MPe93CPyr8ZU9gER08sePTNl0iCVcTqRswJr0jS3sJ9t7"
    b"GiAgWdzT9Jkim++1OHEFxZ4T0ukuW9gvDKjAyuUxQHtb20Um3ad1wUpsRwHyQwnPQWjSlcaTOLG5c/7kdw4+Zm23gf2Z5y1E"
    b"vdC4NWIYGSRAAaRE3fc5DAGIP1a93u+e+U9/+qXBbGJmEPEoCGCQ6vOa3t9j8v43Av8yCCry9akGgqD+Oa05icll8VP6RDMv"
    b"UspHyXObpOtJrEEbmb+z8/ntFz9RIh1k4/PrTqMsYipEKKRf/P3TKAVrLWzPpUcdFj9RMfIzvMNi200nsYXthpOYrH3+vB05"
    b"NJXbGEy2k4H8NfgdgUiASKpQdUgIIXz/UShePvTUI++ufffNK7G7LvDSSyNvAIx6/T4w/nci+jkQefHbk/GT5CBoepEWwGWN"
    b"q1AOl8kO9jvlustPW/OGYvb5TXYQ9XO/jnBZB/tL4HKx36uDviW2owD2Z4KnpHEV0q5GEfR1tV3/HOY4AVksRL2bpHd5TNB+"
    b"6Crk7llkR2ptmH9HgARBgBWE551SSh07+uTD37rxvTfXcfiwwOuvc9kCN0H/iNa7iIME9dtE4mcB1AF0QVGSb6TTEm6nZfrU"
    b"zySYS5BGajJmk9ME44LTbmLIZi/111ARM84QaCqH/G6npdb2xOLXn3wlthdNVF3mpHBxFJ18FU/LVDiHjCe08dRPOwX2i1S3"
    b"oWj4LaW265BpgR0gIjAzK+5CUB2En+1I/Pbdv/PLB/HiiyGee07GiUVrBJD0++d84f8ig/4VEe1DRPf1Bid/QX7dGfa7bAA2"
    b"LLeilE9mLyjPMLhF+6tEra03AEI5M26mLDfXe5an7Yg0Jz9QwvpMjLWOG1KaddLECQptN7E+K9huikkZMwwRplShCknKGis8"
    b"4PX4ndMfeODdq//xs93IFbBHAHIQ9KvVPsag3ySilcQBRmXMuFK4nII25dA3jSgS97bKMGgmI8Ehw6BJ+fSf1cl2ypBVSuCy"
    b"znZhB5fHke6ignRXKdx2iLIPUbUGhYgKcNm0+HP8flPWCekNp9A3NyCKbHzKmGHQ2JGbU2bbM+sp+iciCClWJInf3OjVPwYA"
    b"eAHq2WeflTYbwCDiPz+PwyD8GoM+HP9GxQG/XLqqMFWig/0ZGG/tf2cCZpXSXa5BNk26ywou6079MrhcZnsZXLaA/WkflIrt"
    b"oCLbLYJs+olasBDTpz7BxlWg3Oms32gt/G/bRaqzgzRztDTASChfT5a2R5uFgFIKAES9/mEQ/9rp3/6VwwDw0kc/qnRZP2E4"
    b"/Wut0P8oE34SRE1gwDWmShSiUb9oxtdP297dYDu9R8drrz1D8swQ/f/ikKRogugnicVHH33uuRpeeEHh+TwKENqPq9fvAcSv"
    b"E+holEscpvqsYH/Kj0zA5aJ0VxmisDlxi9Jdgx3ZMspe6ve62F4Cl61sN51GGpdHdyJpg1BFaMMmyk6DMbV3FUwnsSZD4HwS"
    b"2xByCmB/2SltsiPrmlplGGzcaEvbUzGiiI4WdrtMxEcl49c7S7gHAJ65+lApAggACJ/U0wR8FIQmCAFRIv9mC5f7k1GYJ6o1"
    b"y81ywdmlu0pILln7dHDbhh1m8NlLbc/5oLCC/ekAI1mkuzJ2gOz83kKWmwszzgD7XRdictOwtj3L+ixYpEV2iHzAzonhp0l9"
    b"6/gthbbnGa8EArHiQNZqTWZ8VJJ6GnhevPx7vxeUbQCMRuOEAn4KoOVhVVI+e7brYSTN4Hoa8dlHuZ52wXhN+13Perx2s2tH"
    b"4GhzF8uK+adO/earJ3SsQC+XAlDqCSb6CRIICSSHf+NaEJN5QpeCmFwuGHbSS6URb/u0nZ6iamk7MpwBbVEI8gUxpllNsDz5"
    b"82kt6wq7SumuIp5DQYVdJj+es724IEav8ORqu634S5kdZSnuopSldu5WsF2nqcHscRAAQCik/AlPiT8HcL50A2CJBwTEY6BB"
    b"QFA48eGrloKWTsbxL36TqEMl/TdbJSID7DfmpFFl8dtoKzhO1JzfapuPL0k5uvLhc2XlVWy3FH/RLTgj67PADkN9SaVNyLak"
    b"noTgkBWEkKLuP4Zu+IBNGvAIMZ2CIC/+JHZi+GGUxV+B5WYVaNLHHnR+r9l3M9juxA7T2O7EcrO3vZDlZuufkmECj8yMo6Hv"
    b"6sqHNwXJivLxpnnoakdhrMtgB8z1JfpqVAMrsDQGZnrvxBAEWfM9QJ16/H/99SOFG4Cs158k4G6gosbYLP3Aqj77qNfvpRjH"
    b"booT0BTGa9T3O86YEKY8txPXc6gUGHcHIZ4sdAHIoydZ8QoNYT8Z4fLEZJ9H039zl33OnsSwEKG0038rjD2QLt4xCuy3sMOa"
    b"hlsR9mttN+kh2tpOFrDfYHsVzUdnOTk3VyGX/s2Fe3SorSK9nqO6emYWRFgJow3gC+YYgMIjBFrOPunYOOGWvp+92KSF8gqq"
    b"yD9ZbEJV9d+IKi1Ee9UZvQ59ca27xULMbPLltpsWYhXbC3odOCsv29meq7+AnR25dzCJngs2iz++IQGAUgDRMoBHioOARCcB"
    b"7MNu/noPM71m9qyEvfM1bntpD42X6b7MYGAfEU4WbgBRxx5e6B8Z1TTQimDOZGSfiyLN1SvTqtheUf+tEPranrimdFcFV8HI"
    b"UnNAIcbT38L20qxTge22J24B2nCG2y6wv8rpX/gc5bYzAwAtgHGsOAZAWAGJucQA0G6A/enFb+O7JSaHdU7asAm55GUNi7c8"
    b"H2+G/cZ00AjpLiv/W1N27WZ7EVw22D6VhisFLk8BP0XvSiTn2XgXvtUmVH7PSJufGUSYA2GlxAXAAjTcgD0f8Z42y2yvjlcM"
    b"A2fO6ptldmJa4zXOMbb4LCbyCVgo3gBANdJRvUqZXkW7nSvTyw72l6IQ0o+UMwrBiCw3wEX2OXPoupJriohMFYKPOojrgkKA"
    b"Ssy4aTdatROetez76OqC2NzThKaQnWemXouxdJhG/8PL/JFwZ7lVh/35hWzjKljEHqqww8bFcrNcpMYNpZLqzG7ss2hp+zRh"
    b"v5Ptu7HPYhVXI2F5jp+fRwDIjJqbz5/8HTsEmsZ1WlaRf0IBy81lIWpPqRLbYfCzYWFH1dMye2KQzWmpsR22k3E8p23qd1xw"
    b"T5MdsLXd1PU3s4BdFyJG7LOYW54ltifnKMc9fTSugjcxv3cv+oGz8iOrXD8OH3KvVv+NY6x/1KsHc3NL/wGaDcCV6YUK5JpR"
    b"ikIsfP5K6a4qtmcERqtG2a0JORWKQnZTo9VRWG6OJ+7oLdlHFJ41wf5Z91ksdQGqNDkch+qrbaBIS7ksCU6VstwcbbfNSTu3"
    b"GLfchMrgctlmatOWvGgzLWW5VbF9PItfm6qr3GcxEyCeeDXqqIu/vM9i9ktgll9/x3Lbe/bSHrP37+ZH4ZeXBwDjZbkVas87"
    b"FcSQgeRjEZkvJMfYZBjIcBqVwG1rlpul7RVZaqPCfreT2DXKXnTy512FYtifh+2ThP3pzzWtB0fbdUh5DO3VTftDJg04Lthv"
    b"8r+LHtYV+pbA/tLJaJthoJLJaB50O5bbqLB/BL83l+N3tb1gM508y220xa97Dte25BVgv9H2yu3VbdxozTX6GMCEI7g8QkRz"
    b"1AjwXox4z3K8gN3BcpvGu36vsT5tswClOnYFJ6729M/q/tkG4LK5YCvYb3MaFUTZp6n/ZuPylJ78eVchN2pVYD8mnOsvY7k5"
    b"aUGOCPt1fR9dTuKKEnSp9+SqqVHqRutP/tIsQJrp5QiXTZOxUkGMGS7vSpabodegdexh6n0W3VyNws20Sp/FkirQyS3+SfVZ"
    b"HBX221WzFpG0bIrrShFAeXDK5sTASKel9pTSpQGNIhhkhHikzY8WpYNK7HDxTyufljCflmR5Yug20+xpYm0HRjhtiyeq9WnJ"
    b"DqcldF1/YVeKbmDJWgX7dEjDmBaHPsBoEaQv0p7Mdn4vjwGMxa8hx793vD/Z+kXk9llUYTCm4gfS5GIcNMIDV/Kbqfr4jMNn"
    b"J8cPMN6TxvSuaSZxgpwLoGd6VSmIcSwKmZjss70QhHWU3bKW3Dr2ME3Yr4XuDukunR22UfZxwf7K7dWR6X5UDpetJehmBvuT"
    b"a9Ai41a0AeSZXuX+hrMCTOWFqNlsbBeArf6bbZDNOTdesAm5FIWUCm1Ok+VWTcdupIIYA4XayXZXBarSQ89ms7Fxo83j7a4F"
    b"iQL1rfSXGDuU3Uvpl2nbuxvuu9ueYbe/6x/x+eEZ/bJpyD5bFYUUwJyp6L8ViGNMW/a5ei34iCw3+9O/XAvSwVUYO+wvC8AV"
    b"S9A5pbgddCWquQoVtShKg4CVCmIqyD6PorwyBv03N1EHW9snKPvscs+yzdS1z2KZvw9YUlSr2F5RgcpaV6IK7C/pF2lJ7S4S"
    b"njW7Cqb1YBcfs88C7Fbovtf132ZVhz5NVt8oLMZRbR+nFiT2wPXjZgI6yT5nntiJ5ZbdPW117CxP3FIdO0fYr7W9iv6bK8tt"
    b"1EarOjtcT/+SwJdVt2dddHoSsN+kmefKtS/NOmXH28BFqYJCJilBV7QBVC+IGSfLbUyw3+DzFxJPiuCylaswTtnncXZZrmCH"
    b"VqoNFZhxhuh0Jb93lIIYiwo712pW6Fvb2esh2rkKxbDfEB8r0+EwIQD309J8/cj6b9qAnYWOXe4fi9NdRv23yqel6wlNJkHh"
    b"TBDNfDoP/F7Ostws7DCelnA8LXVIAeW2l6JPS9ut6ksyv2P3ZjO5OWotPGu7SG20IDX3tNlQKsUAaESnxZXVV6TfRhYfYNwp"
    b"p+XHjcJyq8gs03aSnRLLjUYcRBrhD6znx4RiWFThwrHEsMaTJ7QsBrKUXqos+zzOdBewa/TfpgX7x9xoVavYXLnR6qiwf1yN"
    b"VrPvqAK5pgBtjB/2G6D9qI1WyxHACLLPyd8xHJle5t+l/bSia1GdGZeq5rO0HSPKPmNMss9V+ixmF3/lIFm5r2xfEFOiQOVk"
    b"OxmqHSv2WURGpaiKBLwD7E+9Pza53gW2F7jRJTGAWUCjMaVP9pKQxG4QoZiVSMqsJOdnlX6etcS+dTGQU1GILTzcC7DfoUFp"
    b"Bi0UiqZMXALKHfanXQ0LuGxtewEzbhywf0aNVseiPG1KD85Cgq5oA6gU8Z4yy40MrZoKn3NG+m/VmF5F423DcrNweayViKqk"
    b"+sbFcsteM0LnKNdq1txCtLnGrr26VZ/FKoKv1rLzVbIAk4SCo3ax/Tv9t+lA0Vl0DX6vaTnOYH4UFAONqcvpbpV9Hkn/bQTZ"
    b"Z0yy0aqD7baNVgttr4CmrE5iVCfX6OxwIdeYgo9VUQgqoBAt6rVxNSwavZQigKmw3Nw3m9Eqwial/zZhlltVppdtZZpLe3Xr"
    b"dBecCmLGynIbtZrVGfYn3TJb4VmD7aPIyTkV11kiANLot/WhoDPLLftT2WlZFEjRpbKKNiFjugv2qb6xs9xKJofNiWNi6yWf"
    b"kW1TfWWnpWkhGvBU1R6TmoleGmTLLgDXPovpmeKuQIUCrcQivUvYKlDB3vaUwIvu5VRmAo6g/1bF5zamMMjdX5qIH0njvd52"
    b"jKp8livLblRmG40w0LPu+TAWn53GeE9ys70Ci9PLr3VzLXjq92PqeDJ9/TdH/fWJstxcXQ2LyrRKjVZtte9MeohFCMWF5TZq"
    b"lH3KjVbL6hfKelxMDfZrYla6DcAMfSsG2Yo6Ahf531WLQkphv0UJprXvl7Ej+Tt2DbKN0JY85YfaBMksFr/NQkTFPg9Glput"
    b"y+PQ66DQxTIViFnEHmwD44by8cosW6eAe+YqKybgrFMo44Jis04PvtdYkKO84/cKC3LWboqdLLg+ajxultvEhSCmqf9GjqSU"
    b"gnTXWGSfXQJwqckxq0arllH2qTRaLXF5tLZbNFolQ9BvYo1WCzIMVlmAsk4jVrLPlkUh5AiXbWvBtZMRU9Z/s2O5jVf22WJD"
    b"Keu4VLYQy6LT42qvXuL3VuqzWAaXy/osUtl6yG82lWG/K8vWspK3YAOYMuz/u66/s3WT9sJ47RbY/iOqfWkuB67ActPqv9m6"
    b"CqUBOFRgxo1L/02j6mKj/2Zhh3tpakHw0SUAhwqw36W0dYIsN2sJOit1KwvbXYOPKID9KdvH22iVDMFD62Kgql1Oq8J+Z7js"
    b"Avs1m5AW9pdG2TM6AQ5RdjKkuKpVplVxeUx+rxvsLxUJqcxys40xOKTIrCXgi2B/8vkdG61Wgv2jF9e5a1GUZQHKZJhKYSyN"
    b"11VwhUa25KGpSUhVxHN9e7Naf1WhpM2zFBYAjSB3NnboTW7PbkOUIYz2wHvMjfZg2DhKo+9G2D+BopCRYD9KYL8pyq6pGYB9"
    b"3t6oxFq10SqNs9HqKLAfbhkGVGi0qlX/seXaVxG1dc31G0psLWC/MaBdxHMocaMJBS4WXLIAZTDFqToLmIz+m+2CK2tFVhRl"
    b"H43lNj7Z53E2Wi1xecYRZbdiuVVxFXZjo1VUaO82Y9hfngWgmZyW+aKQglSfNdMr/hOeHsvNHCiqIvtc/bR0ZrllN+iJsdzc"
    b"gmTGhZj8HTuclmlM4bhITYFXuwWnXU9cfM9yLUhL2+GCAMbtx82K1UcjXj9NP27cPvNeEt7YDXp5mNH8oBmMV9kG4F4QM0ot"
    b"uAH2VykK0TRKLJVeMkagK7gKBf3rJy77vOsbrRbYvpcarUITiHVttOrSYXlcsN+6NRi55KQNwhqueVnXLqelTK9q+m9FLLeq"
    b"+m/utjvIPhcx0qyptnaw37SZlqXIjHGEUXz+ssVfWltPbgtOexhW9PlteCFOWpAWtru3BpsRNNqrXX9n2QV3Vm4KZjBee5H1"
    b"OQ63jCc7PzwbB8mp+YRzQYxbl9PSmoEK+m9FLLeR9N+sbR8h+Fh4GlnYUSXo56wFabB9TCe/U/BxlNMfti7PGButUkUJOhQE"
    b"yM0bQEX9dcucNEGAiCCIQCTivyFAxH/APPhzZoYiBjMXL7hSYQZySPlY2j5KTnoa+m8aOwgCJEQ8t0R83RDa8wAMDf8v+1mV"
    b"K9NGSpE5+r2F7xV27d208axqdtBgnIc/Q9t2LB57ApjYHCMq3KDshErcswCOUCN7OggSECQQcoBO2EGgeghUAMUhgDC5zUMI"
    b"D1J48IQP36vBl3UQCAw13AwMMQNnVl8V3DTO6ycKg6ONVggJZkZP9dALewg4QMgqGs94kRMIJCQkCXjkwRc+fFGDJAkmQLEa"
    b"AfbTaHNrnLDfZX7k3EIquYyGmyaGh1yIED3uIUCIkANEI69iYmf0f5IkJEl4JOHBhwcfBEANNgLL5xhdFtwRLpcUxBAEGIxu"
    b"2IFiBV/6WKgtou414Xs1eNKHJBmhAWIopRBwiED10A07aAcddIIWGEBN+CAhoO/2Aoc6+iK4DEdXgUzgCVYFMZhEo9X+ZAQU"
    b"QnTDLgBCXdaxIJdQEzV48QYr4k2XwVAcIuAQPe6iq7rohB10uQtJXvyOyLywS1RoZ9ZoFS6BtjLYX9JxKTM3ugjArOCTjzkx"
    b"h5qowYcHSR5EjH6JCAoKIYcIKNooOuiigy6IAY9kjNagH9MKRLtSF2CcXU5JRLsghz2EHODo/HE8cvgp3H/wYZzYfxqH5g9j"
    b"qb4fNa8BxSFa3W3caq3h2tZlXLj9Dt5aew1v3ngV271t1L16dJohdgtmqv9mob8+E9nnYbNRIQgKhCBsY85fwP1LD+H+pYdw"
    b"onkPjjTuwv7aATRkA4IEuqqL7d4d3OzcxOX2JZzdPoO3N9/C1e5VEBRqogb0kcAkYf+oHZe08xP2DL+qjVYTzy5i90pxDwDj"
    b"kDyI07XTuMc/hqPeUewX+zBHc/CFjxAhOqqN2+oOboQ3cDm8hrPheZwLLqDLXdRQA8UbNINH0NQwawKm/mnx9HG2ChQJ80Rl"
    b"AL7wEIKx3dnCXG0e9x96BE/c9QweOvwE7l46geXmIexrLGPOX0Dd8yFF5P4HIbAT7GCrs4E7ndu4sX0N59bfwes3vofXb34P"
    b"V+5cgJQ+5v1FBKoHhoqfa8L6b8KWE25guQmHPovJe3Lm+oJFxcwgEvCFh1a4g5AD3DV3HA/vewQP73sUJ+ZP4mBtBYv+Iha9"
    b"RTRkE54QIAAhA13VwU6wgzvBHdzuruN6+xre2f4h3rjzBs7tnEE7bGPeWwARIeAgjuEYTkvhwoyj3JyqxHKjEgn5Ai1JEkWx"
    b"Lv01yfnGguGRj5BDtLiNBTGHk7WTeKj2AE7592DFO4QlsYhFsYAGNeCTD0kCCoyAA7S5gy21hS3exrq6jUu9y3g7OIN3e+ex"
    b"xmvwyEeT6giS7rJAVCAWj50tz+a7n/5dct8AtEE//e+k8NAJ2ghUgJWFo3ji6NP4yKmP45njH8Fdi8cGLkEQ9hByCMUqnryR"
    b"yyCFgCCJmteAL3y0ezt4+9ab+OsLX8I3L/0Vzqy/g67qRKeXELF/WsZRL4P9VKk01ZprP4VGq4IEFIfoqh7qXg0nF07jmYMf"
    b"xAdXPozTC6dRF43IteIuFEewk8GDPg8EEcUAhIca1QAAq53r+O7Gd/HNtb/FW5uv41Z3HVJI1EQdCsqMpqgCxXcUCTrNSVwt"
    b"yFit0aoggS73oKBwyD+ER2oP4OnmU3ik/hAOiGUwFLqI53v8fwwexAAEohhZDT488rCjWjgTnMcrne/j1d7ruBxeg4JCnWoD"
    b"l62qBF3JBnCCyydjMeyX0sNm5w6WGwfwj574H/FzD/0Kju07CcUKSrFjTIigOETda6DV28F3r34Df/z6f8a3r3wdda8OT9Ri"
    b"WFoF9lty7UsXYsGgT1oCKvFzFGTtocsBnj74fvzSiV/BE8s/hoZsoBN2IIbHnNUXgyEgQCRwtXUZX179Ij5//c9xJ7iDBW8h"
    b"CuAafdApwv7KXZYrNFo1XCMhscU7OOQdwM8ufAwfnvsgDngHBpssOUbmFBRqVENLtfD97uv4QvvLeKt3Bg1RhyARBxuraWp8"
    b"9/l/k3oYmfyP+oF9n06qX1Ccxsid/IkIZh/2e8IDCFhv3cLJA/fh1973G/j4g7+Ee5ZPR6cTQnAMYSiRYqLEd9p3i/weIkBK"
    b"gTm/iYNzKziycBcUK5xdfxvtoI35+sJgE0htTpTJSScXb8LnSy3+wcNkbU9fnwZBlPH5J7j4c3ZEF/qihu1wE0QCzx79Gfzi"
    b"iX+Ix5afxII/D4UwRkmcGnfz2A9TgCJ2KfbVlrDSWMGCv4Dr7etY7ayiKZsgElCkQCRGWPxkt/h17zAzD/Pvt+Ad5t59Nt6j"
    b"RwrJayRFMalNtYV76yfx84sfxwfn3o8j/sogwMfEAHFqjLXjTgCIwcQQBEgSaFId++V+HJTL6FEXF8OrCKHQEI1o3KFJKybQ"
    b"J2nG5NpXvvnCeNOAiAaiF3ahoHD30gl84sFfwj945H/AofnDaAdt9MJOFHWO/UYuy9okItkA0Ala6KKNmlfD+49/GJ70sd5e"
    b"w2urr6DVa8ETXhQsYa6eYpvlV1WtEBJgZnRUC3XZwGPLT+Dnj/8Cntj/JEIOsRNsgeP3079N+dhT5F7GWYSdsAufPByfO46P"
    b"e59AK2zhi6tfwGawBUKUNmTiqdg78zHP8Pi7qgcm4Kh/F35y/kP4yfkPYV7OocMdhBxAQkLGF7HFY6TmPHfQA2FO1PF0/QkI"
    b"ImzyNs4GF+PsjCx8n9ZrN40A9n/afBoRcpz5eLcRUqIdtDDnL+AfPv6P8fOP/ipW5o9AqQCKA3jCj3fj/A5o+k7+XZ9HEHAA"
    b"SR72N5ZxYO4QVrev4fKdC/C92vAUouHz2rHDyjTnDFx7HdqgTMS/9DQqRxuFGozxPTuqg8cPPIlfPflreHj/I/CERFd1IYWM"
    b"Nl3NmNqNPcXBqhAgoC7rONxYgQLj3M45dFQXNVGLYW5WJr3E9pKTqtB2qh70o5SuBOVQXM6OeDCy10iSaHMXi3IBP7v0MXxk"
    b"/gNY8hYQcAiAh6nTEeY8ESFECEkC+8QSFsUirvNN3FS3UCNfU+ZOmXgW5cbu2peLEEAFlhsRIVQhFmpLeOzoj+Gn7vs47jv4"
    b"ELa7m4mXxGPZtAUIPe5gvj6P9x/7EC5snMHq9nXc7tyCUmGcJoRbZdqe0n9LT36GgiCJYwvH8fdWPoinDj0NX9TQCdsQNL5x"
    b"J0RwFsw4NX8KP37wx3F25xx+uPU2Qg7c/O/KPr9lOy6rghiH9uoaBiwDCMFYlAt4uPEA3j/3PtxdO4LtcCdx//GNfQ89LMg5"
    b"PEkP42J4GXd4EzuqBZXJphRShg2tF8UosKi/s2+2N3Bg/hA++sDfx8kD90EhRIgAIIYQlPFzqn9Hue2IKNTwG/jA8Q/jqbs/"
    b"gF7YQzvsQJI3eRg4rutHrNuXQqITdtDjAO87+BSeWXkGda+GEAEUhWMfd5BCSCEUKdwzfw8+fOhD2Ofvw1awFbHeRmZRTna8"
    b"xlZQE6+y7XAHK95B/Pj80zjiH0KIEIoipCTGNO79z1IUrag50cRT9cfwoHcvOtxFD0HEO7Cx3XioZnYb40lVEnC4Z/kU/t49"
    b"H0az1sR2bxOeGEIgGtM3CPCEhEKIVrCF08v34+m73485fz5OJRoCRibYnxilspOYUq5QOtdfDNF10F5DLDJB31zQbwD0wGDM"
    b"eXN48sD7cGrhJHbCbSiE8IQc+7gTETwh0Aq30fQb+LHl9+FY864CUo3B9mzQz9l2fdCuEPbDJAEfuyCDua1DIQRtGTgBJ2p3"
    b"47G5h+AJiTba8ZzH+Oc8CYQUoksd3OufwEP+fYNULWlhP/Tz16o9eCHsT0AhIRCqEJIYxw7ci0ePvg+HF4+g5vnohu3IJx8T"
    b"DMqnqBQECPP1eZzafxqPrDyO11a/i17YHbAF8wyvDDRy0n8bZ3FLxVRf6tEFeqqHBX8BD+1/GCcX78G8P4detwvE0eFJjT3A"
    b"qAmJlfpBPLj4IM7vXMBmsAUV+71R5qai5mOh7Q6NVqsWiOXGOvvssU8OiWO1o7ivcRLL3j4AQKCC+BkmOeeBeWrihH8X7vVP"
    b"4FxwGUEcI+DcyV/SSVrvApSc+kimUSjK7YNx78H7cN+hhyAoytsngx+T+iYQetzDvuZ+PHL4cexr7EdXdRPBxqxqsUVwynjq"
    b"o/x3RpabhQqM6eRLBKAoMe4goMs9LNb24eH9D2GptoQe99Kn46S+4zw1EeHU/EmcmD8eFbiw0s+XslRf0nZjYM6cqku9Q5gC"
    b"hHnEqvcJEmOte454zoOAk7XjOFY7GqVNSU1+3ONpEVKA/XIJ9/kn0BR19BBoSXrGNGfhBuDgFwlEzDPFCseWjuPY/hMIOUTI"
    b"oTPppEpgBALohl00/SZOL9+H+doieqqXf7GjipyMK3U0Nq2+6B96qofF2gJOLZ5CQzbQVd0hJXSCX0IQQo580qPNu3BX464B"
    b"s20QdxiHnz8tbUOH+SH6wVAAR2uHccg/gB4CKKgo6DrxvCWhix7mRRPHvbvQoBpChNBKlVl+eaYBKZN97ufdmRkri0dwZOmu"
    b"uN27Sh9iE/wKOcB8bR7H9h3HnD+HQAVxz/kIio2k/6Zp1VSeWpyO/hvFpdVz3hyOLhzFnN+Mkdc0Uuz97ANhpXkIB+sHo3nQ"
    b"h/6FsH88jVZHL4ipJkEXZUOiPNNB/wAO+vvRrxue9Nj3P1shxIJo4rB3CDWqIUCYqFUpr+Qt3gBKKLXJxRFtAAoKEvvnDmB5"
    b"7iB2OptgpjgKyhPfDwMVYM5fwKH5Q6h5dYSs0qk6clxwxsUbC5nEgibpAqS038f9SvuE/U6yz1TGh4+eU7FCTdZxsHEANelj"
    b"p7cNKeQEff/huHNcB7C/toQlfwFMPKSnlqowaYJsBfUl/Xw4YhIZxe8DyWQbRXePOPJDlYPSTciiCpQyTE0VHyz7vEUsynls"
    b"h9uI6v8x8bFH/N4bVMcBuQiPvIE7BqZi8VXrIKDLw8Q+RNNvolmrYacb/+uEXYBkYMSTHuZq85AyGoyUjtqocJci/YEQ4aB4"
    b"KSroYHCfftwX3iAJKSQkefBkVNQBIM1OHOdEQES/nvOakCKi5PYZf5Mf+Kg0teHVUZf1sbtVhKjAhpkjMY1kEQ2rQRFcX8hE"
    b"kIjENMiDhAcBipR1ihbkiC3AmqKOuvCxrSZ/2KXfO8MTEk2uR8VwI36el/OtbWWfE2kTKSRkrNXBmA78R7zzkyB40ot8sLiy"
    b"jdNdFxxO/7Qv1XcpJAk0ZBNiwKwTg5OhH/lVMYd+IPCgwgGjy95VKIfLQ7cmgtye9CCEGF09x2njjcY+eu8i8W+WpBQUS8gz"
    b"GAGiqLoHHzVRj0m1fY2JIb02KWHWr7MLoAZ1dpVgP2CssIu4/QQposrVvoQXTY3fHM15yTJ1T7J1ecpcAGuWW1ySTAkKLBHH"
    b"E3M6OyIRg0gNaKuD7ANn/DdL/TcGBqe5AqMT7EBB4fjCKdy//yGcXDyFu+aOYbl+EHP+AjzhIVQhWr0d3O7ewtX2FVzauoB3"
    b"N9/Ghc0zYDAWaktxMZSCYpUOkJIjXKZMlIz6YzD8nsq4xzCYKFGWGi+MIRS10LHL2C4omtRd7mAnbGNRLuLe5r04WT+Jo/5R"
    b"HPCWseAtoi6iHHhHdbEVbuJWuI7rvWs4372Is91zuBNuoimaqAt/sDnrF74e9pfHwCgz7tMrbyCk3/Ug3saWPSZLN4BRZJtn"
    b"9sWVI8BJ182XHnqqh+3eDhZri3j4wOO4b9+DeGD/Izi2cA9Wmis4UD+EBX8RdelBEqAY6IQK271NrHXXsNa+iSs7l/Duxpt4"
    b"d/NtXNy+gI3uBhqygbqsI+BAG2eYWlZiN3ylJiRDkIQgQku1AQArtRU8VTuJe5v34kTjBO7yjmC/tx+LYhFN2YQfz+4eM1qq"
    b"hc1wE7fD27jeu4FLvYs42zmH871LuBncBMBoiGakbZjUMKg81rR7xp5G/zujKnCZ7DOBwNlSYZryxCRD6idByLAN+hERumEX"
    b"ISscnDuEx5afwIfv/iiePvwBHGocjkQdwh6YQ2wFm9gMeBgBBkEIgZXmCu6eP4YfO/QU1jsfwavr38XXr72E1259D+vddXRV"
    b"TFcm0ii/WsDlJDIjzRjQDBZxLjvkAvsxQEc9FaImari7fhRPLTyNDy58ACcb98AjL3KpuIcAXWyoziCuEgVmCXOyiSVvEfc2"
    b"TiHgp3CxexHf3HkZ39n5Hq52r6LLXQjyIGONykqNVlOl3iXzb0pjr2e2FkvAlwYBneWS44i/mIkLkIDASZ+f2TLK3velBaSQ"
    b"2Oxs4vD8Efzivb+Cn7r7Ezi6cHfE/uJeNHEEQCy0/l5/W1QIwExo+k188PCHcXrpfvzNta/hcxc/i4tb57G/vjzwVyOZdEu4"
    b"nIy79IOPAyiqZuYCUDKSnoOiKFTcIUKsgNNGiABPLzyFjy9/DA82H0RTNsFQCDmIa+RFLFKiDxpyrDjBpHCicRyH/EN4oHE/"
    b"vnTnJXyr9TKYQ9SFjwBBRdifdhsI2BUuwODm7CB/bwwCulamZZhqswUANIwOF+m/UXIoGb7w0VFdbHbu4PS++/GJk/8Azx77"
    b"GZxYPAUFhXbQQhcBCCnmf2oDYe5HnBkB8wAXNGQDJxdPoiZ8+NLDFy5/Hj/ceAO+rGPOa6KnArtTXzNRKa72mwUASN0z3pNY"
    b"x5k3nEYMhqBIDnsjvIOmaOIn9n0IH93/LB6ZfwhNMYcud9FVvQFoJ93plhh7FR8ADEaNatjvLeEJ+Qh8IdGQdXxz+2XcUZtY"
    b"lIsD4lIu1ZedvVrJN8rFE2c19shsSFbNT8oQQGUfZNY7AFncOKddGXHqQw5xeP4IPnHqH+AX7v1HWKwtoRVuoacCeCKCjgXx"
    b"8MzJMJyc28EWpJA40DyIT5z4OUjpYevMHay2b6CnevGp5uo3l43BtP14d98zsluhxwp1UcPjC4/hFw7+PO5r3osAITbVHYhY"
    b"o7Bo3NOe0XDy9biLDtqokY8n5h7BnGhiR+3g++3X0OMg7kUj82lCF9aoiL9n6AJUfvbCDcBWf12kA7ksAIjYL55WXpQ5uqfg"
    b"Ye1CUhmoRP9NkMR2sINDzRX80ulfxrMnPoaFekQpZsHwhZebcC5fnvAiVICIsvzBIz+OrmrjT8//N1zZuYwFfxGczORaFMTk"
    b"Cm0Ep7+nNe4EQKhMFiABRQtgvyBCTzEUKTy98BT+/qFP4MTcMYCAUAVRNeMI4y4iiVMECCBJ4kTjbvz8vp+BIIHvtL4PYgEp"
    b"aKjXV9poVRNNp/Tcm168O74ncyIjYXn6l6cBbZhxebXUVPkipscDSJdOWqj4ZOA2E7BYW8RjBx/Hsyc+hnuWTmKnt4MBoB2D"
    b"HRz/T8BdHJ2/Cz959Fmc3zyH7WAr6tDDQ9nx0g6vSffFUGo9tQMoGYhKpcdQSMMlEBQzPOHhoH8AH9j/frxv6fFojNBLKPaM"
    b"4TmZEKAHnzw8Mf8obqkNXAmu41awHvH3+26jrfhLsoQ4+43pjn1aUzBPwDG7wXkgM5YvgV38lYX+iPL8271NnFg8gZ84/iyO"
    b"Lt4NhkLAXZCgsQlqEBGkiLT0FYc42DyID931EZxeuh87QWsg+VQp1TPjVJSo8DiCJHZUG5I8PL30Y3h04UHUpI8QARgKcoxC"
    b"JtEprxAigC89PDx3P56efwIeeWirNsS0mJO7+MvoAuRSTzq55EFEnQeklOkTgXhw3wQrYrAj6gs8okyBUiHuP/Agnr77/RCC"
    b"0FY78D1vkFEY5/rypESXO2Awnjz0BN7aeBPfW/teQh8eFpLXpCHQzJoIxIM4vE035H5jkSVvEc/sfx+O1Fewo7YhhBjoGIxz"
    b"3PtVeju8jcP+ATwz/wS+s/0q1tXtYU1HUdAv1xwVCRQwyyxAvw+CW5flUhegvDpLN1F3QRog+Ryc8flTE1AgRCQues++U3jo"
    b"wMM4PHc4CgaGJafxSI9KMRNQ4kBzGffvvx8nl05htXUNgQohZELvPQn7DZNRLys9XVTFlMwC9Isw9TnpQR88hFj29+G++Xtx"
    b"vHE35rwmNoKNVIHPZFxnRlPWcax+F+5rnsQGbyCMEoeDYGxZe/VBFoh4duOeCgJy4kArWfikLxUXuxVejgrzzRFoQqh6kCTx"
    b"wIGHcHzpBMAKFOeYJ6rpENOnFYe4a+4IHl5+EDVZi8Q8BjUDe2zsrRfBUMdgpXYIDyycRlPWoRBAJBKsk/oWcal6Q9Rwf/MU"
    b"VvyD6CEYpI7t5xdlFuAumOsj/L2X/zsydP3NBqaSUac0FJ+NC5A8HTmDXNIlnYEKIXyBk/vuwcrcCnrcGULVCT57v4Slyx0s"
    b"1/fh3qWTePnmdxD0NnJ5XH1hVl6ieuYuQOq5qbTEtschDtT2457m8VjqvTfxce8/YYAeBBFO1I/igLcf57qXUSNvKKGmaxib"
    b"RWCDf59hLUB/jZFNvUuxSEw+C1DKkMr7GyRmnwVILg5ms/5bNOlCCCFwdPEYDs4dQIi4SEdO+uXRgAW4r7EPR+fvhic9hCqI"
    b"EYAq1bGjxPugGWcBRFI+C5k0YO7ZI9tDhNjnL+Fo/TA86UWCFnIaMiYU6edJD0f9Q9gnFxGwQp2j6jrWnPRaQRnSeJ0zygIk"
    b"4yq5MtxSUpx2A0CBD5oNEpJeg25mMYAkgsny7NPULWYGQeDg3EEsNhax1dlCTrNuQo/LYCilsFibx8HmAXjkIeBI1IG4QEyz"
    b"P/ZMeQHI3VALwBl12gyKHMRmmTEvmzhQ2wdJhKCvITmFx2VW8MjDsr8P87KZFo/ldMNNbY8AZALgs4oBRGdFOmWaGUPbNOCP"
    b"DBPQZQ71qajztSYaXh2bnY2B4sw0FHWAEHXpY742HzPSHMaaDOM+iw1AwCi/qHv2PgKqSR/zXgMBhwhCBcLklYyiN6sgBdCg"
    b"BmrSj1QETBOn3JgZBgFLql+rMwEduq2KDDSdcQwgFb/gDG880xCZKSLf1IQHTwpAMEhMM6DDkEKgJjwMY38lHV4prUk/bGI6"
    b"2xgAJeIupMkCpFqfcfRrISR8GWkpgKf33Ijb0HtxC3rOAV8dkSlDDCJd+nWGxUCJLEA6VamLB1i4AEWwf7D4c3LJs4kBQMeI"
    b"0gYBEzAupf5DU/efkfMdKQ2jdfnzTNk1J1h3s4wBpMaeYyUmQ59Fjsebdf7zLJ+bEKEA1vv86eB3ulHJzGMAmWdKMYJN7MaR"
    b"XAAqPtX2xBflN5DZwWceccyn7HKN696EfC592rGLXTJ8s34XBZqAppM/cbKmdqA+K2yKxUD9exKn4DIPxAnTz5tt+pCCcFMU"
    b"dkzeF0WiDlr4loGpyTGYmg08XMSJwCsVNFodaLTM3HXhlJQZIau1mDn9DV2h0uM+zVXOqbXWr2UgmGE/ubgA1os/MRl3Qxow"
    b"erEDPJqgOGlkkXOtvTG7CHrymdgQ1U2ImKQZabOH0kgsJi5gpPVdAEKmxfqMxl7nOuZ9/qwd6Y5Su8EFoOzmqoP9I2cByGVC"
    b"Y8qjwXniUsmzG9Nos9gAyHGsodnMZmZDAsbzCPbPxAXg9H+7Pj8weyrwGDaeQj0AItJPOGSCaejXRc/ABRBpFyCnSJNbLJSA"
    b"rNOHoWkXgIdquqV13dC/9SQMnYULkJRWg1mGjSjjMewKFyA/3lrGawpx7h4XwBn2l7sA9rA/HQuYnQsgckUxw2pAMlZ0pX2/"
    b"WWYBUjCubPEnswAZ12tm0fTUIictFE2HOmh3MBgTNx66Jyhu+0ZpqrMYxI9m4AL0twONHkBRZWlpDIDYsrdacjAwex5Aqjw5"
    b"tYNr0Is2hjCLDYDTOzXrFj4AQ4fm3VALMDgVsypMuu7MGS7DzDauJIGpH5TMdbQhzXrIBJAxCx5AUgjXcOojs0PolL7NLgD2"
    b"5lfVTrKkphxB78NHVXms8wuGd/e46/zswRhMO3uRv582BGATz5p1+pUs4l7kEAMwtgYraqAI5MURZ9gXYJgGZEOH15j4QbNK"
    b"oWX99lSze43Pj5w0ddoPxe7IpyeFAXS6Epk04OzSl+mxHxZXUQ4u59dDRg5/lqKgGV+MOO0mjqc1WGHb5BgKiV1CBU7SgZFe"
    b"/JQNAibTamLGMYAEnM8FXnMbcHqiRrEDTgU0Z+EC9ItpOAVF0yIxnIkHRD0WaKZMQEouai5b/EOkNUwBzpAKDM6sd3MFLMbW"
    b"HbhMini3dAaytWFX8ABGhNtxUGvXyYKTpf27Uc580u9xHPcV43FJjMVAZQ0GBzsoY3eoAmf18gZ9H/LNTocXzzYNOEwB6pSN"
    b"UUJKmV0gM01G6ceaYkSSOUlTaCXHBJx2R6MsCzPfcckswzbMHunGfSaqwMigQouOx9YuABk6BQ9havJ3PHsqMGWiTalCm2y0"
    b"lFIR4f73VEMAib4KufEEzDnplAsQfRjPxJdOxlAo8QoMbc37sZeUC5DaA6c37tksgOhPmfIYGJMujjHN4z8ZPKXy1mBwzQJU"
    b"jHbSLigGYsdnn1kasL8AdA9Jtp9BoyC/8Rvk2FU6iSCmj1y4kDw6wmybagxwHO5HzgWgIkWabLAh6xLMUhIsRY9MFgMZmoRQ"
    b"MrDD032hSaSUE/qwgf27JJ+eEWIe1qVr3Jckz4GG2nbTHfvE/TIugEmPIV9bbxj7qboAWfUqcuh4XLABmGC/Xi55eONdoQlI"
    b"w9ouHhQDmQo8aLaBnNw902kpGBb/QFgjseBmuwEkCrAymgaFVNRdEATkTGrYJL0Ogx2zLQbijDAscloSuWyGFRMQNnzi/uBQ"
    b"rlMrZrijE2mq/oDSfnuzmoTZ1lpkQ0WFLiibjLtMc9yTG9dgR0o3jsmsulzx3ayrAZMxIm0/A2i7SxNl59+0Hd1huj1Nd9ec"
    b"+jmfwSYGQNUHdqpOqW4COdx7yB+YbhYglbev0oRQI3Q69WDAIBXF9u8pQRJKZgBmUQxEleebgYSF6W9i44jfeSYDy1JPyZMo"
    b"G0mfSVQXw6KOVGsw7ck/u7Za2fvqatHNp3+2BoOGY0/Ty2QMAtAiWRsSE5J0xUApHcPdIgjClj5/2o4BmknM+am6AKk5n3C/"
    b"2EBiGqk1mHHxJ2WRMymRWaYBs+3AS+yYfTGQjslV0uMt1bCCZ64IxKluWVTQkp3S7MyZBy8zFGw2LX5KC7FCR2WesgugS30X"
    b"cl4SG4OVC/Be+cqKWszivs7Qfw+Nra37NtWx5+rMUWMsZEZjO4Zxy7cGs4T9WXdh9kzAJIrhdGmtTtQhx+eenQvANqd/NupL"
    b"eiQz/ZM0QfJJ+vm6+cLJlPHsXQBO1WAkg2lZ2K9xFbALJMGSehywaA1m4wIUwv4UvOgPnMi9yFnpAfQj6Zz1gbKdjVNdIxPf"
    b"U0RxyXsmfTYyCjlQviWUmK0egMgUA+V7FyK/cfVtjdl3afHWKY47Zaor2bT4dWpSyNDIp18MlKpoLGoNVlIMJNy3nz0I8wt+"
    b"niYF2Blj7sWxpl3uxYiCZ6E9NvZjgHwlmoAaGAdN8cou6A2YzttyXiIp5zzFxUCYpQuARN0SFfRz0wSoaNa+NNI6hEllHR0X"
    b"PUkSmqULAFMGJj3uxZ2yKc9lmPJGS1l+TyHsr+AC2BUD0aCYguSwvdb00oAMEiq6L2UWuChgo8XwT4gZFwPFefQBialQjwFZ"
    b"EYE4Bcup76mNe//eNIy7kKa/QZKh2dczHKSOZ1UMJBF3Kcq4I1oqfKbFWeKawbhPNQ0Yv2cR19+kaNYG2F+gHuSZUVA5D92g"
    b"szGj9uBJX5ONcQ3KxAJ2Qxow51+aTv2UHbRrWmwNWrKnGpZkJmMWAWAXtAYzxShgsAP5eMxs+wIAudZggLYGxgTMPWufea/5"
    b"oFaH4Szoy5l70zR6Eu/CL8GAmmExUNX5vavWB438J/liIBsueo6ZRvnKtinHAJIMM876odpioLinQPJ7ams/0c9AFMF+jX+q"
    b"q2icaQwgwQQykVKy/QwoMeY8RQITJ+5LWdk4fexCKyc3y1oGaOJtlG6NkZ37Dq3BHBa/gVI7XRdAl4pJcLVNA5DR45+tJmBe"
    b"bz73ErXuy+yamyQ1AQdOfGr9m3xQQ1utGVQxJpuxkMbn18bAUlN/F7QHp2GRFWwreUtdgD39RdV305l0B34PQf6cLPiMkcsE"
    b"p9Re+rJvDWYIplEWEs3KBQDyLVMM9QAD/eBdUQw0DDIZJZxTSruUUWbmXUBnHmJQrbBGKiC4O4qBUirSw+6amtR3MixOGjm8"
    b"2bkAqTggZ9BtCaovdQEKF3/CH2LaHS5Ashtw3wUwtUjqcxjS3W1m5wJQliUHQ+wi8cZp1vn0lCz4UOJbt/gpszHQbisGQtL9"
    b"Kkh950JfaiCKMi1JvGjsVTznOR2DsajkHc0F0AQfspN6uhuAYxpG19pgphuAg5CetgEM7Z7egI5Qe+ZqxiO6CbsiDTgGVyXP"
    b"Ayg4+ZENtGVh6S4RBBkI5WRIM4kQzzBQmCplntZzD2E7aXLnxuClKV89iw41SS4YxzJsyHc5Sos1U5pBOJOuxho9gMEJXtJZ"
    b"B8iVNM82C0B9/hK0up1FqF63AZTC/ly0VFMbPdUNYHhfTqTIzMVAlKi+oxkvHtaLNZBZ2z2VOcjYPys9AK2whpaUktYDmKmS"
    b"USJ9mcsCGN1gFOgBTJPFodHAIAIxOy18qyCgTaMB0sGQab9QZIU1eEDtNJVDUu4kmgUCyPYzKFBlJl30R2PPlMd9+B+6tFRm"
    b"wXNyY5hxb8AkdTn1/JlFk6sFoLze3qwyBZTYwCos/vIYgGXrJ9HvuT4LDyBzbxe/KKlmPO0YgKCCDdThKBOUEQua4iEqdOKT"
    b"bGs/zywGIDJdjapmjwVNPxGQtGG0TVu3AZTC/rT/k45OzyILgFxt9OAZNbX2KeaipsXT9IOAmfEsa85KGUgtkPJpp50F0LkA"
    b"WlJKJnaRVESexQYA0qWvTc1Zs8VAybjLiCKjlceeh1ku0FCPodB90e8AnnaYyAb2D33uUIUIVThyRNIVzUU5fUagwigIlaKl"
    b"wtDsYfjyQxVCcTjcSJgmC0eHmhlQUAg5HL6/Ql160uq/MTNCDsHM2fZwk7Uhvk8IhRAqHbcw2JHUrldghKyGCwhTCATycPGG"
    b"CKGgMr4TF2gw5inBA9sHuvzTmvfxnIca6DFqYb8pnVweA3B/plavg1bQAUT/4J1CjSdFadBAhdjutRCoECLZb6tgPvWfsdVr"
    b"oxN2IQaBQDXZtxh/tBBAT/Ww02tDQUEI91pkIQihUtjudTBXa0AImo6qEXFE4SegFXbRCXsRJGXb5xboqgCtsAMpKB57NfnV"
    b"QwrEAEOhpUL0VABRacCiazqqhw4Hwz6DU4IAQgABFFqqBwWuaEOBC2DVYJAABkOQAAmB9dYmbu3cRsOrgYiglJr4+mcAnifQ"
    b"Dbu4sbWOTtCDFNEGQMrU4TX6WQgBxYybrdvY6u5ACIpP5elAOSkIO0EbN1sbCFlFG5cJ9usgNQFSCHTCHm62NrCvOQcpRRzY"
    b"mvS4R++dwbjd2cbtbgsCBDXoCqbn1HN8MgkS2Ak6uNXdxMHGAoSITrTpPDdBQeFWdwfbYXe48aYCxWZhDSZAxL9f77WwGbQh"
    b"BEGAoaa0B0gidLiHW8EOAhVCkoiyWsz6LkAlxUAiHwIgI+RM+tMc7+aCCKu313Bt/SYontwAa9leY/0Gw5cCO702LtxaxXav"
    b"Dd/zYijGmcU/9EGZGZ7woJhxZeMm1rY3IIUYwL9JPnN/XDwhcLu9hct3bqIXhvCEN1g8OsHHofjH8GV60sNO0MGlO2to9Trw"
    b"pUgw9CZpw/Adr+7cxs2dO7EupMjlpClXsUiQJHC7u4OrO+sIVAgvXoTTeG5PCIQIcb29gY1uCzJ+bn0MLBsniDYxGaeRVzub"
    b"WOtuxQHwyc/5vqvokcCW6uBa5w46HG0A1HdfBLIst3TxmBHPVD7JBKSQuLi2iou3ViGlgJQyJoVM2g1lNHwfrW4b76xexma7"
    b"hZr0ra6VUkIx4+zaNaxu3Ubd9yIoCtaXeo7tmyEIaPg+bre3cHb9GnphAF/KvkdqHcGtSYk73TbeXb+GdthFXXrgiY98dJJG"
    b"713g8tY6rmzfhhAiOol0qYIMgvGlxFp7G+c216CgUOvPl4mOO8DEqEkJgHF+ew1r3W34QuoXgIHxygCkkACAyzsbWG1vwhdi"
    b"gGKmEXypS4HtsItz7dtoc4iaSG68VR2aTAAwx9nOQos4+OYJASEE3l69hLeuno8nRzwYNNlvBsOXEus7d/Dq5TPY2NlGzffB"
    b"fdkpDfccceNQ3/MQsMKb1y/iwu1VSNn3RfPlteP87kePPV/g2tY6Xr9+EZ2wB196EYQrQWD9acAAap6P2+1tvLZ6Ebc72/Cl"
    b"ANN0xl2KaEG9c/s6zm3cgCBKLAJKN9FA2gWoCR+rrQ28uX4FHe7Bl2Li496PmvuC0OYe3rpzHdfbm6gJb6DJmG1cSpodgBGd"
    b"wATgzNYtnNu5nUjpTmHsieEJwlqvhbe219AKeqgJbygrj3R/RpuaAJE/ZbJ0U71cMjMgpAchBM7duIrvnX8H1zduRcEV0a9V"
    b"nwyMFnEMYqvbwpkbV/HapTPYbO+g5nkDdep0ff3QDgWGJyVCpfD26iW8cf081lubCDkc5FYnBUEFERQrbLR38MO1y3jrxiV0"
    b"whD+ADVpZLVEkpRCAyhakx62Oi28sXoJZ2+vYjvoRGMDTG7sqR/ADLHauoPX1y7j0uYtSBFB+0EminTNWSMf3JcSN9tbeH39"
    b"Cq5sr6OtulNxXYgYbQ5wtbWBN+9cx83OFjwph65LKtVnatUWHXogwvmddfxwcxW3ei2EUAN3YWLjHs/9LdXFufY63t5eQ1sF"
    b"2rmTg/32LoDJB02k/bT5RMK5m9fw12+/ila3g8XmHEKlxp+WIiBkBSkFFppNvHP9Mr559k1sd9sgkcdt5rbmcSqOGW9cu4Bv"
    b"X/whhCDMNeoIOUgUCI3j9InuFXKAhu+h7nl49dpZfP/qOXRVCEEEgshtwP10jik41d9QtnptfPvKOzizfh0LtQakFHGKbbzj"
    b"zgAUKyzWGmiFXXzz6ru4sHnLKESR90FTswxr7W186/pZ3GhvYqlRRxSfV2Mcd451ShQAxpJfx1pnC99eu4C19ra2TVaR8E12"
    b"2jMYZ7dv4ZX1SwhYYd7zo3GfwFfICh5JNDwP72zfwg82V9HlUBOwdGcFCpdJkC266cPR/fMLuLm5gT//3jfw7vXLkELAkxIQ"
    b"GKt/x2BIKeBLiXavg6+98wP87Zk3UPN9NGr1/MTXVi4SmAFJAkuNeZy9cQ1/+eYruLG1Dl9KeJ7Itwwb0e9nUpCegO8J3O5s"
    b"4cvvvorXVy9hsd6EJyWULolMZZOC0fDq8KWHb116F9+49DbaqgdPytgfH++4gwBPRv7u2Y2b+PL5N7De2cZSfS7ZE6Q0iBGC"
    b"Me83EIQhvnb1Xbx260oEbePPZlJj9PsVhCB4ksDEeP32Vfz16hn0OMS8X0eY1Aa0LCfto4VFv4Fr7S28dP0MrrXuwBMUBUfH"
    b"OnciV0sKghf7/t+4fQmvb95AQ/qoSYmQRyMgyOR/LJ4+/uki2K+TTgIBnpDYarewvrOJhXoDh/fvx/LCQnTygSHEeKCQ4hB1"
    b"30er28E3zryBP3nl6/jBlfPwpQ8hBJiHPiiJvF+XItcQQUqB261tbHXa2D83h5XF/VisNwc7OZn035xeYLSA6tLHWmsLXz37"
    b"Gj73xndwaeMmmn5tGMDJtnoqPI366cxoM7u5s4mQGYfmF7Eyv4i67yFQanzjjsh/JkE4f+cG/uLsa/iriz/Edq+LhuenSVgJ"
    b"2G86jTwh0GOFW51tSEE41FjAgeY8fCEQcOQqjGPxKAC+kAgBvLlxHZ+//CZevnkp+ncpo/kykCjTwOUCO6QQ2Ay62Ao6WKjV"
    b"cKgxh4VaHUQMFQd7xzH2/eDldtjFtzau4C9vnMG59gZqQoIS1YBFsD9p34XPfvEF8wZw74lP5xV2yNjhNdkkgUDoBQHO3rgK"
    b"KQUeOnYP9jXnoZjRC6O4gCCqNAh9lmHd9yGFwCsX3sX/9dJ/xysX3oWUEmKQxtMEoArjGtEzbXXbOL9+E/ubc7j/8FE0azWE"
    b"rNBTIaSghICIja9Kg4BfwCGEIDQ8D4oV/urM6/gP334J59dvoOZ5UQoq1zbbZjImNrOY17C6fQc3tzdxavkgju1bhiBCwApK"
    b"ccIGx8kHRqAUfCnQ8Hyst7fxJ299B59793vY7nWjCHiyt2HS5zf6oNFG0n8Fl3c2sB12cN/+FRxsLgDE6HEYuzhwHPvhognj"
    b"DENNSlzauY3/cua7+Nr1M0P+SqLNtz4NqEuhJd5TXIfRViEutTbQkB7uW1jGvOdDgRGwqjzf+yxLIsQLHXjlzjW8ePl1vLOz"
    b"Dk/I1CZpDfuJyjaA45/WUghL9NIVAN/zIITE6p3buLl1B1IIHFrch5XFfajVfACMUKlUyoK00CVOqPDQxWj4Ppr1Ona6HXzj"
    b"zA/xR9/+Kl568/vohiGWmnPxiW2SADOTazjeVELFuHbnNu60d1DzPRxaXMKB+YUoPccqoo1m678pW9s+hGz9ezVrNTRrPm7t"
    b"bOKrZ9/AH7/6t/jOpbOQQmC+VkfAKhc0K5Nh0xGDGr6PrU4H17fvoBsGWKjVcWh+AYv1BkQcN2EuG3ekUlnM0Und9H14QuD8"
    b"xho+f/Y1/Nk738elzduYr9XhCYlQQ6EtnZBx7rwmPdzutHCjtQUGY6newKHmHJp+LXpuqESdCQxKyDxAWv3/X5MSTS9Ki/5w"
    b"YxX//eIb+OKVt3G728JirYFkHX1x7wWDHfH/86UECLjR2cZGrwNfCBxoNLFca8IXUVZGJcaUCqA6J94AAWhIDw3hYSvo4uXb"
    b"V/HfV8/g5Y1rYAALnh9B/2zPy6INIJ4zxRvA6eOfzsF+EyklQQzq89JBwEJzDqsb63jj8gX4UuDI/mUsNpsx24vjhpbIt1bK"
    b"NBvqp1cUK/he5GJ848yb+Hdf+wK++INX4HvRphCoMB28pHzNr8mOPrz1hECzVsf59Rt49+Z1LNTrOLK0D3O1OkIVDpiCtt9i"
    b"AJsF1nY28dKZ1/F/f/MlfOfyWSw1m3EWgt0Xv8ZVABEUMxpeDUzAmzev4tbONg7MLeDg3Dx8T0aMMaERsTCMe59oBUFgKJzf"
    b"WMPn3vk+/ssb38KNnS0sN+agwLEcnCHoh3KOugIw59XQCnp46/Z1dMIeDjXnsb/ehBQUsyTJOUskidDjED+8fQOfPfcq/uzi"
    b"6+ioEIv1BgKOahf6laAwoBWTnFxS9ZjjkuI5r4ZrrS2c276FmvRwqD6Hed+HYs7Xn1mMfT/Fvhm08a3bV/DilTfw7dvX0PQ8"
    b"1IQcLP7hGiyG/UmEeeFPijaA+45/uhD2J3PShsko4jTVTreDi2urOH/zOkIV4si+Zaws7UPNj8g6ihUUKwQqRBj/zPFpIkmg"
    b"WathrlaHgsLrVy/gj7/9dfzXb30dr10+D8WMmu9FtGP0fUZNz+wydmOifp1EtIndabdwfv0Grm7cgiDgyL79ODC/CF9G9OF+"
    b"IUvI4cCGPkdbCoG5Wh3Neg2doIvvXD6D//q9v8XnXn8F525dhxQiyvlnIuZFsD8dgc4o7iQmaj9joJhxY3sT5zZu4nZnJ9rM"
    b"FpYwX6tDCIoRTd8GjsY9aqQAISg6PWs11KWHtdYmXrrwQ/zRm9/B1y69g/X2DmrSgxRioEFp9JV1k1Fz2kabK6EXhrjW2sSF"
    b"rdvYCbrYX29iZW4eDW/4nrn/3ODBf5MgCCFQlxJNz4cvJS5vb+CLl9/Gfzv3Gl65eRmtMEDNkxGLr7TjUvZETcN+0syjfgJq"
    b"K+jhys4mrra3AGKsNOawXGvCi8k6jP78iVwUTlCUBUWn/pznoccKb27dxJ9fP4O/uHEWZ2K+gU9ySEnWcHOMB0kCOl74k798"
    b"YezFQMlrAhWiWatFVNv1NWzs7ODaxjreuHIRT95zL44fWMGhxSUszy9ivtGIIKaUUCqKFex0O9jY2cbF9W1c31jH29eu4LsX"
    b"3sX3LpzFhbUb8DwPS8059MIQSqmhL1TxmfuFXIoZC/UmumGAszevY6O1g8u31/CDaxfx6NETuHvfMpab89g/N499zSbqnhfX"
    b"FCh0gwDb3S7WW9u4vb6Ka3fW8dbqVbxy6Rx+cO0iVrfuoOnX0PTr6IZBteKRZK096VNFggQW6w1sdbt49folbLRbOH97DU8e"
    b"OYHTyys4srCEpUYT+xpNNL0mfCHjGItCOwiw3etgbXsLa61tXNm8jTduXMUr1y/i7VuraIU9LNbqICEQhGGp0ITtnApZwRMC"
    b"ddnAZreDl1cvYq2zg7N3buGxg0dwYvEAVprzWKo3sODX0fR8eLHjHrBCK+hhu9fFjVYLN9vbuLi9gTdureLVW1dx9s4tKDAW"
    b"avXYL+dCGG6V/TJkZOakjx4rXGlt4k7QxWp3C29v3sKDSwdxV2MR+2sN7PPrWPB91IWEiN2QngqxEwbYDDq40t7CamcHZ3bW"
    b"8cadG3hz8xaudbZREx7mPR+9OLUuKq7N0l8f/fiH2LYYyNyEYHiNiMPQ7W4XgQpxauUI3nfyNB45dg/uPXwXDu/bj+W5eTT8"
    b"WlTZ1m1jbesOLt5awzurV/DaxfN49cI5bHfamG80IEiCiYfR/mzuObcTFtihuaZ/ivYVg7c7HYAIDx45iiePn8QDK0dx8sAK"
    b"VhaXsBSn8EIOsdVpY3VrCxdv38Q7q9fw2tWLeHP1CnqhwkK9DopRUd9NquTzG39HmlM1YqspBrZ7HSzU63hs5RgePXI37j2w"
    b"gmOL+3GwOY85vwZBhE4YYKPdxurOHZy/vYYfrq3itRtXcOnOOjwp0fRrUZ7eIDtlD/szfIDMNSJGCl2l0Al6ONicx2OHjuKh"
    b"5RWcWjqAw3OLWK7PoSkj4NpSIdbbO7ixs4Vzm7fx1sYqXl9fxVprGw3Pj2nGSKVZiczjZoT9MDQ7yVwTIcmIkN9WPTADJ+f3"
    b"4eF9h3Df/DKOzy/hYH0OS34NPkVQfifs4Va3haudbZzb3sCbm2t4Z+sWOirEnFcb9F3sp2LJIBtv5jGkY2Bf/Sf/yqxBc/cn"
    b"PsRmDQBTZZo5ONUfqDAMESqG70k0a3U0ajXU4pO/H9Hk2N8PQ0ZXBej0emj3emj3uhEDS/oxw9Bm0ukr6Wxbn0WQEwjCqG68"
    b"7vlo+jXUfR91L3ruqBIyokQqjqK+3SBEJ+ihHfTQDgIIEaWhBCUgszZAY5qMyDWAtA76xD52wCEIhLrnRTZ4Hnwh4UlvUNnW"
    b"J/n0lEJXDW3oqSg/78UbCjtsQqTrSqtb/JprFUdQXwiBhudFOW/PgycEPJKD5+7D6UCpaM6oEO0ggGIFKb24aCmT/oXDZlow"
    b"77WCrQl7w9jV8kmiIb0oIyEiG6QYuhL9udNjjsZehejGWS9Piogkpt2E7GF/8lD/6j/5l1TgAhSc+jnsTDlKQF7jKvJVPN8H"
    b"kUDACpudFta2tyLfX0X+p8Iw3dMnEfnSixZerR6JjjCneh+4LH5rtdSY49wXFG74PkgQgjDERmsHve0QPaWiWAD3xSAo89wS"
    b"Nc/HfK0WBehSmQ+yP/XhdurnmV4RMcUXUb67pxR2WtsIwhBB7P/3F3//+SUlbJAe5v2IW6E4kSEwtpyilEqwSfSzUHU6/lNf"
    b"CNSFBwVGOwhwp9uNYxYq9SxEFBciRRTjmoiCZEJEqTiFYQUmDAuWcv6gTebLbHs/femRjIRDmLEZdtHrRbGLkKPelZxQ1YrG"
    b"XcAjgZqINoz+Bs42i7//O7bsMVkYA7BxFnJ+EWkv6LeMCxUDFIKEQKNWx1wtKh5J5+3TGh7McdAnRSmm6pwn2wsTAxuyAqlo"
    b"ks3FUD4ZcBueiv3FRMPnZqV9IfbPOyKXN44mhyoqjapJibrnDaLGlJC44gTLrV9RyHFsIBUIs/WVsxRyx1ekYkRCcc1D3Yvd"
    b"A8Mi6CfP+s8eDBihBcJ/ZPGz7eNnuh9xzHiM+FGEhvDQlCKh35g8OTmBrBgKNGQogmA160dQg/L0xUDQd0YtPY0Kdh6KFwdH"
    b"BoehPrdqaixpLtDI/qiBP04dj/NMPI4RSL/apfwkdryn7lTViTrYfqbmxFXMQ2vYQgKe3eGyMUZUyfbhhkSMYT492e8hZQcX"
    b"Qt9CkYzSa8gA+8tdBUZ/2igopqFeRWqBJ0kanF+DRbC/UEVaY0cxAjDA/lKChOHG2clRtOCgPy3LIKO+yMHNV86ebuX5eMNn"
    b"ZjdL12BfWWswC9ivfxema2wk4GEmWRXFiAgj2E52fq+2I0wJ9E32WSyFy1RAnqLibsjG9YCSTtAOtpOt7VVbg41aUTaqVhqN"
    b"+Pc04fvN2t4xpm9Hvh5jGLtpPv847kdTnh8TmNteflMzw379aVQM+63gtg6mkANkzO6shmtcYL97uquKq0GZrjQohtvOUd9q"
    b"kWJrFFKSInO33YQoUEhvTcWIbO1wgv260vgil8e0HixOaVs0ZW07aev7tBuAk8+vG0BDbh3GeIF+AO1y47q2TRVhPzmmiQo7"
    b"yNjYbpqMFe2onCayhcv6dJcxNVySdjMtRGs7SrsnlcQeqixE60VqWIimPg9FtutcU2vbDeup1AWoCm1GhVY84vV7CbYnpcvH"
    b"4XZM295xjBfvkXc9y/kxyc8yZgFGgf2wC+BZB1tMBTGabq2FpBSrjseoxnKD5YlbhmLKouxF42kDl6cG+11JNwWwP/UsejfR"
    b"fKpWsV1jXylCsURTxkYvxUHwarab1lPpBuDC9EIJDbfMb4U1Ocbs81eHy5XZYdrJAXuGnwvBxAL6joMdZpsiK4P96XXtavu4"
    b"0l3ZLkWOcNkF9pv8b1ufvwz2u2QYCt1o8w5gbg1m8Nm1/hvZnvrTOy3Tn2voDmwbYEyx3GBfhQWbBWdAINClyEh7aBUiBcvT"
    b"0hz0sg1OwdF2KrXDOciWm+sVT0uT7aaFqHNtyMZ29/iYTaov//pJvwbhVA04aktkGo8fN7IvVLEVLOlOxVGfl0aIq9B4x8s2"
    b"xUUj2FHlD6jCQI8zTkCOc2vUGBrGsTbsP9BzP1XdouxaKOJUEGPyQacI+6vA7eROX9ZoddxwuYLfWwT77U6jKra7w/5SSFwF"
    b"LhuzX5Yuj21avOo9bd+hLTnPtAEYmV4ws9wmGiRLGsnlATst/C2dqNAz/Fxz0iboWwg5XeEy2cFldmW5WU5U22Cfle16FwRI"
    b"FBO5xB5sYL8OLsOBn6KD/UVpcYOPQxqWrKmkvjDg3r+GXTah0hhABVg4jXTILFh9o14/zWfebaw+2uXvehzjNaqNs3zfNlmA"
    b"IrjtdBLbsAJTPcxNQajxw/5yV8Od5UZkhstFteTjhsvjg/0lp5FGQt5dV6Ko9n5csN8ElysE4KrA/gqCr2ZiURlBrRiFlGYB"
    b"iuA2jXPh64yxgcsT8fltWG5m5ZVKcJlGLAqhCkUhrtH+ssmoi13YbiilijtlC7GC7S5FaUWwv3RcSgrhCmF/8nNN6ltugjiw"
    b"zgLsNabXqEzCWUHXWbsq77V3TTOeH9h97zrvApScuBMrsTUVBBXBHCNvejL6b4Wwf8ylqU4lti5FIXDh4dtx7fWB23JE4WS7"
    b"yT6Xk9ioK2Hp8hRG2UvscIT9ppO8mClqabsxCzCWghgLuKxbiFVg/1ioqCgVdcgvOteCGA1czrVas12IoxSFuC/SypVtpQIv"
    b"rq5CES3dxnZHLQqCfVrcpZo15UaPansB7C9pClocA9g1MJZGvCeNCUrSeGrBR4HLY4l6VyTKaG2nas9VhZA10fGiYrg8jgwB"
    b"jWAYVTDSwS0tZgKOpEjjSvEdc1FI8nobOwrhMhxdhfHDZSfbta4UGTc1J765dvjs8/ZUsCkVoiljxN61IMYiAJe6p60End3J"
    b"r59TVWwvp/4abS+MAdjAZYwm+zydohCXDANZpOpsXIXJwGXnRWpTFDIJO3ST2aUKtGgh2rDcxpF2GzHK7nToVbK9YBOyjMeV"
    b"IACH09JF/81FeaXwhLaVPib3QNOM9N+MpaAjF4VQ8WmrDaZRlv+ZaWNma7spBWZre4VS9L4MNRz979JFqrE9F3A2VfSZ7dCe"
    b"zlxhE7INxpMVD2CXpkNmzULcC0IS47IXe9B2moDtk5wfRZ81xblSoRjIUfZ56iw32wxDyUk8Tf03g4RU9aIQMyIy2pFlYtpC"
    b"eJcItLXtlhJ0GjfU3v8eEfZblcBbZh6qZNy088zC9qINoKgfemWWm+Ga8bPcinz+aeu/WeRlbRbiyCw3yyBbxXRXse1m2G9n"
    b"+/gWYpnPb51bL7qn5cJP6weQW6B6HCxbJxdgFtBk1qy+WcH2WYyXyd698q6r2sszmh+78F0XFwPpUgmuJbZWAbjM6eDMckNl"
    b"2ed8UNMiOm2tylKB5WY6qWztgL0dRQFbZ9jvEnw0oSnbk1hXYuvSaLWSnNyYYT8qZNwMwUsrTQ2rvgC6iHfpZESB0Ob4ikJK"
    b"U32z0n8jO7hsz/SytGOUSTeByrT8ZCzbaF10JUaF/WQhdz7a4reuLqzS7dlJhAbFqe/SDcAZ5tCUi0JM6GBUaETOt9Xa69oI"
    b"lCoMxtRcI6pge8lDEkabIHtNNh5UfW7YjukIthcUA9ly7Q07axWWm+HUMZ6qqX8q6HNnBftdUIibGKMb7McIcHv0RqtmlXdy"
    b"hP2jQ9/CMYVj56hcYNrt5K/ccMa0+J3bvaNao1XTRq7fABxkn7UpGM01E4H9u0X/rQLLzbIoRJ/umgbsLyDd2C4aJ9uLtCfz"
    b"cHkU2F/F57depBpKvJME3SRhf0EJSL41mM2pn6UrTpHlVqWEMjV4XLBorPXfLJRXSkpB049XYoflJHBmxtlU2BXabrEJlQWn"
    b"tEHnUU5byxZnpsYqXAUpmPQQq9heoc9i/55sq0BVGAScgl+0F7vg7qX0II353ruFlbhb58duGa8K13rGANYuZLntDv23Cumu"
    b"Mr9wRJZbNVdhnLB/zCy3aTdaHbU5a9K+aWtqOGXcyjYAhy6n9rn1CsorhfpvDrB/FP03647HRYNuw/QabcGNlCIzUIZH6vac"
    b"FU0ZcSGO4vNTmatAFnDZeTwdYP+o7dWtBV7MLoCYKpSd5he9x56BdsHn0R6aK/QjON8qPJ/RBdDD/uQJ4drxxBzAMwpduMLl"
    b"zM4+Pv23ccL+Am74LButjgr7nV2N2TRaHUmCbhywPwXdLSXoxqGpYccELIb9qRtXYnqVwOXMQrCuBZ+a/tu4WG7ZuMYo4qsj"
    b"+PylktdlC7HgnhOC/UXVoaVS9qnFYCH+Usn/njbsT9pugP1GnYsiSTCGQX5ql0W8nSKw49J/o5LxqnD9rMYLpC+OsXrX5P4s"
    b"xvGiGc4Px3tPlPFKY7Ldbs1qVIH1FzorytrAfsBZ/81J+nhs+m/jZLkZxtsGLpf0fHdutJr8sVK0vwL01X2uCfrqXAUUuZ9V"
    b"Yf8IwUfYBx9TeMsI+3XPYqluRaYzxpoJ6AC3R2C5TVf2eVT9twKfvzQqXAT7bVyFMUfJtfDVdfGPwHKr2Gi1sgR8BVch/yw2"
    b"jFcH2F8az0L1alYbV8PaBSjpckoYMUiGCjnpieu/OTQ5GYf+m/OJYbloijjhBLc+ixiR5YbR/N5KbcnLYkQutpcyXgveoVNZ"
    b"edEG51BSX4qwyzYAVzdlZhr3mD7LzCrOMEF7MYNnpzFe/14Yr1k/u+P1RhegCty2LgrRSh/rdiqqzHJLf+QosH+EdJcu9jDt"
    b"ohAtLHSwfYKNVifS+WnURquGbEGpBJ0RaTlqarhm3HLrqaTRahkCsO5yOgZaqNnndxWbHJf+myXLzTLIVqkteekCcIw9FHV7"
    b"LnUlNDDVxQ4qa7jikiIbZ1tye9srCc+W2O7u849WzVoUBNwbTMDdwErbSyzIcTw37aHx2m2svj00V0rSgK6wf5SiEF1QZATZ"
    b"53F1Nq7Icptpo9WZwX6UstyquArjbbTqaruFHZUarZrmt+aa0oxbCey3zwJULYgp0kCrkJMeFfZXzknr+t9VZYdNGfZbTcYC"
    b"O4yTMfMsI7DcqjHjyAL2W8Dl3Hswtbaz8b8xQnt1W9g//LkI9qfvOZYsQEIVYRToPYsuuOOI2s6iw8y4ZdCnpXkwVS3IXThe"
    b"2APjVXKtp/8jqth7bATp4yKWl20ATpNPrQZ9bXkOJnaYGwqhEv02N9sr8geqNFrV2u7ItXdtUJq1fRxNZmF7EmvsqNpoFSWw"
    b"v2Q9VW20WoYAFIEJlHFiJ14UUsU3H6f+m12vg/LqLAfbRy0KMSxeq0arriw33eda+62jwH6dW1axICa3+C11JVIfR2YJedsC"
    b"sdK4RrGmRjVXgfq4g4uDgKAuCB6IPOuFCFQuBa0UmDPu/o5BNs0LJZvT0urEgCM7zLLPYtHJp52MMHd1ZnuWm2kBWOshWttu"
    b"Yn06nJamzdTY0bqM8Wo4SW38b+1B6XDqF64H23sOfhMCHGTfSDoNyLwFoFfUT3zXpUxm0T5sHH4gxmD3KDGSabXG2uvjNa7r"
    b"pzW3ddcIAQZ6AG0VuwCCbkQbDTU5Ea4Ye1EIdoPs85TZYVaNVkeVff4Ra7RaqSDGgKYqNVp18M11BT9lp7+l22AtQaeB/QQQ"
    b"SQEotQPiGyVBQLoM0BwEHUTI6Uk8Dg007QtxWPwVglNmOD0Fdpi13+vQZ7F0URXAfsvcurvtply1RXBqUgpUKOJUkAPjtWyj"
    b"peKNzYLhN2kFKooQwBZAl4tdAOC8Am9AiHL4sZeZXnvpGXYDhKZdPl7vZSZg6TolCCkAEhvMdL4QAbCgN0jhfkEERQQwcy46"
    b"NjaWm8UpnTg9naWXxg77TadREQqZdqNVwL2gpuT0n0mj1epw2d52G8ZrFZfH/np7GTZL2zPrgfsugO+BWt11Et4bxS4A4/tE"
    b"+HEhpVJBKCDARpg6U9jvIOpQlRlnVZ0FS9WZcVW2TY7lRobFW63PYkWG37Tau5XBfltXoaTPYvpHstKCHBfs769oEgTyfKUY"
    b"N4Tk7xe7AAG+TySukJQiT77YYxHgvRjxnnV2Y1zvepbz40c5u1HpngRZ8wUErgQIizeAs3/wp9cV87mwFwTZZHN6g87vPFQQ"
    b"gMvrvxnINSnonNTpSzbayDD8MtdT7p6mgFj6+rK+a6bTX2t7QjUpHZ1O/0ya50jakbcd6Xy3yQ6T7gIZTv/s6V1oO6UCX/nO"
    b"M7rfmWzXIIcUMiizvUB1qdR2jR0ZFESGZ89dg7K6FoPtVU5/re2UXw+JDwvbrYAknfvSx//n68UuAAAQv616vR+A+HESUrJS"
    b"ikRio6jMcpss7B+ZHWaIsluz3KxJKaPo2Lmz3ErFV107Lo1QmabXQyyLS1ix3Owbrbr6/Lo6/6IsicFnL9KCLF4P1apAmVkJ"
    b"KQVUEHY3Wz9QjLd1+CCnB8Ch/ypDfQ0QknwPEAh0p5Tx5Ku8+Mli19UgBYud2nhagtKLpujEoPQmpE3LZU+polPf2Xb9aak9"
    b"bU2nJRUsfhvbs+gqNScsbB/mvDILrsB2lLPcCBZ2JDaPHEq0QQqFtifwAOn9qbzt42HZ5lL1Q2p64DVqAAnJQfg1JVqvlm8A"
    b"DDr3wBMXScq/ArAexREdvJW9pv82C793XJ/1XmH10S5613spBsZRwwdmrEvf/6svf+zGRTBT4QbwzG894+GFFxRE+B0i/orq"
    b"9lokhQeiYSGBzt8gSglBUM5nL95BiQgkzCeuqaqv/JoqzDgLFJKIRdicxPmTJXN6WNtB6XsbTnkr/7vUP9XYV1IFWmq7Icpu"
    b"tF0k3pMwxEly/HyDHZQ9iZN2FNleDsONrhSZbLdAUwJGtGFcg/3IP4iFJ71eq9MiiK9wr/cd0Avqmc98xivcAF4++jIDQP3O"
    b"uxeY1B8wcFXW6wRmFe0pZPb5qajgwpBfpqzfmn+JeVeDMvpvFsEp3Sakub5wMlIm1WcDt42TMT1B7OzIQ20qdRVMC9HN5Ukt"
    b"gCLYb7Jdc43NZmNKseVgf9k7LIfLZnevAPbnuywnNpuig9LC5y+3XcMX6Vf9ESt/3wIhVFdJ0B9cunPpAgC8fOUKF7sALyDE"
    b"88+L1198vcu99ldIiK9yyC0IIWlYUjjbr79jBe4tN2uvf+0lFiQzCykAIaQKghZJ8VUV7Hzl9U++0MXzzwu88EJYFgRkfOUr"
    b"AgDO/KcvrCIQfxR2e38d84kFgVX2JC6GnIYddBS4TJZwOXl9FvYb7lkE+9NEH7KC2/rTqCgVVgLtB8PnEHykBGJKwF0X2G/q"
    b"slxoO8ywv9R2Uyk0lQcvrU/iklSdDezXBjVL0uLG1Le17WbXmwmKPCkAoLe5/deQ+KMv/Nw/XwWAZz8KoTvA86rAL70U4vno"
    b"3/c1D36JEf4+M99gAEzEoMgVgCiLNBfAfhe4nEuR2cLldEaizNUwT8b0QFtH2U2w3ym3TkhXXbq6CqbJ6OB/69JdJttF4j0J"
    b"HfQtsb0wyq6H/X3bydr2AthPZS6PaSEO7S2D/enPTS/8ctsLW44xGMxKgRk3OFS/X1sMvgQAeP558dJHPx1apQEBMD4NxnPP"
    b"yZc/85md2jx9ngj/Hqzasl7zGNwDmKcOpfYaMw675NlnAXvfizoJMxovQlSzw1A9f3He40C1haB/z7Lx+c+9/7d2nvvD5yQ+"
    b"/WmOA/lWGwBAYLz4YojnnpNv/u5n1yDVvwXoL6DQIeHVIASBwVQAlynrKsABLicHJcuMsw3AUUG034kdptd/s2K5pV5uBZZb"
    b"Csi42J6JeLsG4JCB/bl8d0mu34kZBztFm5Ftp4x8m5lz4BaAS7g8NrA/G9BOMQph1tTQ2REvfkhBwvdrYO4A9BfE9G+/+Inf"
    b"WHvuD5+TL37yxdC0+M0bQP/r0UcZAN75zGffJSH/tVLh56XvQ4BULB9miJInHrbUVdAwwMRocHkYYSfHdBcK2GEOUXYbO0xp"
    b"u6Rfl6sEtLG9eKIaXZ4q6a5xRNkF2cFtk+1J98zK/867CoWLtBD2wx32C92hV2a72Y1mQpekVN58E0Gn8/kQ+Nd//jP/9F0A"
    b"ePS5R3k08MLcf3oFAA/+s1/5GDz/d4jpV4XvIeh2ewAJIghKi8dFPwhHtdTBluRIRdWkyHTXlOq/kV7Iwag6g9j3Q15Brv/v"
    b"Rju0tlfss5jxe62otsn7sW3H4wLbbRZc9p7Cpc9iyt/NjZdVn0VA70NbKT3T8LhkW9sz6FHo3gXsFZbj9cTMDIIiCOUtNn3V"
    b"7gJC/HFns/W7X/6FT0V+Pz8vGJ9mKjj9gbLuwP2Ln39eAC/ghy/8yZdOfeofbvvwPYTiJ0jIAyQAhAwwKwgSM/MDCbPxtXdD"
    b"6miv6N2NM6aDGY/X7OIqioQQwpeSFUvuBbdUL/xaVwX/x1d+4VPfiNYqAIvF7/ZYHG0+AOj+T/3yaUG13yLGP4UQK9GeqBRo"
    b"yGmzL4gZRfY5X0vu1liyvCik+J5kVo1FSS04sn6r6eS3sX03NlrV2F4isT7eJrMFYzoG/UmnRqvaPgCw7/MwnNsMYkUkBIiI"
    b"Fd+AwL8T7ebv/fnP//oZEHGM2nn8+xIP3gEDwCO/+asnAyk+RFL+ppDiE6LuI2z3wMxdgIlAkpIOkPVkLFikZdDX4GpMRP+t"
    b"4CVaw/7SxV9RCMK2vVeV9upWDVfcN5vx9Zgs2UydbHfodWCx+K2VtIYdhJijRh0hABZS1vx9Cwi2WwiD3hfA/PtBu/03X/7F"
    b"f3F+4LInkbvFl+cAR6IPfe45+czysnj5M585D+D8A//Lc5sc8vWwS0+B+bSs+U0igAMFZsUAwujBNAXbu0F7jf7uvjNJ172X"
    b"xtzmeI3/lwkc/RczgYTwPOE16gLMCHbarWC7dSbodl+BxB/85c/8sz8DgGe+/Xv+6TPL6kWicHrm/eFzEs+9qEDgh3/nnxyk"
    b"XvfjEPRJJfDTJMRihBhIpndAjk21h8uFss95cIFy2akKJ65llDx3Apa6PCPLPu/iRqt295xck9mKWhQm6F7W4mzURqsim2mi"
    b"GAJwKH3JYaA2CeLLYP7DcEn+5Rc/+BtrYCa8+KLAJz8ZVl3GovIG8INHuY8K3vzd/7g2h31/Sr3wXwrgt0nh34DxA+l78Bfm"
    b"4TUb8Oo+hO+DfA8U8ZXL87LWLLeyVJ/NZKRSlltZusqUsjRVppk3IUO6KzNRrVhuRbYXpZ6EQaHJKt2F1PVFVaBFsL+S7SnW"
    b"Z0nsodD2od1FClSpzab/rMb0b3rxUyzZTb4HUfMhm3X4C3Pw9y+gcfgAvGYdYPyAFf8bVvhtbAf/stbq/ekXP/gbawOo/4Mf"
    b"jFSfMzogfO45gUdXCS+8NGg79NBv/eNT5MnHmfAQQZwA8d0EWgFwAIQlgBcBMQfAB0ES5Rws80mcFbUw+akwi1oYJ5/J7xcZ"
    b"n6XQX9YEHwUifdbBljsmCSjbAJxro1VB+Wni0mhVWIy1jd9fyXbKz2qqarul3y+K5eTyyDRavASEiDr27EDQJoA7AN0C8Q0S"
    b"dAWhuggSb3U88dpLP/0/netf+uzzz3uHH3uMX/zkJxVGLNAbn4fz/PPimatX5ctHj4Z44QXV/+dH//lzC6Hf+AhCPC4EPwTi"
    b"Uww6RsAKIBYArhORsFaUtZVesoqy2ynKVoqST0H2eTKwf1TpdUdXoWgzdbZ9urA/fU/bjsCDXygi6gC8BdANkLgsCOeY8JZS"
    b"4Ws9v/v1l376XwxaeT3Pz4vPvXy3/MVnPhW+QKTGtWz/f5xl3MjAVo1bAAAAAElFTkSuQmCC"
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
