# Dictate 1.2.6

## Recording popup: controls only when you need them

- **Click the centered mic to record.** An unloaded model is loaded first, then recording starts from that explicit click. Showing/preloading the popup alone does not record.
- During recording, **only moving bars show by default**. Hover to reveal **✕ Cancel · Pause/Resume · ✓ Paste on top of the waveform**. Move away to hide the buttons. Right-click for **Settings**; the gear is removed.
- **✕** discards without pasting, hides and safely unloads the model after active work finishes. Dictate remains in the tray.
- **✓** inserts the entire session once, honors **Auto Enter**, and hides the popup. Pauses never submit or discard a recording.

## Win-key shortcuts, without changing your defaults

The defaults remain **Ctrl+Alt+D** for Show / hide and **Ctrl+Alt+Space** for dictation. Both shortcut boxes now accept supported **Win/Windows/Meta** combinations, including **Win+H** and **Win+V**.

Assigning a duplicate automatically removes the older Dictate assignment. Cleared bindings stop running; failed registration attempts restore the previous bindings where available. Editing a shortcut does not activate the existing action.

Supported assigned Win chords are intercepted **only while Dictate runs**, suppressing the matched native Windows action. Unassign or quit to restore normal delivery. There are no permanent registry/policy changes. **Win+L, Ctrl+Alt+Delete, secure desktops, elevated applications and competing third-party hooks are not override guarantees.** See the [shortcut guide](https://github.com/Comet-Suite/dictate-desktop/blob/main/docs/SHORTCUTS.md).

## Retained

All five popup sizes, default-on position lock, saved bar color/height and opacity, the single **5/10/15/Never/Immediately after pasting** unload choice, three-column settings, supplied logo, model catalog/downloads, true Parakeet Unified and CrispASR runtimes, focus/selection, one complete paste, number/symbol/custom mapping conversion, startup and updater safeguards remain.

## Upgrade

Choose **Check for updates → Install and restart**, or quit the running copy and run `Dictate-Setup-1.2.6-x64.exe` into the existing installation folder. Settings, distinct shortcuts, model files, custom mappings, popup appearance/position and startup preference are retained. Historical releases are not replaced.

The installer remains **unsigned**. Use official downloads and verify `SHA256SUMS.txt`. Automated verification uses synthetic audio, mouse and tagged keyboard input where stated; no physical microphone recording, physical keyboard or reboot test is claimed.
