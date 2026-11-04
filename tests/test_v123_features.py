"""v1.2.3: measured updater progress, conservative model lifecycle and catalog UI."""
import ast
import hashlib
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QFrame, QScrollArea

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as m
from test_targeted_fix import Engine, feed, until, tiny_gguf
from test_focus_symbols import pump
from test_updates import release, downloads, PAYLOAD, application, dialog

ROOT = Path(__file__).resolve().parents[1]
PROTECTED = json.loads((ROOT/'tests/fixtures/v123_preserved_behavior.json').read_text())['protected_ast']


def canonical(value):
    if isinstance(value, ast.AST):
        return {'type': type(value).__name__, 'fields': {
            key: canonical(item) for key, item in ast.iter_fields(value) if item is not None and item != []}}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    if isinstance(value, bytes):
        return {'bytes': value.hex()}
    return value


@pytest.mark.parametrize('name', PROTECTED)
def test_unrelated_runtime_paths_still_match_released_v122(name):
    nodes = ast.parse((ROOT/'dictate.py').read_text()).body
    parts = name.split('.')
    node = next(n for n in nodes if getattr(n, 'name', '') == parts[0])
    if len(parts) > 1:
        node = next(n for n in node.body if getattr(n, 'name', '') == parts[1])
    digest = hashlib.sha256(json.dumps(canonical(node), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert digest == PROTECTED[name], name


def test_both_lifecycle_defaults_migrate_without_overwriting_old_preferences(tmp_path, monkeypatch):
    settings = tmp_path/'settings.json'
    monkeypatch.setattr(m, 'SETTINGS_FILE', settings)
    original = {'auto_enter': False, 'hotkey': 'Ctrl+Alt+D', 'dict_hotkey': 'Ctrl+Alt+Space',
                'start_on_startup': True, 'model_path': 'existing.gguf', 'unload_min': 10}
    settings.write_text(json.dumps(original))
    loaded = m.load_settings()
    assert loaded['unload_after_paste'] is True and loaded['preload_on_popup'] is True
    assert all(loaded[key] == value for key, value in original.items())
    original.update(unload_after_paste=False, preload_on_popup=False)
    settings.write_text(json.dumps(original))
    loaded = m.load_settings()
    assert not loaded['unload_after_paste'] and not loaded['preload_on_popup']


@pytest.mark.parametrize('success,enabled', [(True, True), (False, True), (True, False), (None, True)])
def test_post_paste_unload_requires_actual_success_and_opt_in(qt, success, enabled):
    e = Engine()
    old = e.model
    e.unload_after_paste = enabled
    pasted = []
    def insert(text, generation=None):
        assert e.busy and e.pending == 1 and e.model is old and not old.closed
        pasted.append(text)
        return success
    e._type = insert
    try:
        e.toggle_record(); feed(e, blocks=14); e.toggle_record()
        until(lambda: bool(pasted) and e.pending == 0 and not e.busy)
        if success is True and enabled:
            until(lambda: e.model is None)
            assert old.closed and not e.listening and e.unload_reason == 'paste'
        else:
            assert e.model is old and not old.closed
    finally:
        e.shutdown()


def test_post_paste_unload_does_not_destroy_a_new_recording(qt):
    e = Engine(); old = e.model; e.unload_after_paste = True
    def insert(text, generation=None):
        assert e.toggle_record() is True  # a new shortcut session begins during insertion
        return True
    e._type = insert
    try:
        e.toggle_record(); feed(e, blocks=14); e.toggle_record()
        until(lambda: e.recording and e.pending == 0 and not e.busy)
        assert e.model is old and not old.closed
    finally:
        e.shutdown()


def test_old_success_cannot_unload_a_replacement_model(qt):
    e = Engine(); generation = e._model_generation
    try:
        e._model_generation += 1
        assert e.unload(reason='paste', expected_generation=generation) is False
        assert e.model is not None and not e.model.closed
        assert e.unload(reason='paste', expected_generation=e._model_generation)
    finally:
        e.shutdown()


def test_background_stages_and_silence_never_trigger_post_paste_unload(qt):
    e = Engine(); e.unload_after_paste = True; old = e.model
    try:
        e.toggle_record(); feed(e, seconds=13)
        until(lambda: bool(old.calls))
        assert e.model is old and not old.closed and e.recording and not e.typed
        e.set_listening(False)
        until(lambda: not e.busy)
        assert e.model is old
        e.toggle_record(); feed(e, blocks=30, value=0); e.toggle_record()
        until(lambda: e.pending == 0 and not e.busy)
        assert e.model is old and not old.closed
    finally:
        e.shutdown()


def test_loader_will_not_race_an_unload(qt, tmp_path):
    e = Engine()
    try:
        e._unloading = True
        assert e.load_model(tmp_path/'new.gguf') is False
        assert not e.loading and e._loading_thread is None
    finally:
        e._unloading = False; e.shutdown()


def popup_app(e, path):
    app = m.App.__new__(m.App)
    app.engine = e; app.settings = dict(m.DEFAULTS, model_path=str(path))
    app._quitting = app._dictation_after_load = False
    app.say = Mock(); app.dialog = None
    class Popup:
        user_hidden = True
        visible = False
        def isVisible(self): return self.visible
        def show_popup(self): self.visible = True; self.user_hidden = False
        def hide_popup(self): self.visible = False; self.user_hidden = True
    app.pill = Popup()
    return app


@pytest.mark.parametrize('enabled', [True, False])
def test_visibility_shortcut_preloads_without_recording(qt, tmp_path, enabled):
    e = Engine(); e.model.close(); e.model = None
    app = popup_app(e, tiny_gguf(tmp_path/'chosen.gguf'))
    app.settings['preload_on_popup'] = enabled
    e.load_model = Mock(return_value=True)
    try:
        app.toggle_popup()
        assert app.pill.visible
        assert e.load_model.call_count == int(enabled)
        assert not e.recording and not e.listening and not app._dictation_after_load
        app.on_ready()
        assert not e.recording and not e.listening
        app.toggle_popup()
        assert not app.pill.visible
        assert e.load_model.call_count == int(enabled)  # hiding never preloads
    finally:
        e.shutdown()


def test_popup_preload_is_idempotent_while_loading_or_ready(qt, tmp_path):
    e = Engine(); app = popup_app(e, tiny_gguf(tmp_path/'chosen.gguf'))
    e.load_model = Mock(return_value=True)
    try:
        app.toggle_popup(); e.load_model.assert_not_called()
        app.toggle_popup(); e.model = None; e.loading = True
        app.toggle_popup(); e.load_model.assert_not_called()
        assert not app._dictation_after_load and not e.recording
    finally:
        e.loading = False; e.shutdown()


def test_deferred_popup_preload_is_cancelled_when_popup_is_hidden(qt, tmp_path, monkeypatch):
    e = Engine(); e.model.close(); e.model = None; e._unloading = True
    app = popup_app(e, tiny_gguf(tmp_path/'chosen.gguf'))
    app.pill.show_popup(); deferred = []
    monkeypatch.setattr(m.QTimer, 'singleShot', lambda ms, callback: deferred.append(callback))
    e.load_model = Mock(return_value=True)
    try:
        assert not app.reload_model(purpose='popup') and len(deferred) == 1
        app.pill.hide_popup(); e._unloading = False
        deferred[0](); e.load_model.assert_not_called()
        assert not app._dictation_after_load
    finally:
        e._unloading = False; e.shutdown()


def test_deferred_explicit_record_intent_can_be_cancelled(qt, tmp_path, monkeypatch):
    e = Engine(); e.model.close(); e.model = None; e._unloading = True
    app = popup_app(e, tiny_gguf(tmp_path/'chosen.gguf')); app._dictation_after_load = True
    callbacks = []; monkeypatch.setattr(m.QTimer, 'singleShot', lambda ms, cb: callbacks.append(cb))
    e.load_model = Mock(return_value=True)
    try:
        app.reload_model(purpose='dictation')
        app._dictation_after_load = False; e._unloading = False; callbacks[0]()
        e.load_model.assert_not_called()
    finally:
        e._unloading = False; e.shutdown()


@pytest.mark.parametrize('model', m.MODEL_CATALOG, ids=lambda item: item['name'])
def test_catalog_has_precise_variants_revisions_license_and_checks(model):
    assert model['repo'] in m.PRESET_REPOS and len(model['revision']) == 40
    assert model['languages'] and model['engine'] and model['license'] and model['release_checks']
    variants = {item['quant']: item for item in model['files']}
    assert model['default_quant'] in variants
    assert all(quant in variants for quant in model['release_checks'])
    for quant, item in variants.items():
        assert item['bytes'] > 100_000_000 and item['filename'].endswith('.gguf')
        assert m._catalog_download_file(model['repo'], [v['filename'] for v in model['files']], quant.lower()) == item['filename']
    expected = variants[model['default_quant']]['filename']
    assert m._catalog_download_file(model['repo'], [item['filename'] for item in model['files']], '') == expected
    with pytest.raises(ValueError, match='not available'):
        m._catalog_download_file(model['repo'], [item['filename'] for item in model['files']], 'q2_k')


def test_catalog_never_substitutes_missing_or_undocumented_files():
    model = m.MODEL_CATALOG[0]
    with pytest.raises(ValueError, match='not available'):
        m._catalog_download_file(model['repo'], ['arbitrary-F32.gguf'], 'q4_k_m')
    assert m._catalog_download_file('custom/repo', ['mine-q8_0.gguf'], 'q8_0') == 'mine-q8_0.gguf'
    assert m._catalog_model(None) is None
    # The TDT F16 file is explicitly mapped, not guessed from a missing quant label.
    tdt = m.MODEL_CATALOG[1]
    name = next(v['filename'] for v in tdt['files'] if v['quant'] == 'F16')
    assert m._model_quantization_name(name) == 'unknown'
    assert m._catalog_download_file(tdt['repo'], [name], 'f16') == name


@pytest.mark.parametrize('key', ['unload_after_paste', 'preload_on_popup'])
def test_lifecycle_switches_are_independent_persistent_and_live(qt, monkeypatch, key):
    state = dict(m.DEFAULTS)
    monkeypatch.setattr(m, 'save_settings', Mock()); monkeypatch.setattr(m, 'list_input_devices', lambda: [])
    e = Engine(); d = m.SettingsDialog(state, e)
    other = 'preload_on_popup' if key == 'unload_after_paste' else 'unload_after_paste'
    try:
        assert getattr(d, key).isChecked()
        getattr(d, key).setChecked(False)
        assert state[key] is False and state[other] is True
        assert e.unload_after_paste is state['unload_after_paste']
        m.save_settings.assert_called()
    finally:
        d.tick.stop(); d.close(); e.shutdown(); d.deleteLater()


def test_model_picker_uses_friendly_names_and_model_specific_quants(qt, monkeypatch):
    monkeypatch.setattr(m, 'list_input_devices', lambda: [])
    monkeypatch.setattr(m, 'save_settings', Mock())
    e = Engine(); d = m.SettingsDialog(dict(m.DEFAULTS), e)
    try:
        for model in m.MODEL_CATALOG:
            d._select_repo(model['repo'])
            assert d.repo.currentText() == model['name'] and d._selected_repo() == model['repo']
            choices = {d.combo.itemData(i) for i in range(d.combo.count())}
            assert choices == {item['quant'].lower() for item in model['files']} | {''}
            assert model['engine'] in d.model_note.text()
        d.repo.setCurrentText('custom/another-speech-model'); d._model_selection()
        assert d._selected_repo() == 'custom/another-speech-model'
        assert 'native architecture' in d.model_note.text()
        # A non-catalog item with no userData is still a valid custom text ID.
        d.repo.addItem('custom/no-data'); d.repo.setCurrentIndex(d.repo.count()-1)
        assert d._selected_repo() == 'custom/no-data'
    finally:
        d.close(); e.shutdown(); d.deleteLater()


def test_catalog_dialog_is_scrollable_rounded_and_selects_without_downloading(qt):
    d = m.ModelCatalogDialog(); chosen = []; d.model_selected.connect(chosen.append)
    try:
        d.show(); pump(qt, 30)
        assert len([w for w in d.findChildren(QFrame) if w.objectName() == 'card']) == 5
        scroll = d.findChild(QScrollArea)
        assert scroll.horizontalScrollBar().maximum() == 0
        image = d.grab().toImage()
        assert image.pixelColor(0, 0).alpha() == 0
        assert image.pixelColor(image.width()//2, image.height()//2).alpha() > 200
        d._choose(m.PRESET_REPOS[0]); assert chosen == [m.PRESET_REPOS[0]]
    finally:
        d.close(); d.deleteLater()


def test_measured_progress_reports_download_and_separate_persisted_verification(tmp_path, monkeypatch):
    candidate = m._update_candidate(release()); downloads(monkeypatch, candidate)
    states = []; percentages = []
    result = m._download_update(candidate, tmp_path, progress=percentages.append, details=states.append)
    assert result.read_bytes() == PAYLOAD
    phases = [state['phase'] for state in states]
    assert phases[0] == 'checking' and 'downloading' in phases and phases[-1] == 'verifying'
    transfer = [state for state in states if state['phase'] == 'downloading']
    assert transfer[0]['received'] == 0 and transfer[-1]['received'] == len(PAYLOAD)
    assert percentages[-1] == 100 and transfer[-1]['speed'] > 0
    assert all('ready' != state['phase'] for state in states)  # only the controller can mark verified-ready
    # Cached downloads are rehashed, show verification, and emit 100% without a fake transfer.
    states.clear(); percentages.clear()
    m._download_update(candidate, tmp_path, details=states.append, progress=percentages.append)
    assert 'downloading' not in [state['phase'] for state in states]
    assert percentages == [100]


@pytest.mark.parametrize('during', ['downloading', 'verifying'])
def test_cancellation_during_work_removes_partial_and_never_finishes(tmp_path, monkeypatch, during):
    candidate = m._update_candidate(release()); downloads(monkeypatch, candidate)
    cancel = threading.Event()
    def state(value):
        if value['phase'] == during:
            cancel.set()
    with pytest.raises(InterruptedError):
        m._download_update(candidate, tmp_path, cancelled=cancel, details=state)
    assert not list(tmp_path.iterdir())


def test_controller_never_reports_ready_for_bad_hash(qt, tmp_path, monkeypatch):
    monkeypatch.setattr(m, '_update_json', lambda: release())
    candidate = m._update_candidate(release()); downloads(monkeypatch, candidate, data=b'X'*len(PAYLOAD))
    updater = m.ReleaseUpdater(tmp_path)
    updater.check(); updater._thread.join(3)
    assert updater.state['phase'] == 'error' and updater.candidate is None and updater.installer is None
    assert not list(tmp_path.iterdir()); updater.shutdown()


def test_failed_recheck_does_not_keep_a_previous_install_offer(qt, tmp_path, monkeypatch):
    updater = m.ReleaseUpdater(tmp_path); updater.candidate = m._update_candidate(release()); updater.installer = tmp_path/'old.exe'
    monkeypatch.setattr(m, '_update_json', Mock(side_effect=OSError('offline')))
    updater.check(manual=True); updater._thread.join(3)
    assert updater.candidate is None and updater.installer is None and updater.state['phase'] == 'error'
    updater.shutdown()


def test_progress_animation_is_measured_monotonic_and_phase_aware(qt):
    bar = m.SmoothProgressBar()
    bar.show()
    try:
        bar.set_progress({'phase': 'downloading', 'percent': 40}); pump(qt, 60)
        assert 0 < bar.value() <= 40 and bar.target == 40
        bar.set_progress({'phase': 'downloading', 'percent': 20}); pump(qt, 190)
        assert bar.value() == 40
        bar.set_progress({'phase': 'verifying', 'percent': None})
        assert bar.minimum() == bar.maximum() == 0
        bar.set_progress({'phase': 'ready', 'percent': 100}); pump(qt, 190)
        assert bar.value() == 100 and bar.maximum() == 100
    finally:
        bar.hide(); bar.deleteLater()


def test_update_dialog_closing_is_not_install_consent_and_buttons_are_safe(qt):
    window = m.UpdateDialog(); requested = []; window.install_requested.connect(lambda: requested.append(True))
    try:
        window.set_state({'phase': 'downloading', 'percent': 35, 'status': 'Downloading', 'version': '1.2.4',
                          'received': 35*1048576, 'total': 100*1048576, 'speed': 2*1048576, 'eta': 32.5})
        window.show(); pump(qt, 30)
        assert not window.primary.isVisible() and window.cancel.isVisible()
        assert '35.0 / 100.0 MiB' in window.transfer.text()
        assert '2.0 MiB/s' in window.transfer.text() and '33s left' in window.transfer.text()
        image = window.grab().toImage(); assert image.pixelColor(0, 0).alpha() == 0
        window.set_state({'phase': 'ready', 'percent': 100, 'status': 'Verified', 'version': '1.2.4'})
        assert window.primary.text() == 'Install and restart' and window.secondary.isDefault()
        window.set_install_allowed(False); assert not window.primary.isEnabled()
        window.set_install_allowed(True); window.primary.click(); assert requested == [True]
        window._close(); assert not window.isVisible()  # did not accept/launch anything
    finally:
        window.close(); window.deleteLater()


@pytest.mark.parametrize('consent', [False, True])
def test_rounded_update_confirmation_needs_explicit_install_button(qt, tmp_path, consent):
    window = m.UpdateDialog()
    try:
        QTimer.singleShot(10, window.primary.click if consent else window.secondary.click)
        assert window.ask_install(m._update_candidate(release()), tmp_path) is consent
    finally:
        window.close(); window.deleteLater()


def test_enter_defaults_to_later_not_install(qt, tmp_path):
    window = m.UpdateDialog()
    try:
        QTimer.singleShot(10, lambda: QTest.keyClick(window, Qt.Key.Key_Return))
        assert window.ask_install(m._update_candidate(release()), tmp_path) is False
    finally:
        window.close(); window.deleteLater()


def test_unloading_also_defers_update_confirmation(monkeypatch):
    app = application(); app.engine._unloading = True
    confirmation = dialog(monkeypatch, 1); launch = Mock(); monkeypatch.setattr(m, '_launch_update_helper', launch)
    app.consider_update(); confirmation.assert_not_called(); launch.assert_not_called()


def test_transfer_text_is_not_fabricated_for_verification_or_no_speed():
    assert m._update_transfer_text({'phase': 'verifying', 'percent': 50}) == ''
    text = m._update_transfer_text({'phase': 'downloading', 'total': 10*1048576, 'received': 1048576})
    assert text == '1.0 / 10.0 MiB' and 'left' not in text


def test_production_native_helper_shows_real_install_progress_without_forced_closure():
    source = (ROOT/'packaging/updater.cpp').read_text()
    assert '(quiet ? L"/VERYSILENT " : L"/SILENT ")' in source
    assert '/NOCLOSEAPPLICATIONS /NORESTARTAPPLICATIONS' in source
    assert 'FILE_SHARE_READ' in source and 'BCryptFinishHash' in source
    assert 'TerminateProcess' not in source


@pytest.mark.parametrize('preload', [True, False])
def test_real_application_startup_and_visibility_flow_never_auto_record(qt, tmp_path, monkeypatch, preload):
    original_qt = m.QApplication
    class QtFactoryMeta(type):
        def __getattr__(cls, name):
            return getattr(original_qt, name)
    class QtFactory(metaclass=QtFactoryMeta):
        def __new__(cls, *_): return qt
    class Keys(m.QObject):
        triggered = m.pyqtSignal()
        def set(self, _): return True
        def stop(self): pass
    state = dict(m.DEFAULTS, model_path=str(tiny_gguf(tmp_path/'selected.gguf')),
                 custom_symbols_path=str(tmp_path/'symbols.txt'), check_updates=False, preload_on_popup=preload)
    calls = []
    def load(engine, path):
        calls.append(path); engine.loading = True; return True
    monkeypatch.setattr(m, 'QApplication', QtFactory)
    monkeypatch.setattr(m, 'QSharedMemory', lambda *_: SimpleNamespace(create=lambda _: True))
    monkeypatch.setattr(m, 'Hotkeys', Keys)
    monkeypatch.setattr(m, 'load_settings', lambda: state)
    monkeypatch.setattr(m, 'sync_startup', lambda *a, **kw: True)
    monkeypatch.setattr(m.Engine, 'load_model', load)
    monkeypatch.setattr(m.Engine, '_sync_stream', lambda _: None)
    app = m.App()
    try:
        assert len(calls) == (0 if preload else 1)
        assert app.engine.unload_after_paste and not app.engine.recording and not app.engine.listening
        if preload:
            app.pill.hide_popup()
            app.toggle_popup()
            assert len(calls) == 1 and app.engine.loading
        app.engine.model = SimpleNamespace(close=lambda: None)
        app.engine.loading = False
        app.engine.model_ready.emit()
        assert not app.engine.recording and not app.engine.listening and not app._dictation_after_load
        app.on_dictation_key()
        assert app.engine.recording and app.engine.listening  # only the explicit dictation key starts it
    finally:
        app.idle_timer.stop(); app.update_timer.stop(); app.updater.shutdown()
        app.pill.user_hidden = True; app.pill.timer.stop(); app.pill.guard.stop(); app.pill.fade.stop(); app.pill.hide()
        app.toast.timer.stop(); app.toast.fade.stop(); app.toast.hide()
        app.tray.hide(); app.engine.shutdown()
        qt.removeEventFilter(app._shortcut_input_filter); app._shortcut_input_filter.deleteLater()
        app.pill.deleteLater(); app.toast.deleteLater(); app.tray.deleteLater()
        pump(qt, 10)


def test_typing_failure_is_not_reported_as_success(qt):
    e = Engine(); e._kb = SimpleNamespace(type=Mock(side_effect=RuntimeError('blocked target')))
    e._type = m.Engine._type.__get__(e)
    try:
        assert e._type('plain text ', e._model_generation) is False
        assert e.model is not None and not e.model.closed
    finally:
        e.shutdown()


def test_install_handoff_failure_is_visible_and_does_not_quit(monkeypatch, tmp_path):
    app = application(); dialog(monkeypatch, 1)
    monkeypatch.setattr(m, '_installed_update_directory', lambda: tmp_path)
    monkeypatch.setattr(m, '_launch_update_helper', Mock(side_effect=ValueError('installer changed')))
    app.consider_update()
    app.updater._publish.assert_called_once_with('error', 'Update installation blocked: installer changed')
    app.quit.assert_not_called()


def test_cancel_withdraws_even_a_just_verified_install_offer(qt, tmp_path):
    updater = m.ReleaseUpdater(tmp_path)
    updater.candidate = m._update_candidate(release()); updater.installer = tmp_path/'verified.exe'
    updater.cancel()
    assert updater.candidate is None and updater.installer is None and updater.state['phase'] == 'cancelled'
    updater.shutdown()


def test_cancellation_at_ready_publication_cannot_resurrect_a_candidate(qt, tmp_path, monkeypatch):
    updater = m.ReleaseUpdater(tmp_path)
    monkeypatch.setattr(m, '_update_json', lambda: release())
    monkeypatch.setattr(m, '_download_update', lambda candidate, *a, **kw: tmp_path/candidate['name'])
    publish = updater._publish
    def intercept(phase, *args, **kwargs):
        publish(phase, *args, **kwargs)
        if phase == 'ready': updater.cancel()
    monkeypatch.setattr(updater, '_publish', intercept)
    updater.check(); updater._thread.join(3)
    assert updater.candidate is None and updater.installer is None and updater.state['phase'] == 'cancelled'
    updater.shutdown()


def test_cancellation_during_confirmation_prevents_handoff(monkeypatch, tmp_path):
    app = application()
    def cancel(): app.updater.candidate = None; app.updater.installer = None
    dialog(monkeypatch, 1, during=cancel)
    monkeypatch.setattr(m, '_installed_update_directory', lambda: tmp_path)
    launch = Mock(); monkeypatch.setattr(m, '_launch_update_helper', launch)
    app.consider_update(); launch.assert_not_called(); app.quit.assert_not_called()


@pytest.mark.parametrize('flag', ['loading', '_unloading'])
def test_explicit_record_key_queues_or_cancels_during_model_transition(qt, tmp_path, flag):
    e = Engine(); app = popup_app(e, tiny_gguf(tmp_path/'selected.gguf')); app.reload_model = Mock()
    try:
        setattr(e, flag, True)
        app.on_dictation_key()
        assert app._dictation_after_load and not e.recording
        if flag == '_unloading': app.reload_model.assert_called_once_with(purpose='dictation')
        app.on_dictation_key()
        assert not app._dictation_after_load and not e.recording
    finally:
        setattr(e, flag, False); e.shutdown()


@pytest.mark.parametrize('flag', ['recording', 'busy', 'loading', '_pending_jobs'])
def test_model_selection_cannot_discard_active_or_pending_dictation(qt, tmp_path, monkeypatch, flag):
    e = Engine(); app = popup_app(e, tiny_gguf(tmp_path/'original.gguf'))
    original = app.settings['model_path']; e.load_model = Mock()
    monkeypatch.setattr(m, 'save_settings', Mock())
    try:
        setattr(e, flag, True)
        assert app.choose_model(str(tmp_path/'new.gguf')) is False
        assert app.settings['model_path'] == original
        e.load_model.assert_not_called(); m.save_settings.assert_not_called()
    finally:
        setattr(e, flag, False); e.shutdown()
