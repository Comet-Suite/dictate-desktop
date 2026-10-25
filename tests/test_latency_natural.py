"""Latency, atomic insertion, single-shortcut and context-aware symbol regressions."""
import json
from pathlib import Path
import sys
import threading
import time
import types
from unittest.mock import Mock

import numpy as np
import pytest
from PyQt6.QtCore import QEvent, QMimeData, QObject, Qt
from PyQt6.QtGui import QKeySequence, QTextCursor
from PyQt6.QtWidgets import QApplication, QKeySequenceEdit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as m
from test_targeted_fix import Engine, feed, until
from test_focus_symbols import harness, pump, pump_until, qt_focus


@pytest.mark.parametrize('spoken,expected', [
    ('four hundred dollars', '$400'),
    ('400 dollars', '$400'),
    ('dollars four hundred', '$400'),
    ('I need four hundred dollars today.', 'I need $400 today.'),
    ('400 rupees', '₹400'),
    ('four hundred rupees', '₹400'),
    ('rupees four hundred', '₹400'),
    ('INR 400', '₹400'),
    ('four hundred Indian rupees', '₹400'),
    ('a dollar', '$1'),
    ('half a dollar', '$0.5'),
    ('a half dollar', '$0.5'),
    ('two and a half dollars', '$2.5'),
    ('four hundred and fifty dollars', '$450'),
    ('four hundred fifty dollars', '$450'),
    ('one hundred and ten rupees', '₹110'),
    ('one lakh rupees', '₹100000'),
    ('two lakh fifty thousand rupees', '₹250000'),
    ('two crore rupees', '₹20000000'),
    ('4.5 lakh rupees', '₹450000'),
    ('two thousand five hundred dollars', '$2500'),
    ('one million dollars', '$1000000'),
    ('six point five dollars', '$6.5'),
    ('400.50 rupees', '₹400.50'),
    ('four hundred dollars and fifty cents', '$400.50'),
    ('four hundred rupees and fifty paise', '₹400.50'),
    ('five pounds and twenty pence', '£5.20'),
    ('four euros and five cents', '€4.05'),
    ('four hundred dollars fifty cents', '$400.50'),
    ('negative four hundred dollars', '-$400'),
    ('minus four hundred rupees', '-₹400'),
    ('five euros', '€5'),
    ('five British pounds', '£5'),
    ('I paid four hundred pounds.', 'I paid £400.'),
    ('fifty yen', '¥50'),
    ('twenty yuan', '¥20'),
    ('four hundred won', '₩400'),
    ('fifty rubles', '₽50'),
    ('two bitcoin', '₿2'),
    ('fifty cents', '50¢'),
    ('fifty percent', '50%'),
    ('50 percent', '50%'),
    ('six point five percent', '6.5%'),
    ('twenty per cent', '20%'),
    ('two plus two equals four', '2 + 2 = 4'),
    ('5 multiplied by 3 equals 15', '5 × 3 = 15'),
    ('ten divided by two equals five', '10 ÷ 2 = 5'),
    ('five is greater than two', '5 > 2'),
    ('five greater than two', '5 > 2'),
    ('two less than or equal to five', '2 ≤ 5'),
    ('25 degrees Celsius', '25°C'),
    ('thirty two degrees Fahrenheit', '32°F'),
    ('my email is john dot smith at gmail dot com', 'my email is john.smith@gmail.com'),
    ('john underscore smith at example dot com', 'john_smith@example.com'),
    ('john plus test at example dot co dot in', 'john+test@example.co.in'),
    ('dottie at gmail dot com', 'dottie@gmail.com'),
    ('john at sign gmail dot com', 'john@gmail.com'),
    ('four hundred dollar sign', '$400'),
    ('dollar sign four hundred', '$400'),
    ('price rupee sign four hundred', 'price ₹400'),
    ('at sign hash sign dollar sign', '@#$'),
    ('At sign, hash sign, dollar sign.', '@#$'),
    ('The price is $400 and ₹500.', 'The price is $400 and ₹500.'),
    ('I will meet you at home.', 'I will meet you at home.'),
    ('We need the dollar exchange rate.', 'We need the dollar exchange rate.'),
    ('The rupee has strengthened.', 'The rupee has strengthened.'),
    ('I won four prizes.', 'I won 4 prizes.'),
    ('Try four times.', 'Try 4 times.'),
    ('I go two times a week.', 'I go 2 times a week.'),
    ('It weighs four hundred pounds.', 'It weighs 400 pounds.'),
    ('four hundred pounds of flour', '400 pounds of flour'),
    ('This is a plus for the project.', 'This is a plus for the project.'),
    ('We discuss a hash table and the at keyword.', 'We discuss a hash table and the at keyword.'),
])
def test_natural_symbols_without_saying_sign(spoken, expected):
    assert m.normalize_dictation_text(spoken) == expected


def test_old_symbol_vocabulary_still_works():
    for name, char in m.SPOKEN_SYMBOLS.items():
        assert m.normalize_dictation_text(name) == char


def test_two_saved_shortcuts_are_preserved(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(dict(m.DEFAULTS, hotkey='Alt+H', dict_hotkey='Alt+G')))
    monkeypatch.setattr(m,'SETTINGS_FILE',path)
    settings=m.load_settings()
    assert settings['hotkey']=='Alt+H' and settings['dict_hotkey']=='Alt+G'
    m.save_settings(settings)
    assert json.loads(path.read_text())['dict_hotkey']=='Alt+G'


def test_single_shortcut_build_migrates_without_losing_recording_key(tmp_path, monkeypatch):
    path=tmp_path/'settings.json'
    saved=dict(m.DEFAULTS,hotkey='Alt+J')
    saved.pop('dict_hotkey')
    path.write_text(json.dumps(saved))
    monkeypatch.setattr(m,'SETTINGS_FILE',path)
    settings=m.load_settings()
    assert settings['dict_hotkey']=='Alt+J'
    assert settings['hotkey']==m.DEFAULTS['hotkey']


def test_two_shortcut_controls_are_restored(harness):
    app,e,dialog,pill,toast,controller=harness
    assert dialog.findChildren(QKeySequenceEdit)==[dialog.keys,dialog.keys2]
    assert dialog.keys.keySequence()==QKeySequence(m.DEFAULTS['hotkey'])
    assert dialog.keys2.keySequence()==QKeySequence(m.DEFAULTS['dict_hotkey'])


def test_background_transcription_finishes_without_inserting_before_stop(qt_focus):
    e = Engine()
    emitted = []
    e.heard.connect(emitted.append)
    try:
        e.toggle_record()
        feed(e, seconds=17)
        until(lambda: len(e.model.calls) >= 2 and not e.busy)
        pump(qt_focus, 30)
        assert e.recording and e.pending == 0
        assert not e.typed and not emitted
        calls_before_stop = len(e.model.calls)
        e.toggle_record()
        pump_until(qt_focus, lambda: len(e.typed) == 1 and e.pending == 0)
        assert len(e.model.calls) == calls_before_stop + 1  # only short remainder after Stop
        assert len(emitted) == 1
    finally:
        e.shutdown()
        e._segment_thread.join(1)
        e._transcribe_thread.join(1)


def test_natural_pause_can_precompute_but_never_commit(qt_focus):
    e = Engine()
    try:
        e.toggle_record()
        feed(e, seconds=4)
        feed(e, seconds=8, value=0)
        until(lambda: len(e.model.calls) == 1 and not e.busy)
        assert e.recording and not e.typed
        assert len(e.model.calls[0]) < 5 * m.SR  # eight seconds of silence were not decoded
        e.toggle_record()
        until(lambda: e.pending == 0 and len(e.typed) == 1)
        assert len(e.model.calls) == 1  # no repeated inference at commit
    finally:
        e.shutdown()
        e._segment_thread.join(1)
        e._transcribe_thread.join(1)


def test_cancelling_a_partially_precomputed_recording_never_leaks_text(qt_focus):
    e = Engine()
    try:
        e.toggle_record()
        feed(e, seconds=9)
        until(lambda: len(e.model.calls) == 1 and not e.busy)
        e.set_listening(False)
        time.sleep(.05)
        assert not e.typed
        e.toggle_record()
        feed(e, blocks=10)
        e.toggle_record()
        until(lambda: e.pending == 0 and len(e.typed) == 1)
        assert e.typed == ['part2 ']
    finally:
        e.shutdown()
        e._segment_thread.join(1)
        e._transcribe_thread.join(1)


def test_failed_preview_does_not_paste_a_partial_recording(qt_focus):
    e = Engine()
    errors = []
    e.message.connect(errors.append)
    original = e.model.transcribe
    def transcribe(*args, **kwargs):
        if not e.model.calls:
            return original(*args, **kwargs)
        raise RuntimeError('simulated ASR failure')
    e.model.transcribe = transcribe
    try:
        e.toggle_record()
        feed(e, seconds=17)
        e.toggle_record()
        pump_until(qt_focus, lambda: e.pending == 0)
        pump(qt_focus, 20)
        assert not e.typed
        assert len(errors) == 1 and 'simulated ASR failure' in errors[0]
    finally:
        e.shutdown()
        e._segment_thread.join(1)
        e._transcribe_thread.join(1)


@pytest.mark.parametrize('parts,expected', [
    ([('Please write the whole sentence',False),('whole sentence once only.',True)], 'Please write the whole sentence once only.'),
    ([('I agree.',False),('I agree.',False)], 'I agree. I agree.'),
    ([('Hello, WORLD.',False),('hello world and everyone',True)], 'Hello, WORLD and everyone'),
    ([('This is an experi',False),('experience worth sharing',True)], 'This is an experience worth sharing'),
])
def test_overlap_join_preserves_order_and_intentional_repetition(parts,expected):
    assert m._join_transcript_parts(parts) == expected


def test_clipboard_bridge_restores_previous_contents_but_not_user_new_copy(qt_focus):
    clipboard = QApplication.clipboard()
    clipboard.setText('original clipboard')
    bridge = m._ClipboardBridge(qt_focus)
    request = dict(text='temporary dictation', ready=threading.Event(), ok=False, cancelled=False)
    bridge._prepare(request)
    assert request['ok'] and request['ready'].is_set()
    token = bridge._token
    assert clipboard.text() == 'temporary dictation'
    bridge._restore(token)
    assert clipboard.text() == 'original clipboard'
    request = dict(text='dictation 2', ready=threading.Event(), ok=False, cancelled=False)
    bridge._prepare(request)
    token = bridge._token
    clipboard.setText('a new user copy')
    bridge._restore(token)
    assert clipboard.text() == 'a new user copy'


def test_consecutive_pastes_restore_the_true_original_clipboard(qt_focus):
    clipboard = QApplication.clipboard()
    clipboard.setText('original before both')
    bridge = m._ClipboardBridge(qt_focus)
    for text in ('paste one', 'paste two'):
        bridge._prepare(dict(text=text,ready=threading.Event(),ok=False,cancelled=False))
    bridge._restore(bridge._token)
    assert clipboard.text() == 'original before both'


def test_cancelled_clipboard_request_is_not_executed_later(qt_focus):
    QApplication.clipboard().setText('untouched')
    bridge = m._ClipboardBridge(qt_focus)
    request = dict(text='do not copy',ready=threading.Event(),ok=False,cancelled=True)
    bridge._prepare(request)
    assert request['ready'].is_set() and not request['ok']
    assert QApplication.clipboard().text() == 'untouched'


class KeyRecorder(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.pastes = self.enters = 0
    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_V and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.pastes += 1
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.enters += 1
        return False


@pytest.mark.parametrize('auto_enter', [False, True])
def test_actual_long_paragraph_is_one_paste_not_character_typing(harness, auto_enter):
    app, e, d, pill, toast, controller = harness
    if QApplication.platformName() != 'xcb':
        pytest.skip('native paste test needs X11')
    d.test.selectAll()
    d.test.setFocus()
    recorder = KeyRecorder(d.test)
    d.test.installEventFilter(recorder)
    e.auto_enter = auto_enter
    e._clipboard_bridge = m._ClipboardBridge(app)
    e._type = types.MethodType(m.Engine._type, e)
    text = ('One entire paragraph, including symbols ₹400 $500 @#%.\n' * 100)
    QApplication.clipboard().setText('clipboard before dictation')
    finished = threading.Event()
    def insert():
        e._type(text, e._model_generation)
        finished.set()
    worker = threading.Thread(target=insert)
    start = time.monotonic()
    worker.start()
    pump_until(app, finished.is_set, 5)
    pump_until(app, lambda: d.test.toPlainText().startswith('One entire paragraph'), 3)
    pump(app, 100)
    worker.join(1)
    assert d.test.toPlainText() == text + ('\n' if auto_enter else '')
    assert recorder.pastes == 1
    assert recorder.enters == (1 if auto_enter else 0)
    assert time.monotonic() - start < 1.5
    e._clipboard_bridge._restore(e._clipboard_bridge._token)
    assert QApplication.clipboard().text() == 'clipboard before dictation'
