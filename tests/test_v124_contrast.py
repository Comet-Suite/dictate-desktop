"""v1.2.4: readable update UI despite broken/inactive native palettes."""
import ast
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtGui import QPalette, QColor
from PyQt6.QtWidgets import QFrame

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as m
from test_focus_symbols import pump

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT/'tests/fixtures/v124_preserved_behavior.json').read_text())


def canonical(value):
    if isinstance(value, ast.AST):
        return {'type': type(value).__name__, 'fields': {k: canonical(v) for k, v in ast.iter_fields(value) if v is not None and v != []}}
    if isinstance(value, list):
        return [canonical(v) for v in value]
    if isinstance(value, bytes):
        return {'bytes': value.hex()}
    return value


@pytest.mark.parametrize('name', FIXTURE['protected_ast'])
def test_contrast_patch_does_not_change_other_application_behavior(name):
    parts = name.split('.')
    node = next(n for n in ast.parse((ROOT/'dictate.py').read_text()).body if getattr(n, 'name', '') == parts[0])
    if len(parts) > 1:
        node = next(n for n in node.body if getattr(n, 'name', '') == parts[1])
    digest = hashlib.sha256(json.dumps(canonical(node), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert digest == FIXTURE['protected_ast'][name], name


def test_native_handoff_and_all_model_defaults_are_unchanged():
    assert hashlib.sha256((ROOT/'packaging/updater.cpp').read_bytes()).hexdigest() == FIXTURE['native_helper_sha256']
    assert m.DEFAULTS['preload_on_popup'] and m.DEFAULTS['unload_after_paste']


@pytest.mark.parametrize('dark', [True, False])
def test_all_updater_text_and_button_states_meet_minimum_contrast(dark):
    c = m._update_dialog_colors(dark)
    assert m._color_contrast(c['text'], c['bg']) >= 7
    assert m._color_contrast(c['sub'], c['bg']) >= 4.5
    assert m._color_contrast(c['sub'], c['input']) >= 4.5
    assert m._color_contrast('#ffffff', c['action']) >= 4.5
    assert m._color_contrast('#ffffff', c['action_hover']) >= 4.5


@pytest.mark.parametrize('ambient', ['#000000', '#ffffff'])
@pytest.mark.parametrize('dark', [True, False])
def test_unreadable_native_palette_cannot_hide_updater_text(qt, ambient, dark):
    saved = QPalette(qt.palette())
    hostile = QPalette(saved)
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.WindowText, QPalette.ColorRole.Base,
                     QPalette.ColorRole.Text, QPalette.ColorRole.Button, QPalette.ColorRole.ButtonText):
            hostile.setColor(group, role, QColor(ambient))
    qt.setPalette(hostile)
    window = m.UpdateDialog(dark)
    try:
        window.set_state({'phase': 'ready', 'percent': 100, 'version': '1.2.5', 'status': 'Downloaded and verified; your settings and models are retained.'})
        window.show(); pump(qt, 30)
        c = window._theme_colors
        frame = window.findChild(QFrame, 'win')
        assert frame.palette().color(QPalette.ColorRole.Window).name() == c['bg']
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
            text = window.palette().color(group, QPalette.ColorRole.WindowText)
            assert text.alpha() == 255 and m._color_contrast(text, c['bg']) >= 4.5
        assert window.secondary.isDefault() and not window.primary.autoDefault()
        image = window.grab().toImage()
        assert image.pixelColor(0, 0).alpha() == 0  # corner geometry was not redesigned
        # Switching the ambient application palette while visible cannot override explicit widget roles.
        changed = QPalette(hostile)
        changed.setColor(QPalette.ColorRole.WindowText, QColor(c['bg']))
        qt.setPalette(changed); pump(qt, 10)
        assert m._color_contrast(window.palette().color(QPalette.ColorRole.WindowText), c['bg']) >= 7
        window.setEnabled(False); pump(qt, 10)
        assert m._color_contrast(window.primary.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText), c['input']) >= 4.5
    finally:
        window.hide(); window.deleteLater(); qt.setPalette(saved); pump(qt, 10)


def test_live_theme_switch_updates_existing_update_window_without_replacing_it(qt):
    app = m.App.__new__(m.App)
    app.settings = {'dark_mode': True}
    app.pill = SimpleNamespace(update=Mock())
    app.update_dialog = m.UpdateDialog(True)
    window = app.update_dialog
    try:
        app.settings['dark_mode'] = False
        app._refresh_dialog_themes()
        assert app.update_dialog is window
        assert window._theme_colors['bg'] == m.palette(False)['bg']
        app.pill.update.assert_called_once()
        app.settings['dark_mode'] = True; app._refresh_dialog_themes()
        assert window._theme_colors['bg'] == m.palette(True)['bg']
    finally:
        window.deleteLater()


def test_packaging_render_check_covers_both_themes_and_hostile_palettes(qt):
    original = QPalette(qt.palette())
    report = m._check_update_dialog_readability(qt)
    assert report['status'] == 'ok' and len(report['rendered_cases']) == 4
    assert {case['theme'] for case in report['rendered_cases']} == {'dark', 'light'}
    assert {case['ambient_palette'] for case in report['rendered_cases']} == {'#000000', '#ffffff'}
    assert all(case['visible_text_pixels'] > 10 and min(case['contrast_ratios'].values()) >= 4.5 for case in report['rendered_cases'])
    assert not report['installation_performed'] and not report['microphone_opened']
    assert qt.palette() == original
