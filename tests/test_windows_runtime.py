"""Local IPC protocol and Windows native-runtime isolation regressions."""
from collections import deque
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as m


class Pipe:
    def __init__(self, replies=(), available=True):
        self.replies = deque(replies)
        self.sent = []
        self.available = available
        self.closed = False

    def send(self, message):
        self.sent.append(message)

    def recv(self):
        if not self.replies:
            raise EOFError
        return self.replies.popleft()

    def poll(self, timeout):
        assert timeout == 180
        return self.available

    def close(self):
        self.closed = True


class Process:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.terminated = False
        self.alive = False
        self.joins = []

    def start(self):
        self.started = True

    def join(self, timeout):
        self.joins.append(timeout)

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated = True
        self.alive = False


def spawn_stub(monkeypatch, replies=(("ready", None),), available=True):
    parent, child = Pipe(replies, available), Pipe()
    process = Process()

    def create(**kwargs):
        process.kwargs = kwargs
        return process

    context = SimpleNamespace(Pipe=lambda: (parent, child), Process=create)

    def get_context(method):
        assert method == "spawn"
        return context

    monkeypatch.setattr(m.multiprocessing, "get_context", get_context)
    return parent, child, process


@pytest.mark.parametrize("runtime", ["transcribe_cpp", "crispasr"])
def test_windows_dispatch_does_not_load_native_dlls_in_gui(monkeypatch, runtime):
    monkeypatch.setattr(m, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(m.importlib.util, "find_spec", lambda name: object())

    def unexpected_import(name):
        raise AssertionError("Native runtime imported in GUI process: " + name)

    monkeypatch.setattr(m.importlib, "import_module", unexpected_import)
    factory = m.Engine._import_engine(object(), runtime)
    assert factory.func is m._WindowsNativeSession
    assert factory.keywords == {"runtime": runtime}


def test_proxy_spawns_one_persistent_local_worker(monkeypatch):
    parent, child, process = spawn_stub(monkeypatch)
    session = m._WindowsNativeSession("model.gguf", 3, runtime="crispasr")
    assert process.started and child.closed
    assert process.kwargs["target"] is m._native_session_worker
    assert process.kwargs["args"] == (child, "crispasr", "model.gguf", 3)
    assert process.kwargs["daemon"] is True
    session.close()
    session.close()
    assert parent.closed and parent.sent == [("close", None)]


def test_proxy_returns_text_and_sends_contiguous_float32_audio(monkeypatch):
    parent, _, process = spawn_stub(monkeypatch, (("ready", None), ("text", "whole message")))
    session = m._WindowsNativeSession("model.gguf")
    audio = np.arange(20, dtype=np.float64)[::2]
    assert session.transcribe(audio) == "whole message"
    command, payload = parent.sent[0]
    assert command == "transcribe"
    assert payload.dtype == np.float32 and payload.flags.c_contiguous
    np.testing.assert_array_equal(payload, audio)
    assert process.started
    session.close()


def test_proxy_rejects_wrong_audio_rate_without_sending(monkeypatch):
    parent, _, _ = spawn_stub(monkeypatch)
    session = m._WindowsNativeSession("model.gguf")
    with pytest.raises(ValueError, match="16 kHz"):
        session.transcribe(np.zeros(10), sample_rate=48000)
    assert not parent.sent
    session.close()


def test_proxy_surfaces_worker_errors(monkeypatch):
    _, _, _ = spawn_stub(monkeypatch, (("ready", None), ("error", "model rejected audio")))
    session = m._WindowsNativeSession("model.gguf")
    with pytest.raises(RuntimeError, match="model rejected audio"):
        session.transcribe(np.zeros(10))
    session.close()


def test_startup_failure_closes_worker_and_pipe(monkeypatch):
    parent, _, process = spawn_stub(monkeypatch, (("error", "missing native DLL"),))
    with pytest.raises(RuntimeError, match="missing native DLL"):
        m._WindowsNativeSession("model.gguf")
    assert parent.closed and process.joins == [2]


def test_startup_timeout_cleans_up(monkeypatch):
    parent, _, process = spawn_stub(monkeypatch, available=False)
    process.alive = True
    with pytest.raises(RuntimeError, match="did not respond"):
        m._WindowsNativeSession("model.gguf")
    assert parent.closed and process.terminated
    assert process.joins == [2, 2]


def test_dead_worker_is_reported_without_hanging(monkeypatch):
    parent, _, _ = spawn_stub(monkeypatch)
    session = m._WindowsNativeSession("model.gguf")
    with pytest.raises(RuntimeError, match="stopped unexpectedly"):
        session.transcribe(np.zeros(10))
    session.close()
    assert parent.closed


@pytest.mark.parametrize("runtime", ["transcribe_cpp", "crispasr"])
def test_worker_loads_only_requested_runtime_and_closes_it(monkeypatch, runtime):
    model = SimpleNamespace(transcribe=lambda audio, sample_rate: "private words", close=lambda: closed.append(True))
    closed, calls = [], []

    def unified(path, n_threads):
        calls.append(("unified", path, n_threads))
        return model

    def crisp(path, n_threads, backend):
        calls.append((backend, path, n_threads))
        return model

    monkeypatch.setattr(m, "_UnifiedSession", unified)
    monkeypatch.setitem(sys.modules, "crispasr", SimpleNamespace(Session=crisp))
    pipe = Pipe((("transcribe", np.zeros(10)), ("close", None)))
    m._native_session_worker(pipe, runtime, "model.gguf", 4)
    assert calls == [("unified" if runtime == "transcribe_cpp" else "cpu", "model.gguf", 4)]
    assert pipe.sent == [("ready", None), ("text", "private words")]
    assert pipe.closed and closed == [True]


def test_worker_reports_native_initialization_failure(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("native initialization failed")

    monkeypatch.setattr(m, "_UnifiedSession", unavailable)
    pipe = Pipe()
    m._native_session_worker(pipe, "transcribe_cpp", "model.gguf", 4)
    assert pipe.sent == [("error", "native initialization failed")]
    assert pipe.closed
