"""Regression tests kept outside the single-file deliverable."""
import ast
import hashlib
import json
import importlib.util
from pathlib import Path
import queue
import struct
import sys
import threading
import time
import types
from unittest.mock import Mock

import numpy as np
import pytest
from PyQt6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dictate as appmod


@pytest.fixture(scope='session', autouse=True)
def qt():
    instance = QApplication.instance() or QApplication(['targeted-regression-tests'])
    return instance


class Model:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.gate = None
        self.started = threading.Event()

    def transcribe(self, audio, sample_rate=16000):
        index = len(self.calls) + 1
        self.calls.append(np.asarray(audio).copy())
        self.started.set()
        if self.gate:
            assert self.gate.wait(5)
        return f'part{index}'

    def close(self):
        self.closed = True


class Engine(appmod.Engine):
    def __init__(self):
        self.typed = []
        self.type_ready = threading.Event()
        self.syncs = 0
        super().__init__()
        self.model = Model()
        self._clipboard_bridge = None  # explicit unit-test fallback; bulk paste has separate live Qt tests

    def _sync_stream(self):
        # No physical microphone in these synthetic tests.
        self.syncs += 1

    def _type(self, text, generation=None):
        self.typed.append(text)
        self.type_ready.set()


@pytest.fixture
def engine():
    e = Engine()
    yield e
    e.shutdown()
    e._segment_thread.join(1)
    e._transcribe_thread.join(1)


def until(predicate, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.003)
    assert predicate(), 'condition did not become true'


def feed(e, seconds=None, blocks=None, value=0.8, frames=None):
    if frames is not None:
        e._on_audio(np.full((frames, 1), value, np.float32), frames, None, None)
        return
    count = round(seconds * appmod.SR / appmod.BLOCK) if seconds is not None else blocks
    for _ in range(count):
        data = np.full((appmod.BLOCK, 1), value, np.float32)
        e._on_audio(data, len(data), None, None)


def controller(e, path=''):
    app = appmod.App.__new__(appmod.App)
    app.engine = e
    app.settings = dict(appmod.DEFAULTS, model_path=path, stop_mode='auto', unload_min=5)
    app._dictation_after_load = False
    app.dialog = None
    app.say = Mock()
    app.reload_model = Mock()
    return app


def test_existing_visual_classes_and_two_shortcut_layout_are_preserved():
    expected = json.loads((ROOT / 'tests/fixtures/ui_source_hashes.json').read_text())
    source = (ROOT / 'dictate.py').read_text()
    nodes = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.ClassDef)}
    for name, digest in expected.items():
        actual = hashlib.sha256(ast.get_source_segment(source,nodes[name]).encode()).hexdigest()
        assert actual == digest, name


def test_shortcut_overrides_saved_auto_mic_toggle(engine):
    app = controller(engine)
    assert not engine.manual
    app.on_dictation_key()
    assert engine.recording and engine.listening
    feed(engine, blocks=12)
    app.on_dictation_key()
    until(lambda: len(engine.typed) == 1 and engine.pending == 0)
    assert not engine.recording
    assert engine.listening  # committing is NOT the old microphone-off action
    assert engine.typed == ['part1 ']


def test_pause_does_not_commit(engine):
    engine.toggle_record()
    feed(engine, blocks=15)
    feed(engine, blocks=80, value=0)
    time.sleep(0.3)
    assert engine.recording
    assert engine.pending == 0
    assert not engine.model.calls
    assert not engine.typed
    feed(engine, blocks=15)
    engine.toggle_record()
    until(lambda: engine.pending == 0 and len(engine.typed) == 1)
    assert len(engine.model.calls) == 1
    assert 30 * appmod.BLOCK <= len(engine.model.calls[0]) < 110 * appmod.BLOCK  # only prolonged silence is compacted


def test_ten_second_boundary_never_auto_submits(engine):
    engine.toggle_record()
    feed(engine, seconds=16)
    time.sleep(0.25)
    assert engine.recording and not engine.typed
    assert engine.model.calls  # background inference, NEVER background insertion
    engine.toggle_record()
    until(lambda: engine.pending == 0 and len(engine.typed) == 1)


def test_internal_twentyfive_second_chunks_type_once(engine):
    engine.toggle_record()
    feed(engine, seconds=72)
    assert not engine.typed
    engine.toggle_record()
    until(lambda: engine.pending == 0 and len(engine.typed) == 1)
    assert len(engine.model.calls) == 10
    assert engine.typed == [" ".join(f"part{i+1}" for i in range(10)) + " "]
    assert all(len(chunk) <= appmod.ASR_PIECE_SAMPLES for chunk in engine.model.calls)


def test_audio_before_commit_is_drained_even_if_queued(engine):
    # Artificially hold the segmenter out of the queue so stop has to wait for
    # all previously captured audio, rather than throwing away the backlog.
    gate = threading.Event()
    class GatedQueue(queue.Queue):
        def get(self, *args, **kwargs):
            gate.wait(3)
            return super().get(*args, **kwargs)
    old = engine._audio_q
    engine._audio_q = GatedQueue()
    time.sleep(0.22)  # old get times out; the worker now waits at the test gate
    engine.toggle_record()
    feed(engine, blocks=30)
    assert engine.toggle_record() is False
    assert engine.pending == 1 and not engine.typed
    gate.set()
    until(lambda: engine.pending == 0 and len(engine.typed) == 1)
    assert sum(map(len, engine.model.calls)) == 30 * appmod.BLOCK


def test_partial_final_block_is_not_lost(engine):
    engine.toggle_record()
    feed(engine, blocks=4)
    feed(engine, frames=123)
    engine.toggle_record()
    until(lambda: engine.pending == 0 and len(engine.typed) == 1)
    assert len(engine.model.calls[0]) == 4 * appmod.BLOCK + 123


def test_one_hundred_rapid_start_commit_pairs(engine):
    for _ in range(100):
        assert engine.toggle_record() is True
        feed(engine, frames=211)
        assert engine.toggle_record() is False
    until(lambda: len(engine.typed) == 100 and engine.pending == 0, 8)
    assert len(engine.model.calls) == 100
    assert all(len(audio) == 211 for audio in engine.model.calls)


def test_empty_double_press_does_not_type(engine):
    engine.toggle_record()
    engine.toggle_record()
    until(lambda: engine.pending == 0)
    assert not engine.typed and not engine.model.calls


def test_silence_only_session_does_not_type(engine):
    engine.toggle_record()
    feed(engine, blocks=25, value=0)
    engine.toggle_record()
    until(lambda: engine.pending == 0)
    assert not engine.typed and not engine.model.calls


def test_pause_resume_keeps_the_same_recording(engine):
    engine.toggle_record()
    feed(engine, blocks=8)
    engine.toggle_pause()
    assert engine.recording and engine.paused
    time.sleep(0.25)  # exercise the segmenter's inactive timeout
    engine.toggle_pause()
    feed(engine, blocks=9)
    engine.toggle_record()
    until(lambda: engine.pending == 0 and len(engine.typed) == 1)
    assert len(engine.model.calls[0]) == 17 * appmod.BLOCK


def test_commit_while_paused_still_types(engine):
    engine.toggle_record()
    feed(engine, blocks=10)
    engine.toggle_pause()
    assert engine.toggle_record() is False
    until(lambda: engine.pending == 0 and len(engine.typed) == 1)


def test_idle_unload_cannot_interrupt_recording(engine):
    app = controller(engine)
    engine.toggle_record()
    engine.last_used = time.time() - 999999
    engine.speaking = False  # guard must not depend on a flickering UI/VAD flag
    app.check_idle()
    assert engine.model is not None and engine.recording
    assert engine.unload() is False


def test_idle_unload_cannot_interrupt_pending_commit(engine):
    app = controller(engine)
    engine.model.gate = threading.Event()
    engine.toggle_record()
    feed(engine, blocks=9)
    engine.toggle_record()
    until(lambda: engine.model.started.is_set())
    engine.last_used = time.time() - 999999
    engine.speaking = False
    app.check_idle()
    assert engine.model is not None and engine.pending == 1
    assert engine.unload() is False
    engine.model.gate.set()
    until(lambda: engine.pending == 0)


def test_idle_unload_still_works_after_finished_message(engine):
    app = controller(engine)
    engine.last_used = time.time() - 999999
    model = engine.model
    app.check_idle()
    assert engine.model is None and model.closed


def test_no_more_output_after_commit_until_next_start(engine):
    engine.toggle_record()
    feed(engine, blocks=12)
    engine.toggle_record()
    until(lambda: engine.pending == 0 and len(engine.typed) == 1)
    feed(engine, blocks=400)
    feed(engine, blocks=60, value=0)
    time.sleep(0.25)
    assert engine.typed == ['part1 ']
    assert len(engine.model.calls) == 1


def test_consecutive_recordings_are_two_deliberate_messages(engine):
    for blocks in (12, 20):
        engine.toggle_record()
        feed(engine, blocks=blocks)
        engine.toggle_record()
    until(lambda: engine.pending == 0 and len(engine.typed) == 2)
    assert list(map(len, engine.model.calls)) == [12 * appmod.BLOCK, 20 * appmod.BLOCK]
    assert engine.typed == ['part1 ', 'part2 ']


def test_stale_auto_message_cannot_leak_into_shortcut_session(engine):
    model = engine.model
    model.gate = threading.Event()
    engine.set_listening(True)
    feed(engine, blocks=20)
    feed(engine, blocks=30, value=0)
    until(model.started.is_set)
    engine.toggle_record()
    model.gate.set()
    until(lambda: engine.pending == 0)
    assert not engine.typed
    feed(engine, blocks=15)
    engine.toggle_record()
    until(lambda: engine.pending == 0 and len(engine.typed) == 1)
    assert engine.typed == ['part2 ']


def test_explicit_mic_off_cancels_uncommitted_audio_only(engine):
    engine.toggle_record()
    feed(engine, blocks=20)
    engine.set_listening(False)
    time.sleep(0.25)
    assert not engine.recording and not engine.typed
    engine.toggle_record()
    feed(engine, blocks=7)
    engine.toggle_record()
    until(lambda: engine.pending == 0 and len(engine.typed) == 1)
    assert len(engine.model.calls[0]) == 7 * appmod.BLOCK


def test_model_generation_change_discards_stale_inference(engine):
    model = engine.model
    model.gate = threading.Event()
    engine.toggle_record()
    feed(engine, blocks=10)
    engine.toggle_record()
    until(model.started.is_set)
    engine._model_generation += 1
    model.gate.set()
    until(lambda: engine.pending == 0)
    assert not engine.typed


def test_loading_model_does_not_open_mic(engine):
    app = controller(engine)
    syncs = engine.syncs
    app.on_ready()
    assert engine.syncs == syncs
    assert not engine.listening and not engine.recording


def test_shortcut_waits_for_cold_reload_then_records(engine, tmp_path):
    path = tmp_path / 'model.gguf'
    path.touch()
    app = controller(engine, str(path))
    model, engine.model = engine.model, None
    app.on_dictation_key()
    assert app._dictation_after_load
    app.reload_model.assert_called_once()
    engine.model = model
    app.on_ready()
    assert engine.recording and not app._dictation_after_load


def test_second_press_during_cold_load_cancels_start(engine, tmp_path):
    path = tmp_path / 'model.gguf'
    path.touch()
    app = controller(engine, str(path))
    model, engine.model = engine.model, None
    app.on_dictation_key()
    engine.loading = True
    app.on_dictation_key()
    assert not app._dictation_after_load
    engine.model, engine.loading = model, False
    app.on_ready()
    assert not engine.recording


def test_auto_enter_once_for_many_internal_chunks(engine, monkeypatch):
    # Use the real _type implementation with a fake keyboard backend.
    e = engine
    keyboard = types.ModuleType('pynput.keyboard')
    keyboard.Key = types.SimpleNamespace(enter='ENTER')
    monkeypatch.setitem(sys.modules, 'pynput.keyboard', keyboard)
    e._kb = Mock()
    e.auto_enter = True
    e._type = types.MethodType(appmod.Engine._type, e)
    e.toggle_record()
    feed(e, seconds=53)
    e.toggle_record()
    until(lambda: e.pending == 0 and e._kb.type.call_count == 1)
    e._kb.type.assert_called_once_with(' '.join(f'part{i+1}' for i in range(len(e.model.calls))) + ' ')
    e._kb.press.assert_called_once_with('ENTER')
    e._kb.release.assert_called_once_with('ENTER')


def test_auto_enter_false_still_only_types_once(engine):
    e = engine
    e._kb = Mock()
    e.auto_enter = False
    e._type = types.MethodType(appmod.Engine._type, e)
    e.toggle_record()
    feed(e, blocks=15)
    e.toggle_record()
    until(lambda: e.pending == 0 and e._kb.type.call_count == 1)
    e._kb.press.assert_not_called()


def test_wait_for_windows_shortcut_modifiers(engine, monkeypatch):
    e = engine
    e._kb = Mock()
    e._type = types.MethodType(appmod.Engine._type, e)
    held = threading.Event()
    held.set()
    windll = types.SimpleNamespace(user32=types.SimpleNamespace(
        GetAsyncKeyState=lambda key: 0x8000 if held.is_set() else 0))
    monkeypatch.setattr(appmod.ctypes, 'windll', windll, raising=False)
    monkeypatch.setattr(appmod, 'os', types.SimpleNamespace(name='nt'))
    worker = threading.Thread(target=e._type, args=('whole message ', e._model_generation))
    worker.start()
    time.sleep(.05)
    e._kb.type.assert_not_called()
    held.clear()
    worker.join(1)
    e._kb.type.assert_called_once_with('whole message ')


def tiny_gguf(path, cpp=False):
    def string(s):
        b = s.encode()
        return struct.pack('<Q', len(b)) + b
    pairs = [('general.architecture','parakeet')]
    if cpp:
        pairs.append(('stt.variant','parakeet-unified-en-0.6b'))
    data = b'GGUF' + struct.pack('<IQQ', 3, 1, len(pairs))
    for key, value in pairs:
        data += string(key) + struct.pack('<I', 8) + string(value)
    data += b'\0' * 64
    path.write_bytes(data)
    return path


@pytest.mark.parametrize('cpp,runtime', [(True,'transcribe_cpp'),(False,'crispasr')])
def test_runtime_detected_from_metadata_not_filename(tmp_path, cpp, runtime):
    path = tiny_gguf(tmp_path / 'arbitrarily-renamed.gguf', cpp)
    assert appmod._gguf_runtime(path) == runtime


def test_corrupt_gguf_rejected(tmp_path):
    path = tmp_path / 'bad.gguf'
    path.write_text('<html>Not a model</html>')
    with pytest.raises(ValueError):
        appmod._gguf_runtime(path)


def test_unified_adapter_uses_matching_native_api(monkeypatch):
    model = Mock()
    session = Mock()
    model.session.return_value = session
    session.run.return_value = types.SimpleNamespace(text='Unified works')
    module = types.SimpleNamespace(Model=Mock(return_value=model))
    monkeypatch.setitem(sys.modules, 'transcribe_cpp', module)
    adapter = appmod._UnifiedSession('model.gguf', n_threads=3)
    module.Model.assert_called_once_with('model.gguf', backend='cpu')
    model.session.assert_called_once_with(n_threads=3)
    assert adapter.transcribe(np.zeros(16000, np.float32)) == 'Unified works'
    assert session.run.call_args.args[0].dtype == np.float32
    adapter.close()
    session.close.assert_called_once()
    model.close.assert_called_once()
