"""Capture installer-test startup failures without showing unattended dialogs."""
import ctypes
import faulthandler
from pathlib import Path
import sys

if '--packaging-smoke-test' in sys.argv:
    try:
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except AttributeError:
        pass
    try:
        position = sys.argv.index('--packaging-smoke-test') + 1
        log = Path(sys.argv[position] + '.startup.log')
        log.parent.mkdir(parents=True, exist_ok=True)
        _stream = log.open('w', encoding='utf-8', buffering=1)
        sys.stdout = sys.stderr = _stream
        faulthandler.enable(_stream, all_threads=True)
        print('Frozen process started; importing application.', flush=True)
    except Exception:
        pass
