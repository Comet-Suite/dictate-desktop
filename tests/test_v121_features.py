"""v1.2.1: literal mappings/newlines, safe unloading, quantizations and binary IPC."""
import gc
from pathlib import Path
import struct
import sys
import threading
import types
from unittest.mock import Mock
import weakref

import numpy as np
import pytest
from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QScrollArea

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as m
from test_targeted_fix import Engine, feed, until, tiny_gguf
from test_focus_symbols import harness as harness, qt_focus as qt_focus, pump, pump_until
from test_windows_runtime import Pipe, spawn_stub


@pytest.mark.parametrize('spoken,expected', [
    ('hello enter world', 'hello\nworld'), ('Hello, enter, world.', 'Hello\nworld.'),
    ('hello. Enter. world', 'hello.\nworld'), ('one enter two', '1\n2'),
    ('five zero enter twenty enter zerozerofive', '50\n20\n005'),
    ('enter hello enter', '\nhello\n'), ('enter enter', '\n\n'),
    ('hello\tENTER\tworld', 'hello\nworld'), ('literal enter', 'enter'),
    ('the literal enter word', 'the enter word'), ('entering the center', 'entering the center'),
    ('re-enter the word', 're-enter the word'), ('enter.com', 'enter.com'),
    ('https://enter.example/enter?q=one', 'https://enter.example/enter?q=one'),
    ('enter@example.com', 'enter@example.com'), ('www.enter.example', 'www.enter.example'),
    (r'C:\enter\file.txt', r'C:\enter\file.txt'),
    ('four hundred dollars enter at sign', '$400\n@'),
])
def test_spoken_enter_is_text_not_an_action(spoken, expected):
    assert m.normalize_dictation_text(spoken) == expected


@pytest.mark.parametrize('definition,spoken,expected', [
    ('happy face = :)', 'happy face', ':)'),
    ('happy face = :)', 'HAPPY   FACE', ':)'),
    ('my token = five zero', 'my token', 'five zero'),
    ('my token = enter at sign twenty', 'my token', 'enter at sign twenty'),
    ('work mail = person@example.com', 'work mail', 'person@example.com'),
    ('x = "  indented  "', 'x', '  indented  '),
    (r'paragraph = \n\n', 'paragraph', '\n\n'),
    (r'path = C:\\Users\\Me', 'path', r'C:\Users\Me'),
    (r'tab item = a\tb', 'tab item', 'a\tb'),
    ('equality = a=b=c', 'equality', 'a=b=c'),
    ('enter = <enter>', 'enter', '<enter>'),
    ('hash tag = tag', 'hash tag', 'tag'),
    ('face = short\nhappy face = long', 'happy face', 'long'),
    ('word = first\nWORD = second', 'word', 'second'),
    ('straße = ✓', 'Straße STRASSE', '✓ ✓'),
    ('κόσμος = ✓', 'ΚΌΣΜΟΣ', '✓'),
    ('a+b = literal', 'a+b', 'literal'),
    ('cat = dog', 'concatenate cat cats', 'concatenate dog cats'),
    ('x = "__import__(\\"os\\").system(\\"unsafe\\")"', 'x', '__import__("os").system("unsafe")'),
    ('enter = override', 'https://site/enter enter@example.com', 'https://site/enter enter@example.com'),
])
def test_custom_mapping_is_safe_literal_utf8_data(tmp_path, definition, spoken, expected):
    path = tmp_path / 'symbols.txt'
    path.write_text(definition, encoding='utf-8')
    symbols = m.CustomSymbols(path)
    assert m.normalize_dictation_text(spoken, symbols) == expected


@pytest.mark.parametrize('bad', ['missing separator', '= no phrase', 'x = "broken', 'x = "\\u0000"', 'x\0 = y', 'x' * 257 + ' = y'])
def test_malformed_mappings_have_line_or_limit_errors(bad):
    with pytest.raises(ValueError):
        m.CustomSymbols.parse(bad)


def test_mapping_edits_reload_and_invalid_edits_keep_last_good_version(tmp_path):
    path = tmp_path / 'symbols.txt'
    path.write_text('token = original', encoding='utf-8')
    symbols = m.CustomSymbols(path)
    path.write_text('token = changed value', encoding='utf-8')
    assert m.normalize_dictation_text('token', symbols) == 'changed value'
    path.write_text('broken edit', encoding='utf-8')
    assert m.normalize_dictation_text('token', symbols) == 'changed value'
    assert 'Last valid mappings' in symbols.error
    path.unlink()
    assert m.normalize_dictation_text('token', symbols) == 'changed value'
    path.write_text('token = recovered!', encoding='utf-8')
    assert m.normalize_dictation_text('token', symbols) == 'recovered!'
    assert not symbols.error
    symbols.configure('')
    assert m.normalize_dictation_text('token', symbols) == 'token'


def test_mapping_limits_comments_bom_and_no_file_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(m, 'APP_DIR', tmp_path)
    monkeypatch.setattr(m, 'save_settings', Mock())
    state = dict(m.DEFAULTS)
    path = m._custom_symbols_file(state)
    assert m.CustomSymbols.parse(path.read_text()) == {}
    path.write_text('\ufeff#comment\n\nphrase = # literal comment', encoding='utf-8')
    assert m._custom_symbols_file(state).read_text(encoding='utf-8-sig').endswith('# literal comment')
    assert m.CustomSymbols(path)._values == {'phrase': '# literal comment'}
    wrong = tmp_path / 'wrong.py'; wrong.write_text('x = y')
    with pytest.raises(ValueError, match='.txt'):
        m.CustomSymbols(wrong)
    path.write_bytes(b'x' * (m.CustomSymbols.MAX_BYTES + 1))
    with pytest.raises(ValueError, match='4 MiB'):
        m.CustomSymbols(path)
    with pytest.raises(ValueError, match='5,000'):
        m.CustomSymbols.parse('\n'.join(f'alias {i} = ok' for i in range(5001)))


@pytest.mark.parametrize('file,quant', [('m-Q4_K_M.gguf','Q4_K_M'), ('m-q5_0.gguf','Q5_0'),
    ('nested/m.Q8_0.gguf','Q8_0'), ('m-f16.gguf','F16'), ('m-q6_k.gguf','Q6_K'),
    ('my-renamed-file.gguf','unknown')])
def test_quantization_filename_detection(file, quant):
    assert m._model_quantization_name(file) == quant


@pytest.mark.parametrize('quant,expected', [('q4_k', 'm-Q4_K_M.gguf'), ('q4_k_s','m-Q4_K_S.gguf'),
    ('q5_k_m','m-Q5_K_M.gguf'), ('q8_0','m-Q8_0.gguf'), ('f16','m-F16.gguf'), ('','m-Q4_K_M.gguf')])
def test_quantization_selection_is_exact_and_low_memory(quant, expected):
    files = ['m-F16.gguf', 'm-Q8_0.gguf', 'm-Q5_K_M.gguf', 'm-Q4_K_S.gguf', 'm-Q4_K_M.gguf', 'readme.txt']
    assert m.select_model_file(files, quant) == expected


@pytest.mark.parametrize('quant', ['q5_0','q6_k','f32','q4_0'])
def test_absent_quantization_never_downloads_an_arbitrary_larger_model(quant):
    with pytest.raises(ValueError, match='not available'):
        m.select_model_file(['large-F16.gguf','m-Q8_0.gguf'], quant)
    with pytest.raises(ValueError, match='not available'):
        m.select_model_file(['unlabeled.gguf'], 'f16')


@pytest.mark.parametrize('version', [2,3])
@pytest.mark.parametrize('file_type,quant', [(0,'F32'),(1,'F16'),(7,'Q8_0'),(15,'Q4_K_M'),(17,'Q5_K_M'),(18,'Q6_K')])
def test_full_gguf_metadata_after_runtime_marker(version, file_type, quant, tmp_path):
    def string(value):
        value=value.encode();return struct.pack('<Q',len(value))+value
    items = [('general.architecture',8,string('parakeet')), ('stt.variant',8,string('parakeet-unified-en-0.6b')),
             ('unrelated.array',9,struct.pack('<IQ3I',4,3,1,2,3)), ('general.file_type',4,struct.pack('<I',file_type))]
    blob=b'GGUF'+struct.pack('<IQQ',version,1,len(items))
    for key,kind,value in items:
        blob+=string(key)+struct.pack('<I',kind)+value
    path=tmp_path/'renamed.gguf';path.write_bytes(blob+b'\0'*64)
    info=m._gguf_info(path)
    assert info['runtime']=='transcribe_cpp' and info['quantization']==quant
    assert info['architecture']=='parakeet' and info['bytes']==path.stat().st_size


def test_multiple_models_in_folder_require_an_explicit_choice(qt, tmp_path):
    tiny_gguf(tmp_path/'one.gguf');tiny_gguf(tmp_path/'two.gguf')
    e=Engine(); errors=[];e.model_error.connect(errors.append)
    try:
        e._load(tmp_path)
        assert errors and 'several models' in errors[0]
    finally:e.shutdown()


@pytest.mark.parametrize('flag', ['recording','speaking','busy','loading','_pending_jobs'])
def test_manual_unload_cannot_discard_active_or_pending_dictation(qt, flag):
    e=Engine(); model=e.model
    try:
        setattr(e,flag,True)
        assert e.unload() is False and e.model is model and not model.closed
    finally:
        setattr(e,flag,False);e.shutdown()


def test_unload_releases_model_and_shutdown_is_idempotent(qt):
    e=Engine();model=e.model
    assert e.unload() and e.model is None and model.closed
    assert e.unloaded_idle and not e.listening
    e.shutdown();e.shutdown()
    assert not e._segment_thread.is_alive() and not e._transcribe_thread.is_alive()


def test_idle_worker_does_not_pin_the_last_audio_array(qt):
    e=Engine();refs=[]
    def run(audio,sample_rate=16000):
        refs.append(weakref.ref(audio));return 'finished'
    e.model.transcribe=run
    try:
        e.toggle_record();feed(e,blocks=12);e.toggle_record()
        until(lambda:e.pending==0 and bool(e.typed))
        gc.collect()
        assert refs and all(ref() is None for ref in refs)
    finally:e.shutdown()


@pytest.mark.parametrize('shape',[(2,10),(10,2)])
def test_ipc_rejects_stereo_before_sending(monkeypatch,shape):
    parent,_,_=spawn_stub(monkeypatch);session=m._WindowsNativeSession('model.gguf')
    with pytest.raises(ValueError,match='mono'):session.transcribe(np.zeros(shape))
    assert not parent.sent and not parent.byte_packets
    session.close()


def test_ipc_worker_reads_binary_float32_without_array_unpickling(monkeypatch):
    seen=[];closed=[]
    def run(audio,sample_rate=16000):
        seen.append((audio.flags.writeable,audio.copy()));return 'ok'
    monkeypatch.setattr(m,'_UnifiedSession',lambda *a,**kw:types.SimpleNamespace(transcribe=run,close=lambda:closed.append(True)))
    audio=np.arange(20,dtype=np.float32)
    pipe=Pipe((('transcribe',len(audio)),audio.tobytes(),('close',None)))
    m._native_session_worker(pipe,'transcribe_cpp','model.gguf',4)
    assert pipe.sent==[('ready',None),('text','ok')] and closed==[True]
    assert seen[0][0] is False
    np.testing.assert_array_equal(seen[0][1],audio)


@pytest.mark.parametrize('packet',[(8,b'bad'),(-1,b''),(m.SR*121,b'')])
def test_ipc_rejects_malformed_frames(monkeypatch,packet):
    run=Mock(return_value='should not run')
    monkeypatch.setattr(m,'_UnifiedSession',lambda *a,**kw:types.SimpleNamespace(transcribe=run,close=lambda:None))
    count,data=packet
    replies=[('transcribe',count)]
    if 0<=count<=m.SR*120:replies.append(data)
    replies.append(('close',None));pipe=Pipe(replies)
    m._native_session_worker(pipe,'transcribe_cpp','m.gguf',4)
    assert pipe.sent[1][0]=='error';run.assert_not_called()


def test_manual_unload_button_and_new_controls_stay_inside_columns(harness):
    app,e,d,*_=harness
    d.resize(960,720);pump(app,50)
    scroll=d.findChild(QScrollArea)
    for control in (d.btn_unload,d.symbol_path,d.updates_toggle):
        point=control.mapTo(scroll.widget(),QPoint(0,0))
        assert point.x()+control.width()<=scroll.widget().width()
    e.recording=True;d._tick();assert not d.btn_unload.isEnabled()
    e.recording=False;d._tick();assert d.btn_unload.isEnabled()
    d.btn_unload.click();assert e.model is None


def test_mapping_select_and_reload_are_persistent_without_recompiling(harness,tmp_path,monkeypatch):
    _,e,d,*_=harness;save=Mock();monkeypatch.setattr(m,'save_settings',save)
    path=tmp_path/'custom.txt';path.write_text('smile = :)',encoding='utf-8')
    assert d._set_symbols(path)
    assert d.settings['custom_symbols_path']==str(path.resolve()) and save.called
    assert e._normalize_text('smile')==':)'
    path.write_text('broken edit',encoding='utf-8')
    assert not d._set_symbols(path)
    assert e._normalize_text('smile')==':)'


def test_newline_mapping_is_assembled_across_chunks_and_inserted_only_at_stop(qt,tmp_path):
    e=Engine();utterances=iter(('hello enter','world enter five zero'));started=threading.Event()
    def transcribe(*args,**kwargs):
        started.set();return next(utterances)
    e.model.transcribe=transcribe
    try:
        e.toggle_record();feed(e,seconds=10)
        until(started.is_set)
        # Wait for the privately staged first part; never a visible preview.
        until(lambda:not e._seg_q.qsize() and not e.busy)
        assert not e.typed
        e.toggle_record();until(lambda:e.pending==0 and bool(e.typed))
        assert e.typed==['hello\nworld\n50 ']
    finally:e.shutdown()


def test_multiline_clipboard_failure_never_falls_back_to_enter_key(qt):
    e=Engine();e._type=types.MethodType(m.Engine._type,e);e._kb=Mock();errors=[];e.message.connect(errors.append)
    try:
        e._type('hello\nworld ')
        e._kb.type.assert_not_called();e._kb.press.assert_not_called()
        assert errors and 'no Enter key was sent' in errors[0]
    finally:e.shutdown()


@pytest.mark.parametrize('auto_enter',[False,True])
def test_spoken_newlines_are_a_single_native_paste_without_implicit_enter(harness,auto_enter):
    from PyQt6.QtWidgets import QApplication
    from test_latency_natural import KeyRecorder
    app,e,d,*_=harness
    if QApplication.platformName()!='xcb':pytest.skip('native insertion requires X11')
    e.auto_enter=auto_enter;e._type=types.MethodType(m.Engine._type,e);e._clipboard_bridge=m._ClipboardBridge(app)
    e.model.transcribe=lambda *args,**kwargs:'hello enter world enter five zero'
    d.test.selectAll();d.test.setFocus();recorder=KeyRecorder(d.test);d.test.installEventFilter(recorder)
    e.toggle_record();feed(e,blocks=16);assert not e.typed
    e.toggle_record();pump_until(app,lambda:e.pending==0 and not e.busy)
    pump_until(app,lambda:d.test.toPlainText().startswith('hello\nworld'))
    pump(app,120)
    assert d.test.toPlainText()=='hello\nworld\n50 '+ ('\n' if auto_enter else '')
    assert recorder.pastes==1 and recorder.enters==(1 if auto_enter else 0)
    e._clipboard_bridge._restore(e._clipboard_bridge._token)


@pytest.mark.parametrize('code,expected',[(7,'Q8_0'),(8,'Q5_0'),(11,'Q3_K'),(12,'Q4_K'),(13,'Q5_K'),(14,'Q6_K')])
def test_crisp_metadata_uses_ggml_file_types_not_unified_mixture_codes(tmp_path,code,expected):
    path=tiny_gguf(tmp_path/'renamed.gguf')
    blob=bytearray(path.read_bytes());blob[16:24]=struct.pack('<Q',2)
    key=b'general.file_type'
    blob=blob[:-64]+struct.pack('<Q',len(key))+key+struct.pack('<II',4,code)+b'\0'*64
    path.write_bytes(blob)
    assert m._gguf_info(path)['quantization']==expected
