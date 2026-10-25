import ctypes
from pathlib import Path
import sys
import time
import types
from unittest.mock import Mock

import numpy as np
import pytest
from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6.QtGui import QFont, QKeyEvent, QShowEvent, QTextCursor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (QApplication, QComboBox, QFrame, QLineEdit,
                             QScrollArea, QWidget)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as m
from test_targeted_fix import Engine, controller, feed, until


@pytest.fixture(scope='session')
def qt_focus():
    app = QApplication.instance() or QApplication(['focus-symbol-checks'])
    app.setQuitOnLastWindowClosed(False)
    app.setStyle('Fusion')
    app.setFont(QFont('Segoe UI', 10))
    return app


def pump(app, milliseconds=100):
    deadline = time.monotonic() + milliseconds / 1000
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.002)


def pump_until(app, condition, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(.002)
    assert condition()


@pytest.fixture
def harness(qt_focus, monkeypatch):
    monkeypatch.setattr(m, 'list_input_devices', lambda: [
        'Microphone Array (AMD Audio Device)',
        'An extremely long external microphone device name, recording input ' * 10])
    e = Engine()
    state = dict(m.DEFAULTS, dark_mode=False,
                 model_path='C:/models/parakeet-unified-en-0.6b-Q8_0.gguf')
    d = m.SettingsDialog(state, e)
    d.resize(1000, 720)
    pill = m.Pill(e, state)
    toast = m.Toast()
    app = controller(e)
    app.settings = state
    app.qt = qt_focus
    app._dictation_shortcut_registered = False
    app._shortcut_input_filter = m._DictationShortcutFilter(app)
    app.pill, app.toast, app.dialog = pill, toast, d
    app.tray = Mock()
    app.say = types.MethodType(m.App.say, app)
    d.show()
    d.activateWindow()
    pill.move(10, 10)
    pill.fade_in()
    pump(qt_focus, 80)
    d.activateWindow()
    d.test.setPlainText('Please keep this selection for dictation.')
    cursor = d.test.textCursor()
    cursor.setPosition(7)
    cursor.setPosition(16, QTextCursor.MoveMode.KeepAnchor)
    d.test.setTextCursor(cursor)
    d.test.setFocus()
    pump(qt_focus, 40)
    yield qt_focus, e, d, pill, toast, app
    pill.user_hidden = True
    pill.timer.stop()
    pill.guard.stop()
    pill.fade.stop()
    pill.hide()
    toast.timer.stop()
    toast.fade.stop()
    toast.hide()
    d.tick.stop()
    d.hide()
    e.shutdown()
    e._segment_thread.join(1)
    e._transcribe_thread.join(1)
    qt_focus.removeEventFilter(app._shortcut_input_filter)
    app._shortcut_input_filter.deleteLater()
    for widget in (pill, toast, d):
        widget.deleteLater()
    pump(qt_focus, 10)


def selected(widget):
    c = widget.textCursor()
    return c.anchor(), c.position(), c.selectedText()


@pytest.mark.parametrize('width', [960, 984, 1000, 1100])
def test_settings_columns_do_not_clip_long_model_or_mic_names(harness, width):
    app, e, d, pill, toast, controller_app = harness
    d.resize(width, 720)
    d.repo.addItem('owner/' + 'very-long-repository-name-' * 30)
    d.repo.setCurrentIndex(d.repo.count() - 1)
    pump(app, 80)
    scroll = d.findChild(QScrollArea)
    assert scroll.widget().width() <= scroll.viewport().width()
    assert scroll.horizontalScrollBar().maximum() == 0
    for control in (d.repo, d.combo, d.mic, d.btn_dl, d.btn_pick):
        top = control.mapTo(scroll.widget(), QPoint(0, 0))
        assert top.x() >= 0
        assert top.x() + control.width() <= scroll.widget().width()
    assert len([w for w in d.findChildren(QFrame) if w.objectName() == 'card']) == 8


def test_start_speak_pause_commit_preserves_focus_and_selection(harness):
    app, e, d, pill, toast, controller_app = harness
    assert QApplication.focusWidget() is d.test
    original = selected(d.test)
    controller_app.on_dictation_key()
    assert e.recording
    feed(e, blocks=20)
    feed(e, blocks=30, value=0)
    pump(app, 750)  # exercise both settings meter and pill visibility timers
    assert QApplication.focusWidget() is d.test
    assert selected(d.test) == original
    assert not e.typed
    controller_app.on_dictation_key()
    pump_until(app, lambda: e.pending == 0 and len(e.typed) == 1)
    assert QApplication.focusWidget() is d.test
    assert selected(d.test) == original


def test_popup_notification_show_and_hide_never_focuses_editor_away(harness):
    app, e, d, pill, toast, controller_app = harness
    original = selected(d.test)
    controller_app.say('Recording', 80)
    pump(app, 650)  # show, fade, hide
    assert not toast.isVisible()
    assert QApplication.focusWidget() is d.test
    assert selected(d.test) == original
    assert toast.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert pill.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert toast.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert toast.windowFlags() & Qt.WindowType.WindowTransparentForInput


def test_clicking_mic_does_not_take_textbox_focus(harness):
    app, e, d, pill, toast, controller_app = harness
    original = selected(d.test)
    center = pill._centers()[1]
    QTest.mouseClick(pill, Qt.MouseButton.LeftButton,
                     pos=QPoint(round(center.x() * pill.k), round(center.y() * pill.k)))
    pump(app, 150)
    assert QApplication.focusWidget() is d.test
    assert selected(d.test) == original


def test_guard_restores_selection_only_on_same_unmodified_document(harness):
    app, e, d, pill, toast, controller_app = harness
    original = selected(d.test)
    guard = m._InputFocusGuard()
    c = d.test.textCursor()
    c.clearSelection()
    d.test.setTextCursor(c)
    guard.restore()
    assert selected(d.test) == original
    guard2 = m._InputFocusGuard()
    d.test.setPlainText('A changed document must not get an old selection.')
    changed = selected(d.test)
    guard2.restore()
    assert selected(d.test) == changed


def test_deferred_guard_does_not_undo_user_selection_change(harness):
    app, e, d, pill, toast, controller_app = harness
    guard = m._InputFocusGuard()
    c = d.test.textCursor()
    c.setPosition(2)
    d.test.setTextCursor(c)
    deliberate = selected(d.test)
    guard.restore(deferred=True)
    assert selected(d.test) == deliberate


def test_guard_does_not_take_focus_back_from_other_interactive_control(harness):
    app, e, d, pill, toast, controller_app = harness
    guard = m._InputFocusGuard()
    d.repo.setFocus()
    pump(app, 30)
    current = QApplication.focusWidget()
    guard.restore()
    assert QApplication.focusWidget() is current
    assert QApplication.focusWidget() is not d.test


def test_line_edit_selection_guard(harness):
    app, e, d, pill, toast, controller_app = harness
    edit = d.repo.lineEdit()
    edit.setText('keep the selected text')
    edit.setFocus()
    edit.setSelection(5, 3)
    pump(app, 20)
    guard = m._InputFocusGuard()
    edit.deselect()
    guard.restore()
    assert edit.selectedText() == 'the'


def test_guard_restores_native_foreground_only_if_our_popup_stole_it(harness):
    app, e, d, pill, toast, controller_app = harness
    guard = m._InputFocusGuard()
    guard.foreground = int(d.winId())
    guard.api = Mock()
    guard.api.GetForegroundWindow.return_value = int(pill.winId())
    guard.restore()
    guard.api.SetForegroundWindow.assert_called_once_with(int(d.winId()))
    guard.api.SetForegroundWindow.reset_mock()
    guard.api.GetForegroundWindow.return_value = 0x7fffffff  # unrelated app
    guard.restore()
    guard.api.SetForegroundWindow.assert_not_called()


def test_windows_popup_style_and_mouse_activation_are_nonactivating(harness, monkeypatch):
    app, e, d, pill, toast, controller_app = harness
    user = Mock()
    user.GetWindowLongPtrW.return_value = 0x80000
    monkeypatch.setattr(m, 'os', types.SimpleNamespace(name='nt'))
    monkeypatch.setattr(m.ctypes, 'windll', types.SimpleNamespace(user32=user), raising=False)
    pill.showEvent(QShowEvent())
    args = user.SetWindowLongPtrW.call_args.args
    assert args[0] == int(pill.winId()) and args[1] == -20
    assert args[2] & 0x08000000
    assert args[2] & 0x00000080
    assert args[2] & 0x00080000  # existing layered-window style preserved
    from ctypes import wintypes
    msg = wintypes.MSG()
    msg.message = 0x0021
    assert pill.nativeEvent(b'windows_generic_MSG', ctypes.addressof(msg)) == (True, 3)
    msg.message = 0x1234
    assert pill.nativeEvent(b'windows_generic_MSG', ctypes.addressof(msg)) == (False, 0)


def test_unhandled_native_events_have_explicit_safe_result(harness):
    app, e, d, pill, toast, controller_app = harness
    assert pill.nativeEvent(b'xcb_generic_event_t', 0) == (False, 0)
    assert toast.nativeEvent(b'xcb_generic_event_t', 0) == (False, 0)


def test_real_alt_g_shortcut_keeps_editor_focus(harness):
    app, e, d, pill, toast, controller_app = harness
    if QApplication.platformName() != 'xcb':
        pytest.skip('requires virtual X11 display')
    from Xlib import X, XK, display
    from Xlib.ext import xtest
    keys = m.Hotkeys()
    keys.triggered.connect(controller_app.on_dictation_key)
    assert keys.set('Alt+G')
    controller_app.settings['dict_hotkey'] = 'Alt+G'
    controller_app._dictation_shortcut_registered = True
    keys._listener.wait()
    keyboard = display.Display()
    pump(app, 100)
    original = selected(d.test)
    def press():
        alt = keyboard.keysym_to_keycode(XK.string_to_keysym('Alt_L'))
        g = keyboard.keysym_to_keycode(XK.string_to_keysym('g'))
        for event_type, code in ((X.KeyPress, alt), (X.KeyPress, g),
                                 (X.KeyRelease, g), (X.KeyRelease, alt)):
            xtest.fake_input(keyboard, event_type, code)
        keyboard.sync()
    try:
        press()
        pump_until(app, lambda: e.recording)
        assert QApplication.focusWidget() is d.test
        assert selected(d.test) == original
        feed(e, blocks=15)
        press()
        pump_until(app, lambda: e.pending == 0 and len(e.typed) == 1)
        assert QApplication.focusWidget() is d.test
        assert selected(d.test) == original
    finally:
        keys.stop()
        keyboard.close()


def test_at_least_100_distinct_symbol_characters():
    assert len(set(m.SPOKEN_SYMBOLS.values())) >= 100
    assert all(len(char) == 1 for char in m.SPOKEN_SYMBOLS.values())


@pytest.mark.parametrize('name,symbol', sorted(m.SPOKEN_SYMBOLS.items()))
def test_every_symbol_name_and_asr_punctuation(name, symbol):
    assert m.expand_spoken_symbols(name) == symbol
    assert m.expand_spoken_symbols(name.title() + '.') == symbol


@pytest.mark.parametrize('spoken,expected', [
    ('at sign hash sign dollar sign', '@#$'),
    ('At sign, hash sign, dollar sign.', '@#$'),
    ('underscore asterisk ampersand plus sign', '_*&+'),
    ('open bracket hello close bracket', '[hello]'),
    ('hello comma world exclamation mark.', 'hello, world!'),
    ('the total is dollar sign 100', 'the total is $100'),
    ('price rupee sign 500', 'price ₹500'),
    ('turn right arrow now', 'turn → now'),
    ('left arrow go', '← go'),
    ('please use open double quote hello close double quote', 'please use "hello"'),
    ('We meet at five and discuss a hash table and dollar costs.',
     'We meet at five and discuss a hash table and dollar costs.'),
    ('john@example.com costs $100 #example', 'john@example.com costs $100 #example'),
    ('longernameat sign should not match', 'longernameat sign should not match'),
    ('I was at the sign yesterday.', 'I was at the sign yesterday.'),
])
def test_symbol_composition_spacing_and_ordinary_words(spoken, expected):
    assert m.expand_spoken_symbols(spoken) == expected


def test_symbol_commands_cross_internal_chunk_boundaries_and_commit_once(qt_focus):
    e = Engine()
    parts = iter(['at', 'sign hash', 'sign dollar sign'])
    e.model.transcribe = lambda *args, **kwargs: next(parts)
    try:
        e.toggle_record()
        feed(e, seconds=20)
        e.toggle_record()
        until(lambda: e.pending == 0 and len(e.typed) == 1)
        assert e.typed == ['@#$ ']
    finally:
        e.shutdown()
        e._segment_thread.join(1)
        e._transcribe_thread.join(1)


def test_guard_does_not_activate_stale_qt_editor_over_external_native_target(harness, monkeypatch):
    app, e, d, pill, toast, controller_app = harness
    native = Mock()
    native.GetForegroundWindow.return_value = 0x7ffffffe
    monkeypatch.setattr(m, '_foreground_api', lambda: native)
    guard = m._InputFocusGuard()
    assert guard.foreground == 0x7ffffffe
    assert guard.focus_widget is None and guard.editor is None
    native.GetForegroundWindow.return_value = int(pill.winId())
    guard.restore()
    native.SetForegroundWindow.assert_called_once_with(0x7ffffffe)


def test_shortcut_filter_tracks_registration_and_current_saved_shortcut(harness):
    app, e, d, pill, toast, controller_app = harness
    filter_ = controller_app._shortcut_input_filter
    controller_app.settings['dict_hotkey'] = 'Alt+G'
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_G, Qt.KeyboardModifier.AltModifier, 'g')
    assert filter_.eventFilter(d.test, event) is False
    controller_app._dictation_shortcut_registered = True
    assert filter_.eventFilter(d.test, event) is True
    controller_app.settings['dict_hotkey'] = 'Alt+H'
    assert filter_.eventFilter(d.test, event) is False
    controller_app.settings['dict_hotkey'] = 'Ctrl+Alt+Space'
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space,
                      Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier, ' ')
    assert filter_.eventFilter(d.test, event) is True


def test_shortcut_filter_does_not_block_shortcut_capture_control(harness):
    app, e, d, pill, toast, controller_app = harness
    controller_app.settings['dict_hotkey'] = 'Alt+G'
    controller_app._dictation_shortcut_registered = True
    d.keys2.setFocus()
    pump(app, 20)
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_G, Qt.KeyboardModifier.AltModifier, 'g')
    assert controller_app._shortcut_input_filter.eventFilter(d.keys2, event) is False


def test_every_symbol_alias_accepts_hyphens_tabs_and_variable_whitespace():
    for name, value in m.SPOKEN_SYMBOLS.items():
        assert m.expand_spoken_symbols(name.replace(' ', '-')) == value
        assert m.expand_spoken_symbols(name.replace(' ', '\t  ')) == value
