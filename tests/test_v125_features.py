"""v1.2.5: narrowly scoped popup preferences and one safe model-memory policy."""
import ast
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QColor, QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QFrame, QPushButton, QScrollArea

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as m
from test_targeted_fix import Engine, feed, until
from test_focus_symbols import pump

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT/'tests/fixtures/v125_preserved_behavior.json').read_text())
SOURCE_TREE = ast.parse((ROOT/'dictate.py').read_text())


def canonical(value):
    if isinstance(value, ast.AST):
        return {'type': type(value).__name__, 'fields': {
            key: canonical(item) for key, item in ast.iter_fields(value) if item is not None and item != []}}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    if isinstance(value, bytes):
        return {'bytes': value.hex()}
    return value


def digest(value):
    return hashlib.sha256(json.dumps(canonical(value), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@pytest.mark.parametrize('name', FIXTURE['protected_ast'])
def test_unrelated_paths_and_unchanged_ui_methods_match_actual_v124(name):
    parts = name.split('.')
    node = next(n for n in SOURCE_TREE.body if getattr(n, 'name', '') == parts[0])
    if len(parts) == 2:
        node = next(n for n in node.body if getattr(n, 'name', '') == parts[1])
    assert digest(node) == FIXTURE['protected_ast'][name], name


@pytest.mark.parametrize('name', FIXTURE['protected_constants'])
def test_catalog_symbols_native_identity_logo_and_other_constants_match_v124(name):
    target, _, occurrence = name.partition('#')
    seen = 0
    for node in SOURCE_TREE.body:
        if isinstance(node, ast.Assign):
            names = [n.id for target in node.targets for n in ast.walk(target) if isinstance(n, ast.Name)]
            if ','.join(names) == target:
                seen += 1
                if occurrence and seen != int(occurrence):
                    continue
                assert digest(node) == FIXTURE['protected_constants'][name], name
                return
    pytest.fail('Protected constant was removed: ' + name)


def test_native_updater_and_supplied_artwork_are_exactly_unchanged():
    assert hashlib.sha256((ROOT/'packaging/updater.cpp').read_bytes()).hexdigest() == FIXTURE['native_helper_sha256']
    assert hashlib.sha256((ROOT/'assets/logo.png').read_bytes()).hexdigest() == 'e040f451dc524e1598b5857691526b6e9022b23572c3ee7cbb86905b2035c7f3'
    assert hashlib.sha256((ROOT/'assets/app-icon.png').read_bytes()).hexdigest() == '0181674d3da6f1f27d7cc7dafec8919916f39d9237af7de6c151ac27e5a7350e'


@pytest.mark.parametrize('legacy,expected', [
    ({}, -1), ({'unload_min': 0}, 0), ({'unload_min': 5}, 5), ({'unload_min': 10}, 10),
    ({'unload_min': 15}, 15), ({'unload_min': -1}, -1),
    ({'unload_after_paste': True, 'unload_min': 15}, -1),
    ({'unload_after_paste': True, 'unload_min': 0}, -1),
    ({'unload_after_paste': False, 'unload_min': 10}, 10),
    ({'unload_after_paste': False, 'unload_min': 0}, 0),
    ({'unload_after_paste': False}, 0),
    ({'unload_min': None}, -1), ({'unload_min': True}, -1),
    ({'unload_min': '5'}, -1), ({'unload_min': 99}, -1),
])
def test_saved_policy_migration_preserves_unrelated_settings(tmp_path, monkeypatch, legacy, expected):
    path = tmp_path/'settings.json'
    monkeypatch.setattr(m, 'SETTINGS_FILE', path)
    sentinel = {'hotkey': 'Ctrl+Shift+D', 'dict_hotkey': 'Ctrl+Shift+Space', 'auto_enter': False,
                'model_path': 'D:/My models/selected.gguf', 'download_dir': 'D:/My models',
                'custom_symbols_path': 'D:/my symbols café.txt', 'start_on_startup': True,
                'position': 'custom', 'pos': [183, 257], 'unknown_future_preference': ['005', {'keep': True}]}
    saved = dict(sentinel, **legacy, preload_on_popup=False)
    path.write_text(json.dumps(saved), encoding='utf-8')
    result = m.load_settings()
    assert result['unload_min'] == expected
    assert all(result[key] == value for key, value in sentinel.items())
    assert result['popup_locked'] is True
    assert not {'unload_after_paste', 'preload_on_popup'} & result.keys()
    # Loading alone does not overwrite the user's file; the next ordinary save
    # persists one canonical policy plus all unrelated settings and unknown keys.
    assert json.loads(path.read_text()) == saved
    m.save_settings(result)
    assert m.load_settings() == result
    assert not {'unload_after_paste', 'preload_on_popup'} & json.loads(path.read_text()).keys()


@pytest.mark.parametrize('key,bad,expected', [
    ('popup_opacity', None, 100), ('popup_opacity', True, 100), ('popup_opacity', '75', 100),
    ('popup_opacity', -1, 50), ('popup_opacity', 400, 100), ('popup_opacity', 73.6, 74),
    ('popup_opacity', float('nan'), 100), ('popup_opacity', float('inf'), 100),
    ('popup_opacity', 10**400, 100),
    ('popup_locked', 'false', True), ('popup_locked', None, True),
    ('voice_bar_color', 'red', '#c93ff2'), ('voice_bar_color', '#00000000', '#c93ff2'),
    ('voice_bar_color', '#ABCDEF', '#abcdef'), ('voice_bar_color', [], '#c93ff2'),
    ('voice_bar_height', 'giant', 'normal'), ('voice_bar_height', [], 'normal'),
    ('size', [], 'medium'), ('size', 'mini', 'medium'),
])
def test_new_preferences_tolerate_corrupt_or_unsupported_values(key, bad, expected):
    result = m._normalise_ui_settings(dict(m.DEFAULTS, **{key: bad}))
    assert result[key] == expected
    assert .5 <= m._popup_opacity(result) <= 1
    assert m._voice_bar_color(result).isValid()


@pytest.fixture
def ui(qt, monkeypatch):
    monkeypatch.setattr(m, 'list_input_devices', lambda: [])
    monkeypatch.setattr(m, 'save_settings', Mock())
    e = Engine()
    state = dict(m.DEFAULTS)
    d = m.SettingsDialog(state, e)
    p = m.Pill(e, state)
    p.timer.stop(); p.guard.stop(); p.glide.setDuration(0)
    d.popup_changed.connect(p.apply_appearance)
    d.size_changed.connect(lambda key: p.set_scale(m.SIZES[key]))
    yield qt, e, state, d, p
    d.tick.stop(); d.hide()
    p.user_hidden = True; p.timer.stop(); p.guard.stop(); p.fade.stop(); p.glide.stop(); p.hide()
    e.shutdown(); p.deleteLater(); d.deleteLater(); pump(qt, 5)


def test_only_one_ordered_unload_control_and_original_three_column_cards(ui):
    qt, e, state, d, p = ui
    assert [(d.unload.itemText(i), d.unload.itemData(i)) for i in range(d.unload.count())] == [
        ('5 minutes idle', 5), ('10 minutes idle', 10), ('15 minutes idle', 15),
        ('Never', 0), ('Immediately after pasting', -1)]
    assert d.unload.currentData() == -1 and e.unload_after_paste
    assert not any(hasattr(d, attr) for attr in ('btn_unload', 'unload_after_paste', 'preload_on_popup'))
    assert not any('unload' in button.text().lower() for button in d.findChildren(QPushButton))
    assert len([card for card in d.findChildren(QFrame) if card.objectName() == 'card']) == 10
    assert d.repo.count() == 5 and d.combo.count() > 3 and d.btn_dl.text() == 'Download'
    d.show(); pump(qt, 15)
    assert d.width() > d.height() and d.findChild(QScrollArea).horizontalScrollBar().maximum() == 0


@pytest.mark.parametrize('choice', [5, 10, 15, 0, -1])
@pytest.mark.parametrize('success', [True, False])
def test_every_policy_waits_until_successful_complete_insertion(ui, choice, success):
    _, e, state, d, _ = ui
    d.unload.setCurrentIndex(d.unload.findData(choice))
    old = e.model
    pasted = []
    def insert(text, generation=None):
        assert e.model is old and not old.closed and e.busy and e.pending == 1
        pasted.append(text)
        return success
    e._type = insert
    e.toggle_record(); feed(e, blocks=16); e.toggle_record()
    until(lambda: bool(pasted) and e.pending == 0 and not e.busy)
    if choice == -1 and success:
        until(lambda: e.model is None and not e._unloading)
        assert old.closed and e.unload_reason == 'paste'
    else:
        assert e.model is old and not old.closed
    assert len(pasted) == 1 and not e.recording and state['auto_enter'] is False


@pytest.mark.parametrize('choice', [5, 10, 15, 0, -1])
def test_idle_clock_matches_only_the_selected_policy(ui, monkeypatch, choice):
    _, e, state, d, _ = ui
    d.unload.setCurrentIndex(d.unload.findData(choice))
    app = m.App.__new__(m.App); app.engine = e; app.settings = state; app._dictation_after_load = False
    e.last_used = 10_000
    old = e.model
    if choice > 0:
        monkeypatch.setattr(m.time, 'time', lambda: 10_000 + choice*60 - .1)
        app.check_idle(); assert e.model is old
        monkeypatch.setattr(m.time, 'time', lambda: 10_000 + choice*60 + .1)
        app.check_idle(); assert e.model is None and old.closed
    else:
        monkeypatch.setattr(m.time, 'time', lambda: 10_000 + 100*86400)
        app.check_idle(); assert e.model is old and not old.closed


@pytest.mark.parametrize('choice', [5, 10, 15])
@pytest.mark.parametrize('flag', ['recording', 'speaking', 'busy', 'loading', '_pending_jobs', '_unloading'])
def test_idle_policy_never_interrupts_active_loading_or_pending_work(ui, choice, flag):
    _, e, state, d, _ = ui
    d.unload.setCurrentIndex(d.unload.findData(choice))
    app = m.App.__new__(m.App); app.engine = e; app.settings = state
    e.last_used = 0
    old = e.model
    try:
        setattr(e, flag, True)
        app.check_idle()
        assert e.model is old and not old.closed
    finally:
        setattr(e, flag, False)


@pytest.mark.parametrize('size', list(m.SIZES))
def test_new_sizes_keep_original_sizes_and_four_nonoverlapping_usable_targets(ui, size):
    _, _, state, d, p = ui
    d.size.setCurrentIndex(d.size.findData(size))
    k = m.SIZES[size]
    assert (p.width(), p.height()) == (int(252*k), int(80*k))
    assert {'small': .8, 'medium': 1.0, 'large': 1.3}.items() <= m.SIZES.items()
    for i, center in enumerate(p._centers()):
        center *= k
        assert p._hit(center) == i
        assert p._hit(center + QPointF(0, 10.99)) == i
        assert p._hit(center - QPointF(0, 10.99)) == i
        assert 11 <= center.x() <= p.width() - 11
        assert 11 <= center.y() <= p.height() - 11
    centers = p._centers()
    for i in range(3):
        radii = [max(base*k, 11) for base in (20, 24, 20, 15)]
        assert (centers[i+1].x() - centers[i].x())*k > radii[i] + radii[i+1]
    assert p._hit(QPointF(0, 0)) == -1
    assert p.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert p.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)


def mouse(p, kind, local, global_pos, pressed=True):
    button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
    buttons = Qt.MouseButton.LeftButton if pressed else Qt.MouseButton.NoButton
    m.QApplication.sendEvent(p, QMouseEvent(kind, QPointF(local), QPointF(global_pos), button, buttons,
                                          Qt.KeyboardModifier.NoModifier))


def gesture(p, delta, index=1):
    local = p._centers()[index]*p.k if index >= 0 else QPointF(24*p.k, 18*p.k)
    global_start = QPointF(p.pos()) + local
    mouse(p, QEvent.Type.MouseButtonPress, local, global_start)
    mouse(p, QEvent.Type.MouseMove, local+delta, global_start+delta)
    mouse(p, QEvent.Type.MouseButtonRelease, local+delta, global_start+delta, False)


@pytest.mark.parametrize('size', list(m.SIZES))
@pytest.mark.parametrize('index', [0, 1, 2, 3, -1])
def test_default_lock_cancels_drag_release_without_moving_clicking_or_saving(ui, size, index):
    _, e, state, d, p = ui
    p.set_scale(m.SIZES[size]); p.move(180, 120)
    requested, dragged = [], []
    p.settings_requested.connect(lambda: requested.append(True)); p.dragged.connect(lambda: dragged.append(True))
    e.set_listening = Mock()
    e.toggle_pause = Mock()
    origin = p.pos()
    gesture(p, QPointF(28, 14), index)
    assert p.pos() == origin and not requested and not dragged and not p.user_hidden
    assert state['popup_locked'] and state['position'] == 'bottom_center' and state['pos'] is None
    e.set_listening.assert_not_called(); e.toggle_pause.assert_not_called(); m.save_settings.assert_not_called()


@pytest.mark.parametrize('size', list(m.SIZES))
@pytest.mark.parametrize('index', [0, 1, 2, 3])
def test_locked_popup_buttons_remain_clickable_at_every_size(ui, size, index):
    _, e, state, d, p = ui
    p.set_scale(m.SIZES[size]); p.show()
    requested = []; p.settings_requested.connect(lambda: requested.append(True))
    e.listening = True; e.toggle_pause = Mock(); e.set_listening = Mock()
    center = p._centers()[index]*p.k
    QTest.mouseClick(p, Qt.MouseButton.LeftButton, pos=center.toPoint())
    if index == 0:
        e.toggle_pause.assert_called_once()
    elif index == 1:
        e.set_listening.assert_called_once_with(False)
    elif index == 2:
        assert requested == [True]
    else:
        assert p.user_hidden
    assert state['popup_locked']


def test_unlock_drag_clamp_saved_custom_position_and_relock_are_live(ui, monkeypatch, tmp_path):
    _, e, state, d, p = ui
    path = tmp_path/'settings.json'; monkeypatch.setattr(m, 'SETTINGS_FILE', path)
    monkeypatch.setattr(m, 'save_settings', lambda s: path.write_text(json.dumps(s)))
    app = m.App.__new__(m.App); app.pill = p; app.settings = state; app.dialog = d
    p.dragged.connect(app.on_dragged)
    p.move(180, 120)
    d.position_lock.setChecked(False)
    gesture(p, QPointF(63, 24), -1)
    assert p.pos() == QPoint(243, 144)
    assert state['position'] == 'custom' and state['pos'] == [243, 144]
    assert m.load_settings()['pos'] == [243, 144] and m.load_settings()['popup_locked'] is False
    d.position_lock.setChecked(True)
    gesture(p, QPointF(50, 20), -1)
    assert p.pos() == QPoint(243, 144) and m.load_settings()['popup_locked']
    # The picker is deliberate placement and still works with the drag lock on.
    d.position_chosen.connect(app.go_to_preset)
    d._pos('top_right')
    assert p.pos() == app.preset_point('top_right') and state['popup_locked']


def test_turning_lock_on_mid_drag_cancels_the_gesture_instead_of_clicking(ui):
    _, _, state, d, p = ui
    p.move(180, 120); d.position_lock.setChecked(False)
    local = p._centers()[1]*p.k; start = QPointF(p.pos()) + local; delta = QPointF(32, 12)
    mouse(p, QEvent.Type.MouseButtonPress, local, start)
    mouse(p, QEvent.Type.MouseMove, local+delta, start+delta)
    assert p.pos() != QPoint(180, 120)
    d.position_lock.setChecked(True)
    mouse(p, QEvent.Type.MouseButtonRelease, local+delta, start+delta, False)
    assert p.pos() == QPoint(180, 120) and state['position'] == 'bottom_center'


@pytest.mark.parametrize('opacity', [50, 65, 85, 100])
def test_opacity_is_live_and_survives_fade_hide_show_and_size_change(ui, opacity):
    qt, _, state, d, p = ui
    p.fade_in(); p.fade.setCurrentTime(120)
    d.opacity.setValue(opacity)
    p.fade.setCurrentTime(p.fade.duration()); qt.processEvents()
    assert abs(p.windowOpacity() - opacity/100) < .01
    assert state['popup_opacity'] == opacity and d.opacity_value.text() == f'{opacity}%'
    d.size.setCurrentIndex(d.size.findData('tiny'))
    assert abs(p.windowOpacity() - opacity/100) < .01
    p.hide_popup(); p.fade.setCurrentTime(120)
    p.apply_appearance()  # a preference change must not reverse the hide animation
    assert p.fade.endValue() == 0
    p.fade.setCurrentTime(p.fade.duration()); qt.processEvents(); assert not p.isVisible()
    p.show_popup(); p.fade.setCurrentTime(p.fade.duration()); qt.processEvents()
    assert p.isVisible() and abs(p.windowOpacity() - opacity/100) < .01


@pytest.mark.parametrize('label,color', m.BAR_COLORS)
def test_color_presets_apply_live_without_changing_other_preferences(ui, label, color):
    _, _, state, d, p = ui
    before = dict(state)
    d.bar_color.setCurrentIndex(d.bar_color.findData(color))
    assert state['voice_bar_color'] == color and m._voice_bar_color(state).name() == color
    assert d.custom_color.text() == color.upper()
    assert all(state[key] == value for key, value in before.items() if key != 'voice_bar_color')


def test_custom_color_cancel_and_all_popup_preferences_persist(ui, tmp_path, monkeypatch):
    _, _, state, d, p = ui
    path = tmp_path/'settings.json'; monkeypatch.setattr(m, 'SETTINGS_FILE', path)
    monkeypatch.setattr(m, 'save_settings', lambda s: path.write_text(json.dumps(s)))
    monkeypatch.setattr(m.QColorDialog, 'getColor', lambda *a, **kw: QColor('#12abef'))
    d.custom_color.click()
    assert state['voice_bar_color'] == '#12abef' and d.bar_color.currentData() == 'custom'
    monkeypatch.setattr(m.QColorDialog, 'getColor', lambda *a, **kw: QColor())
    d.custom_color.click()
    assert state['voice_bar_color'] == '#12abef'
    d._set_bar_color('not a hex color'); assert state['voice_bar_color'] == '#12abef'
    d.position_lock.setChecked(False); d.opacity.setValue(68)
    d.bar_height.setCurrentIndex(d.bar_height.findData('subtle'))
    d.size.setCurrentIndex(d.size.findData('extra_small'))
    d.unload.setCurrentIndex(d.unload.findData(15))
    saved = m.load_settings()
    for key in ('popup_locked', 'popup_opacity', 'voice_bar_color', 'voice_bar_height', 'size', 'unload_min'):
        assert saved[key] == state[key]
    assert not saved['auto_enter'] and saved['hotkey'] != saved['dict_hotkey']


@pytest.mark.parametrize('height', ['subtle', 'normal', 'taller'])
@pytest.mark.parametrize('size', list(m.SIZES))
@pytest.mark.parametrize('dark', [True, False])
def test_custom_bars_render_without_clipping_at_all_heights_sizes_and_themes(ui, height, size, dark):
    _, e, state, d, p = ui
    state['dark_mode'] = dark
    d.size.setCurrentIndex(d.size.findData(size))
    d.bar_height.setCurrentIndex(d.bar_height.findData(height))
    d._set_bar_color('#29b6f6')
    e.listening = e.speaking = True; e.level = .06
    for _ in range(70): p._tick()
    assert 0 < min(p.bars) <= max(p.bars) < p.PH - 10
    assert max(p.bars) <= 31*m.BAR_HEIGHTS[height] + .01
    image = p.grab().toImage()
    pixels = [(x, y) for y in range(image.height()) for x in range(image.width())
              if image.pixelColor(x, y).name() == '#29b6f6']
    assert len(pixels) >= 5
    assert min(y for _, y in pixels) > (p.M*p.k*image.devicePixelRatio())
    assert max(y for _, y in pixels) < ((p.M+p.PH)*p.k*image.devicePixelRatio())
    assert max(y for _, y in pixels) - min(y for _, y in pixels) < 60*p.k*image.devicePixelRatio()


def test_popup_packaging_check_renders_all_ten_cases_without_audio_or_installation(qt):
    report = m._check_popup_controls(qt)
    assert report['status'] == 'ok' and len(report['rendered_cases']) == 10
    assert report['default_position_locked'] and not report['microphone_opened']
    assert report['minimum_hit_target_px'] == 22 and report['synthetic_mouse_events']
    assert {case['size'] for case in report['rendered_cases']} == set(m.SIZES)
    assert all(case['locked_drag_blocked'] and case['unlocked_drag_works']
               and case['locked_settings_button_works'] and abs(case['opacity_after_fades'] - .65) < .01
               for case in report['rendered_cases'])


@pytest.mark.parametrize('key', FIXTURE['preserved_default_fields'])
def test_unrelated_saved_setting_defaults_match_v124(key):
    node = next(n.value for n in SOURCE_TREE.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'DEFAULTS' for t in n.targets))
    value = next(v for k, v in zip(node.keys, node.values) if k.value == key)
    assert digest(value) == FIXTURE['preserved_default_fields'][key]
    assert m.APP_NAME == 'Dictate'


def test_application_wires_live_popup_preferences_into_the_real_settings_window(ui):
    qt, e, state, _, p = ui
    app = m.App.__new__(m.App)
    app.engine = e; app.settings = state; app.pill = p; app.dialog = None
    app.update_dialog = None
    app.updater = SimpleNamespace(state={'phase': 'idle', 'status': 'Idle'}, cancel=Mock())
    try:
        app.open_settings()
        d = app.dialog
        d.opacity.setValue(64)
        assert abs(p.windowOpacity() - .64) < .01 and state['popup_opacity'] == 64
        d.position_lock.setChecked(False); assert not state['popup_locked']
        d._set_bar_color('#ee7744'); assert m._voice_bar_color(p.settings).name() == '#ee7744'
        d.bar_height.setCurrentIndex(d.bar_height.findData('taller'))
        assert p.settings['voice_bar_height'] == 'taller'
        d.size.setCurrentIndex(d.size.findData('tiny')); assert p.k == .55
        d.unload.setCurrentIndex(d.unload.findData(0)); assert not e.unload_after_paste
        assert not e.recording and not e.listening
    finally:
        if app.dialog is not None:
            app.dialog.tick.stop(); app.dialog.hide(); app.dialog.deleteLater(); pump(qt, 5)
