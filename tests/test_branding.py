import base64
import hashlib
from pathlib import Path
import sys
from PIL import Image
from PyQt6.QtWidgets import QApplication
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import dictate as m


def test_embedded_logo_is_exactly_the_packaged_artwork():
    root=Path(__file__).resolve().parents[1]
    assert base64.b64decode(m._LOGO_PNG)==(root/'assets/app-icon.png').read_bytes()
    icon=Image.open(root/'assets/app.ico')
    assert (256,256) in icon.ico.sizes()
    assert m._WINDOWS_APP_ID=='CometSuite.Dictate.Desktop'


def test_qt_application_icon_loads(qt):
    icon=m._application_icon()
    assert not icon.isNull()
    assert not icon.pixmap(32,32).isNull()
