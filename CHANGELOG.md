# Changelog

## 1.2.2

- Replace the app, tray/taskbar, executable, installer/shortcut and repository logo with the supplied new artwork.
- Preserve its colors, transparency and aspect ratio; provide multi-resolution Windows icons.
- Logo/version-only runtime update: no changes to dictation, layout, shortcuts, symbols, model handling or updater behavior.
- Distribute through the existing confirmed-install updater, retaining installation location and user data.

## 1.2.0

- Restore independent show/hide and start/commit dictation shortcuts.
- Preserve customized shortcut settings, including migration from the single-shortcut build.
- Transcribe long recordings in the background; hold all insertion until explicit commit.
- Insert each completed recording in one bulk paste, with an optional single Enter.
- Add natural currency amounts, percentages, numeric formulas and spoken email addresses.
- Keep support for 167 explicitly named symbols and true Parakeet Unified GGUF models.
- Preserve the settings clipping and input-focus corrections.
- Embed the supplied Dictate logo in the application, tray, executable and Windows shortcuts.
- Add a Windows installer, portable package, checksums and native runtime smoke tests.
