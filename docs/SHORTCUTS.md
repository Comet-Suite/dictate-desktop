# Recording controls and Windows-key shortcuts

## Two shortcuts, unchanged defaults

| Role | Default | Behavior |
|---|---|---|
| Show / hide | **Ctrl+Alt+D** | Toggle the popup. Showing it can preload the selected model, but never records. |
| Dictation | **Ctrl+Alt+Space** | Start a complete session; press again to commit its text once and hide the popup. |

The centered idle **mic** is another explicit Start action. If the model is unloaded, Dictate remembers that request, loads the model and then records. Repeated mic clicks cannot queue multiple starts. Pressing the dictation shortcut again during loading cancels its pending start intent.

During recording, **only the moving waveform is visible by default**. Hover over it to reveal:

- **✕ Cancel** — discard without pasting; hide and safely unload after active model work finishes. Dictate remains available in the tray.
- **Pause / Resume** — keep the same recording and all its words. It does not submit or discard the session. Paused bars freeze and dim.
- **✓ Paste** — commit the complete session once, using the current Auto Enter preference, and hide. Long recordings are transcribed internally in pieces, not pasted in pieces.

Moving the pointer away hides the buttons; the waveform remains behind them, not above them or replaced by them. Right-click the popup for **Settings** or **Quit Dictate**. The tray also retains Settings and Show / hide.

## Assign a Windows-key combination

1. Open **Settings** and click either shortcut box.
2. Press a supported combination such as **Win+H** or **Win+V**. Additional Ctrl/Alt/Shift modifiers are supported.
3. The new binding takes effect immediately. `Win`, `Windows` and Qt’s `Meta` name refer to the same modifier.

The defaults are **not** changed to Win+Alt+H or another new shortcut. Keep the original defaults if you prefer.

Assigning the same combination to the other Dictate action automatically clears the **older** assignment. Modifier order and supported aliases do not evade duplicate detection. Clearing a box unregisters its action; Reset restores that role’s original default. If a new registration fails, Dictate attempts to restore the previous active bindings and reports the failure instead of silently saving the failed choice. A legacy preferences file containing duplicate assignments keeps the dictation binding and clears Show / hide.

While a shortcut box owns focus, supported captured chords are routed to that editor instead of activating an existing Dictate action. This includes editing Win+H/Win+V without intentionally invoking their native panels.

## What happens to Windows voice typing and clipboard history?

On Windows, Dictate uses ordinary native registration for non-Win global chords. Assigned supported **Win** chords use a dedicated `WH_KEYBOARD_LL` message thread so reserved native registrations such as **Win+H** (voice typing) and **Win+V** (clipboard history) do not simply fail or run both actions.

Only exact assigned chords—or supported captures while a shortcut editor is focused—are handled. Dictate consumes the primary key’s press/repeats/release and sends a tagged, unassigned mask key to avoid also opening Start when Win is released. Unmatched input is forwarded. No typed-text history is recorded by this handler; it retains only current modifier and handled-key-up state.

This ownership lasts **only while Dictate runs and the binding exists**. Clear or change the assignment, or quit Dictate, to return normal key delivery. There is no permanent registry change, policy edit, deletion of clipboard history, or system-wide disabling of Windows features. Dictate’s ordinary temporary clipboard paste/restore behavior is unchanged; Windows clipboard history/sync may still retain dictated text according to your Windows settings.

## Important limits

- **Win+L and Ctrl+Alt+Delete are security-protected and are rejected**, not advertised as overrideable. Do not use Dictate to disable workstation locking.
- Secure desktops, lock/sign-in screens, UAC prompts, elevated targets, Windows-version differences and higher-priority third-party keyboard hooks are not override guarantees.
- Another application may already register an ordinary Ctrl/Alt/Shift chord. Choose a different chord if registration fails.
- Win shortcuts are platform-specific. Source execution on another operating system uses its existing pynput fallback and does not promise Windows-native feature suppression there.
- No administrator rights or permanent system changes are required for supported ordinary-desktop interception. Giving Dictate elevated privileges is not a general solution for protected targets.

The release smoke test uses tagged synthetic input and an independent downstream hook to verify suppression, capture, forwarding and teardown on a Windows runner. It does not claim a physical keyboard, microphone or reboot test.

Implementation references: [Microsoft LowLevelKeyboardProc](https://learn.microsoft.com/en-us/windows/win32/winmsg/lowlevelkeyboardproc) and [RegisterHotKey](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey). The callback stays short, runs on a message thread, forwards unhandled input and does not query asynchronous modifier state from inside the callback.
