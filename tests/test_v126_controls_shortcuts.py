"""v1.2.6 explicit popup sessions, hover-only overlay and runtime Win ownership.

Audio, keyboard delivery and model inference are synthetic unless a test is
explicitly marked as the native Windows packaged hook verification.
"""
import ast
import hashlib
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeySequence
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QKeySequenceEdit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as m
from test_targeted_fix import Engine, controller, feed, until
from test_focus_symbols import pump, pump_until
from test_v125_features import canonical

ROOT = Path(__file__).resolve().parents[1]
BASELINE = json.loads((ROOT/'tests/fixtures/v126_preserved_behavior.json').read_text())
TREE = ast.parse((ROOT/'dictate.py').read_text())


@pytest.mark.parametrize('name', BASELINE['protected_ast'])
def test_unchanged_paths_match_the_actual_published_v125_source(name):
    parts = name.split('.')
    node = next(n for n in TREE.body if getattr(n, 'name', '') == parts[0])
    if len(parts) > 1:
        node = next(n for n in node.body if getattr(n, 'name', '') == parts[1])
    digest = hashlib.sha256(json.dumps(canonical(node), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert digest == BASELINE['protected_ast'][name], name


@pytest.fixture
def popup(qt):
    e = Engine()
    app = controller(e)
    p = m.Pill(e, app.settings)
    p.timer.stop(); p.guard.stop(); p.move(180, 120); p.show()
    app.pill, app.tray = p, Mock()
    app._quitting = False
    p.record_requested.connect(app.start_popup_recording)
    p.cancel_requested.connect(app.cancel_popup_recording)
    p.pause_requested.connect(app.pause_popup_recording)
    p.commit_requested.connect(app.commit_popup_recording)
    yield qt, e, app, p
    app._quitting = True
    model = e.model
    if model is not None and getattr(model, 'gate', None) is not None:
        model.gate.set()
    p.user_hidden = True; p.timer.stop(); p.guard.stop(); p.fade.stop(); p.glide.stop(); p.hide()
    e.shutdown(); p.deleteLater(); pump(qt, 5)


def click(p, index):
    QTest.mouseClick(p, Qt.MouseButton.LeftButton, pos=(p._centers()[index]*p.k).toPoint())


def hover(p):
    p.enterEvent(QEvent(QEvent.Type.Enter))
    for _ in range(30):
        p._tick()
    assert p.controls_blend == 1


def test_idle_has_one_centered_mic_and_no_gear(popup):
    _, e, app, p = popup
    assert p.view_mode() == 'idle'
    assert p._hit(p._centers()[1]*p.k) == 1
    assert p._hit(p._centers()[0]*p.k) == p._hit(p._centers()[2]*p.k) == -1
    assert p._centers()[1].x() == p.M + p.PW/2
    assert not p.findChildren(m.QPushButton)
    menu = p._create_context_menu()
    assert [action.text() for action in menu.actions()] == ['Settings', 'Quit Dictate']
    settings = Mock(); p.settings_requested.connect(settings)
    menu.actions()[0].trigger(); settings.assert_called_once()
    assert not e.recording and not e.listening
    menu.deleteLater()


@pytest.mark.parametrize('size', m.SIZES)
@pytest.mark.parametrize('dark', [True, False])
def test_waveform_only_until_hover_and_hover_is_an_overlay(popup, size, dark):
    qt, e, app, p = popup
    app.settings['dark_mode'] = dark; p.set_scale(m.SIZES[size])
    click(p, 1)
    assert e.recording and e.listening
    p.leaveEvent(QEvent(QEvent.Type.Leave))
    for _ in range(50):
        p._tick()
    assert p.controls_blend == 0 and p.blend == 1
    assert all(p._hit(center*p.k) == -1 for center in p._centers())
    bars = list(p.bars); p._tick(); assert p.bars != bars
    plain = p.grab().toImage()
    hover(p)
    image = p.grab().toImage()
    assert plain != image
    # Full-width bars remain visible between and outside the circular controls.
    color = app.settings['voice_bar_color']
    colored = [(x, y) for y in range(image.height()) for x in range(image.width())
               if image.pixelColor(x, y).name() == color]
    assert len(colored) > 10
    assert len(p.bars) == 27 and min(x for x, _ in colored) < image.width()/3
    assert max(x for x, _ in colored) > 2*image.width()/3
    p.leaveEvent(QEvent(QEvent.Type.Leave))
    assert not p.inside and p.controls_blend == 0
    assert all(p._hit(center*p.k) == -1 for center in p._centers())


def test_mic_starts_explicit_recording_even_with_saved_auto_mode(popup):
    _, e, app, p = popup
    assert not e.manual and app.settings['stop_mode'] == 'auto'
    click(p, 1)
    assert e.recording and e.listening and e._shortcut_mode
    first = e._capture_id
    assert app.start_popup_recording() is False
    assert e.recording and e._capture_id == first and e.pending == 0
    feed(e, blocks=15); feed(e, blocks=90, value=0)
    assert e.recording and not e.typed


def test_pause_hover_button_never_discards_or_pastes_and_tick_commits_once(popup):
    qt, e, app, p = popup
    click(p, 1); feed(e, blocks=8)
    hover(p); click(p, 1)
    assert e.paused and e.recording and not e.typed
    for _ in range(20): p._tick()
    frozen = list(p.bars)
    for _ in range(20): p._tick()
    assert p.bars == frozen
    click(p, 1); assert not e.paused and e.recording
    feed(e, blocks=9)
    click(p, 2)
    assert p.user_hidden and not e.recording
    assert p.view_mode() == 'finishing'  # no visible idle-mic flash during fade
    assert app.commit_popup_recording() is False
    pump_until(qt, lambda: e.pending == 0 and len(e.typed) == 1)
    assert sum(map(len, e.model.calls)) == 17*m.BLOCK
    assert e.typed == ['part1 ']
    p.fade.setCurrentTime(p.fade.duration()); qt.processEvents()
    assert not p.isVisible() and p.user_hidden


@pytest.mark.parametrize('auto_enter', [False, True])
def test_tick_respects_auto_enter_and_submits_entire_long_session_once(popup, auto_enter):
    qt, e, app, p = popup
    e.auto_enter = auto_enter
    e._kb = Mock()
    e._type = m.Engine._type.__get__(e, m.Engine)
    click(p, 1); feed(e, seconds=24)
    assert not e._kb.type.called
    hover(p); click(p, 2)
    pump_until(qt, lambda: e.pending == 0 and not e.busy)
    e._kb.type.assert_called_once()
    assert all('part'+str(i+1) in e._kb.type.call_args.args[0] for i in range(len(e.model.calls)))
    assert e._kb.press.call_count == e._kb.release.call_count == int(auto_enter)
    assert p.user_hidden and not e.recording


def cold_load(app, e, tmp_path):
    path = tmp_path/'selected.gguf'; path.touch(); app.settings['model_path'] = str(path)
    model, e.model = e.model, None
    def load(_):
        assert not e.listening and not e.recording
        e.loading = True; e._model_generation += 1
        return True
    e.load_model = Mock(side_effect=load)
    app.reload_model = m.App.reload_model.__get__(app, m.App)
    return model


def test_cold_mic_click_remembers_intent_and_starts_exactly_once_when_ready(popup, tmp_path):
    _, e, app, p = popup
    model = cold_load(app, e, tmp_path)
    click(p, 1)
    assert app._dictation_after_load and e.loading and p.preparing
    assert not e.recording and not e.listening
    assert p.view_mode() == 'preparing'
    assert app.start_popup_recording() is True
    assert e.load_model.call_count == 1
    e.model, e.loading = model, False
    app.on_ready()
    token = e._capture_id
    assert e.recording and not p.preparing and not app._dictation_after_load
    app.on_ready()
    assert e.recording and e._capture_id == token and not e.typed


def test_preload_and_show_alone_never_start_recording(popup, tmp_path):
    _, e, app, p = popup
    model = cold_load(app, e, tmp_path)
    p.hide_popup(); app.toggle_popup()
    assert e.loading and e.load_model.call_count == 1
    assert not app._dictation_after_load and not p.preparing
    e.model, e.loading = model, False; app.on_ready()
    assert not e.recording and not e.listening
    click(p, 1); assert e.recording


def test_cancel_during_cold_load_hides_discards_intent_and_unloads_on_ready(popup, tmp_path):
    qt, e, app, p = popup
    model = cold_load(app, e, tmp_path)
    click(p, 1); hover(p); click(p, 0)
    assert p.user_hidden and not app._dictation_after_load and not e.recording
    assert app._popup_cancel_ticket is not None
    e.model, e.loading = model, False
    app.on_ready()
    assert e.model is None and model.closed and e.unload_reason == 'cancel'
    assert not e.typed and not app._popup_cancel_ticket and not app._quitting
    pump(qt, 120)
    assert not e.typed and p.user_hidden


def test_cancel_running_background_inference_never_pastes_late_result(popup):
    qt, e, app, p = popup
    old = e.model; old.gate = threading.Event()
    click(p, 1); feed(e, seconds=13)
    until(old.started.is_set)
    hover(p); click(p, 0)
    assert p.user_hidden and not e.recording and not e.listening
    assert e.model is old and not old.closed  # don't race native inference with close
    assert not e.typed and app._popup_cancel_ticket is not None
    old.gate.set()
    pump_until(qt, lambda: e.model is None and not e.busy)
    assert old.closed and not e.typed and not app._quitting


def test_cancel_small_queued_session_discards_without_inference_or_paste(popup):
    qt, e, app, p = popup
    old = e.model
    click(p, 1); feed(e, blocks=9)
    hover(p); click(p, 0)
    pump_until(qt, lambda: e.model is None)
    assert old.closed and not e.typed and not old.calls
    assert p.user_hidden


def test_stale_cancel_timer_cannot_unload_a_new_recording(popup):
    qt, e, app, p = popup
    old = e.model; old.gate = threading.Event()
    click(p, 1); feed(e, seconds=13); until(old.started.is_set)
    app.cancel_popup_recording(); ticket = app._popup_cancel_ticket
    assert ticket is not None
    app.start_popup_recording(); assert e.recording and not p.user_hidden
    app._finish_popup_cancel(ticket)
    old.gate.set(); pump(qt, 160)
    assert e.model is old and not old.closed and e.recording
    feed(e, blocks=8); app.commit_popup_recording()
    pump_until(qt, lambda: e.pending == 0 and len(e.typed) == 1)
    assert e.model is old and len(e.typed) == 1


def test_show_again_invalidates_a_still_waiting_cancel_unload(popup):
    qt, e, app, p = popup
    e.busy = True
    app.cancel_popup_recording(); ticket = app._popup_cancel_ticket
    app.toggle_popup()
    assert not app._popup_cancel_ticket and not p.user_hidden
    e.busy = False; app._finish_popup_cancel(ticket)
    assert e.model is not None and not e.model.closed and not e.recording


def test_empty_model_path_is_helpful_and_does_not_claim_recording(popup):
    _, e, app, p = popup
    model, e.model = e.model, None
    assert app.start_popup_recording() is False
    assert not e.recording and not app._dictation_after_load
    assert 'Right-click' in app.say.call_args.args[0]
    model.close()


@pytest.mark.parametrize('alias', ['Win', 'win', 'Windows', 'Meta', 'Super', 'Cmd'])
@pytest.mark.parametrize('key', ['H', 'V', 'Space', 'F23'])
def test_windows_modifier_aliases_are_canonical_and_equal(alias, key):
    assert m._canonical_hotkey(f'{alias}+{key}') == 'Meta+'+key
    assert m._same_hotkey(f'{alias}+{key}', 'Meta+'+key)
    assert m.parse_win_hotkey(f'{alias}+{key}')[0] == 8
    assert m.to_pynput(f'{alias}+{key}').startswith('<cmd>+')
    assert m._display_hotkey(f'{alias}+{key}') == 'Win+'+key


@pytest.mark.parametrize('seq,canonical,parts', [
    ('Alt+Win+H', 'Alt+Meta+H', (9, 0x48)),
    ('Shift+Ctrl+Windows+V', 'Ctrl+Shift+Meta+V', (14, 0x56)),
    ('control+ALT+d', 'Ctrl+Alt+D', (3, 0x44)),
    ('Ctrl+Alt+Space', 'Ctrl+Alt+Space', (3, 0x20)),
    ('Meta+PageDown', 'Meta+PgDown', (8, 0x22)),
    ('F24', 'F24', (0, 0x87)),
])
def test_existing_shortcuts_and_all_windows_modifiers_map_to_native_vk(seq, canonical, parts):
    assert m._canonical_hotkey(seq) == canonical
    assert m.parse_win_hotkey(seq) == parts


@pytest.mark.parametrize('bad', ['H', 'Win', 'Ctrl+Alt', 'Ctrl+A+B', 'F25', 'Win+F99', None, [], 'Windows+UnknownKey'])
def test_invalid_or_incomplete_bindings_are_rejected(bad):
    assert m._canonical_hotkey(bad) is None
    assert m._shortcut_problem(bad, windows=True)


@pytest.mark.parametrize('seq', ['Win+L', 'Windows+L', 'Meta+L', 'Ctrl+Alt+Delete', 'Alt+Control+Del', 'Win+Ctrl+Alt+Delete'])
def test_protected_security_chords_are_not_promised_or_injected(seq):
    assert 'security' in m._shortcut_problem(seq, windows=True)


def test_defaults_are_not_replaced_with_a_new_win_alt_h_binding():
    assert m.DEFAULTS['hotkey'] == 'Ctrl+Alt+D'
    assert m.DEFAULTS['dict_hotkey'] == 'Ctrl+Alt+Space'


@pytest.mark.parametrize('win', [0x5B, 0x5C])
@pytest.mark.parametrize('primary', [0x48, 0x56])
def test_win_state_exact_press_repeat_and_keyup_ownership(win, primary):
    state = m._WinShortcutState()
    lookup = lambda mods, key: 'action' if (mods, key) == (8, primary) else None
    assert state.feed(win, True, lookup) == (False, None)
    assert state.feed(primary, True, lookup) == (True, 'action')
    for _ in range(8): assert state.feed(primary, True, lookup) == (True, None)
    assert state.feed(win, False, lookup) == (False, None)
    assert state.feed(primary, False, lookup) == (True, None)
    assert not state.held and not state.suppressed
    assert state.feed(primary, True, lookup) == (False, None)


def test_win_state_does_not_swallow_other_chords_and_handles_both_win_keys():
    state = m._WinShortcutState(); lookup = lambda mods, key: 'h' if (mods, key) == (8, 0x48) else None
    state.feed(0x5B, True, lookup); state.feed(0x5C, True, lookup)
    state.feed(0x5B, False, lookup); assert state.modifiers == 8
    state.feed(0xA0, True, lookup)
    assert state.feed(0x48, True, lookup) == (False, None)
    state.feed(0xA0, False, lookup)
    assert state.feed(0x56, True, lookup) == (False, None)
    assert state.feed(0x48, True, lookup) == (True, 'h')
    # Unassignment while a key is held still consumes its matching key-up.
    assert state.feed(0x48, False, lambda *_: None) == (True, None)


@pytest.fixture
def router(monkeypatch):
    instance = m._WindowsShortcutHook()
    monkeypatch.setattr(m._WindowsShortcutHook, '_instance', instance)
    monkeypatch.setattr(instance, '_ensure_thread', lambda: True)
    monkeypatch.setattr(instance, '_stop_if_idle', Mock())
    monkeypatch.setattr(m, 'os', SimpleNamespace(name='nt'))
    yield instance
    instance._bindings.clear(); instance._capture_target = None


@pytest.mark.parametrize('seq', ['Win+H', 'Win+V', 'Win+Alt+H'])
def test_win_chords_use_exclusive_hook_not_native_registration_or_pynput(router, seq):
    handle = m.Hotkeys(); handle._set_win = Mock(); handle._set_pynput = Mock()
    assert handle.set(seq) and handle._uses_win_hook
    handle._set_win.assert_not_called(); handle._set_pynput.assert_not_called()
    action = router._lookup(*m.parse_win_hotkey(seq))
    assert action[:2] == ('binding', handle)
    assert handle.stop() and router._lookup(*m.parse_win_hotkey(seq)) is None


def test_failed_win_hook_does_not_silently_fall_back_to_a_nonexclusive_binding(router, monkeypatch):
    monkeypatch.setattr(router, '_ensure_thread', lambda: False)
    router.last_error = 'hook failed'
    handle = m.Hotkeys(); handle._set_win = Mock()
    assert handle.set('Win+H') is False and not handle.sequence
    handle._set_win.assert_not_called()
    assert not router._bindings and handle.last_error == 'hook failed'


def test_capture_is_delivered_only_to_current_editor_without_triggering_an_assignment(router, qt):
    handle = m.Hotkeys(); assert handle.set('Win+V')
    first, second = m.ShortcutEdit(), m.ShortcutEdit()
    try:
        assert router.set_capture(first)
        action = router._lookup(8, 0x56)
        assert action[0] == 'capture' and action[1]() is first
        assert router._lookup(8, 0x4C) is None  # protected Win+L is not swallowed even in editor
        assert router.set_capture(second)
        router._deliver_capture(action[1], 8, 0x56)
        assert first.keySequence().isEmpty() and second.keySequence().isEmpty()
        target = router._lookup(8, 0x48)[1]
        router._deliver_capture(target, 8, 0x48)
        assert second.keySequence() == QKeySequence('Meta+H')
        router.end_capture(second)
        assert router._lookup(8, 0x56)[:2] == ('binding', handle)
    finally:
        handle.stop(); first.deleteLater(); second.deleteLater(); pump(qt, 5)


def test_queued_action_is_invalidated_on_unassignment_or_reassignment(qt, router):
    handle = m.Hotkeys(); fired = Mock(); handle.triggered.connect(fired)
    assert handle.set('Win+H')
    serial = handle._serial
    worker = threading.Thread(target=lambda: handle._activated.emit(serial)); worker.start(); worker.join()
    handle.stop(); pump(qt, 5); fired.assert_not_called()
    assert handle.set('Win+V')
    handle._deliver(serial); fired.assert_not_called()
    handle._deliver(handle._serial); fired.assert_called_once()
    handle.stop()


def test_nonwin_shortcut_still_uses_registerhotkey(router):
    handle = m.Hotkeys(); handle._set_win = Mock(return_value=True)
    assert handle.set('Ctrl+Alt+Space')
    handle._set_win.assert_called_once_with('Ctrl+Alt+Space')
    assert not handle._uses_win_hook and not router._bindings
    handle.stop()


def test_empty_assignment_stops_backend_instead_of_only_clearing_saved_text(qt, monkeypatch):
    handle = m.Hotkeys(); listener = Mock(); listener.is_alive.return_value = False
    handle._listener = listener; handle.sequence = 'Ctrl+Alt+D'
    assert handle.set('') is False
    assert handle.sequence == '' and handle._listener is None
    listener.stop.assert_called_once(); listener.join.assert_called_once_with(1)


class FakeBindings:
    """Deterministic backend ownership/failure model, not a fake Win API test."""
    def __init__(self, role, owners, operations):
        self.role, self.owners, self.operations = role, owners, operations
        self.sequence, self.last_error = '', ''
        self.fail = set()

    def stop(self):
        self.operations.append(('stop', self.role, self.sequence))
        if self.sequence and self.owners.get(self.sequence) == self.role:
            self.owners.pop(self.sequence)
        self.sequence = ''
        return True

    def set(self, seq):
        self.stop(); self.operations.append(('set', self.role, seq))
        if seq in self.fail or (seq in self.owners and self.owners[seq] != self.role):
            self.last_error = 'synthetic registration failure'; return False
        self.last_error = ''; self.sequence = seq; self.owners[seq] = self.role
        return True


@pytest.fixture
def bindings(monkeypatch):
    app = m.App.__new__(m.App)
    app.settings, app.dialog, app.engine, app.tray, app.say = dict(m.DEFAULTS), None, SimpleNamespace(), Mock(), Mock()
    owners, operations = {}, []
    app.hotkeys = FakeBindings('hotkey', owners, operations)
    app.dict_hotkeys = FakeBindings('dict_hotkey', owners, operations)
    app._active_shortcuts = {'hotkey': '', 'dict_hotkey': ''}
    assert app.set_hotkey(app.settings['hotkey'])
    assert app.set_dict_hotkey(app.settings['dict_hotkey'])
    monkeypatch.setattr(m, 'save_settings', Mock())
    operations.clear()
    return app, owners, operations


@pytest.mark.parametrize('key,other', [('hotkey', 'dict_hotkey'), ('dict_hotkey', 'hotkey')])
def test_duplicate_clears_older_assignment_then_takes_ownership_transactionally(bindings, key, other):
    app, owners, operations = bindings
    wanted = app.settings[other]
    assert app.assign_shortcut(key, wanted)
    assert app.settings[other] == '' and app.settings[key] == wanted
    assert app._active_shortcuts[other] == '' and owners == {wanted: key}
    assert operations[0][:2] == ('stop', other)
    assert app._dictation_shortcut_registered == (key == 'dict_hotkey')
    m.save_settings.assert_called_once_with(app.settings)


def test_alias_duplicate_is_cleared_not_rejected(bindings):
    app, owners, _ = bindings
    assert app.assign_shortcut('hotkey', 'Windows+H')
    assert app.assign_shortcut('dict_hotkey', 'Win+H')
    assert app.settings['hotkey'] == '' and app.settings['dict_hotkey'] == 'Meta+H'
    assert owners == {'Meta+H': 'dict_hotkey'}


@pytest.mark.parametrize('key,other', [('hotkey', 'dict_hotkey'), ('dict_hotkey', 'hotkey')])
def test_failed_duplicate_assignment_rolls_back_both_active_and_saved_bindings(bindings, key, other):
    app, owners, _ = bindings
    saved, active = dict(app.settings), dict(app._active_shortcuts)
    handles = {'hotkey': app.hotkeys, 'dict_hotkey': app.dict_hotkeys}
    handles[key].fail.add(saved[other])
    assert not app.assign_shortcut(key, saved[other])
    assert app.settings == saved and app._active_shortcuts == active
    assert owners == {value: role for role, value in active.items()}
    assert 'failure' in app.say.call_args.args[0]
    m.save_settings.assert_not_called()


@pytest.mark.parametrize('key', ['hotkey', 'dict_hotkey'])
def test_clear_stops_active_handle_and_persists_unassignment(bindings, key):
    app, owners, _ = bindings
    old = app.settings[key]
    assert app.assign_shortcut(key, '')
    assert app.settings[key] == app._active_shortcuts[key] == '' and old not in owners
    if key == 'dict_hotkey': assert not app._dictation_shortcut_registered


def test_invalid_binding_does_not_unassign_or_rewrite_settings(bindings):
    app, owners, operations = bindings
    saved, active = dict(app.settings), dict(app._active_shortcuts)
    assert not app.assign_shortcut('dict_hotkey', 'Ctrl+Alt')
    assert not operations and app.settings == saved and app._active_shortcuts == active
    m.save_settings.assert_not_called()


@pytest.mark.parametrize('show,dictation', [('Windows+H', 'Meta+H'), ('Alt+Ctrl+D', 'Ctrl+Alt+D')])
def test_legacy_duplicate_preferences_keep_dictation_and_clear_show_without_overwriting_file(tmp_path, monkeypatch, show, dictation):
    file = tmp_path/'settings.json'; saved = dict(m.DEFAULTS, hotkey=show, dict_hotkey=dictation)
    file.write_text(json.dumps(saved)); monkeypatch.setattr(m, 'SETTINGS_FILE', file)
    result = m.load_settings()
    assert result['hotkey'] == '' and result['dict_hotkey'] == dictation
    assert json.loads(file.read_text()) == saved


def test_settings_editor_allows_duplicate_transfer_and_an_empty_binding(qt, monkeypatch):
    monkeypatch.setattr(m, 'list_input_devices', lambda: [])
    monkeypatch.setattr(m, 'save_settings', Mock())
    e = Engine(); settings = dict(m.DEFAULTS); d = m.SettingsDialog(settings, e)
    changes = []
    d.hotkey_changed.connect(lambda value: changes.append(('show', value)))
    d.dict_hotkey_changed.connect(lambda value: changes.append(('dictate', value)))
    try:
        assert isinstance(d.keys, m.ShortcutEdit) and isinstance(d.keys2, m.ShortcutEdit)
        d.keys.setKeySequence(QKeySequence('Meta+H'))
        d.keys2.setKeySequence(QKeySequence('Meta+H'))
        assert settings['hotkey'] == '' and settings['dict_hotkey'] == 'Meta+H'
        assert d.keys.keySequence().isEmpty()
        assert ('show', '') in changes and changes[-1] == ('dictate', 'Meta+H')
        d.keys2.clear(); assert settings['dict_hotkey'] == ''
        assert changes[-1] == ('dictate', '')
        assert d.findChildren(QKeySequenceEdit) == [d.keys, d.keys2]
    finally:
        d.tick.stop(); d.hide(); d.deleteLater(); e.shutdown(); pump(qt, 5)


def test_shortcut_events_in_capture_box_do_not_start_dictation_or_hide_popup(popup, monkeypatch):
    _, e, app, p = popup
    monkeypatch.setattr(m, '_shortcut_editor_active', lambda: True)
    app.on_dictation_key(); app.toggle_popup(from_shortcut=True)
    assert not e.recording and not p.user_hidden
    # A deliberate popup mic click remains explicit even if the settings editor
    # still owns keyboard focus (the passive popup never takes that focus).
    app.start_popup_recording(); assert e.recording


def test_native_keyboard_smoke_is_explicit_about_platform_and_synthetic_input(qt):
    report = m._check_windows_shortcuts(qt)
    if m.os.name == 'nt':
        assert report['status'] == 'ok' and report['win_h_v_downstream_suppression']
        assert report['synthetic_keyboard_input'] and not report['permanent_system_changes']
        assert not report['protected_shortcuts_injected'] and not report['physical_keyboard_test']
        assert len(report['checks']) >= 10
    else:
        assert report['status'] == 'not_applicable' and not report['synthetic_keyboard_input']


@pytest.mark.parametrize('modifiers,primary', [(3, 0x20), (2, 0x44), (0, 0x86)])
def test_focused_capture_can_own_nonwin_chords_before_registerhotkey(modifiers, primary):
    state = m._WinShortcutState()
    for flag, key in ((1, 0xA4), (2, 0xA2)):
        if modifiers & flag:
            state.feed(key, True, lambda *_: None)
    lookup = lambda mods, key: 'capture' if (mods, key) == (modifiers, primary) else None
    assert state.feed(primary, True, lookup) == (False, None)
    assert state.feed(primary, True, lookup, capture=True) == (True, 'capture')
    assert state.feed(primary, False, lookup, capture=True) == (True, None)


def test_mic_intent_survives_unload_before_the_old_model_pointer_is_cleared(popup, tmp_path, monkeypatch):
    _, e, app, p = popup
    path = tmp_path/'selected.gguf'; path.touch(); app.settings['model_path'] = str(path)
    app.reload_model = m.App.reload_model.__get__(app, m.App)
    scheduled = []
    monkeypatch.setattr(m, 'QTimer', SimpleNamespace(singleShot=lambda delay, callback: scheduled.append((delay, callback))))
    e._unloading = True
    app.start_popup_recording()
    assert app._dictation_after_load and scheduled and e.model is not None
    old, e.model = e.model, None; old.close(); e._unloading = False
    e.load_model = Mock(return_value=True)
    scheduled[0][1]()
    e.load_model.assert_called_once_with(str(path))
    assert not e.recording and app._dictation_after_load
