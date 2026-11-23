"""Precisely superseded v1.2.5 paths: popup, explicit actions and Win ownership.

Historical baseline fixtures stay intact. Unchanged methods within the two UI
classes are additionally protected against the actual released v1.2.5 source.
"""
V126_AUTHORIZED = {
    'load_settings', '_DictationShortcutFilter',
    'Pill', 'Pill.__init__', 'Pill.hide_popup', 'Pill._fade_done', 'Pill.fade_in',
    'Pill._centers', 'Pill._tick', 'Pill.paintEvent', 'Pill._hit',
    'Pill.mousePressEvent', 'Pill.mouseMoveEvent', 'Pill.leaveEvent',
    'Pill.mouseReleaseEvent', 'Pill.contextMenuEvent',
    'to_pynput', 'parse_win_hotkey', 'Hotkeys', '_check_popup_controls',
    'SettingsDialog', 'SettingsDialog.__init__', 'SettingsDialog._apply_hotkey',
    'SettingsDialog._keys', 'SettingsDialog._apply_hotkey2', 'SettingsDialog._keys2',
    'App.__init__', 'App.toggle_popup', 'App.set_dict_hotkey', 'App.on_dictation_key',
    'App.set_hotkey', 'App.on_ready', 'App.on_error', 'App.on_unloaded',
    'App.reload_model', 'App.open_settings', 'App.quit',
}
