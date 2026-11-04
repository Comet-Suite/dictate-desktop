# Changelog

## [1.2.3] — Model controls and update progress

### Added
- Measured updater progress with byte totals, speed/ETA, separate persisted-file SHA-256 verification, cancel/retry, and an animated rounded progress window and Settings bar.
- Native installation file-progress display after explicit consent; integrity, parent-wait, recording and in-place installation safeguards remain.
- Independent unload-after-successful-paste and preload-on-popup options, both enabled by default for new/upgraded settings; safe lazy startup and no recording from visibility shortcuts.
- Curated model catalog with friendly names, language/runtime details, exact formats/sizes, revision-pinned downloads, model licenses and explicit release-test variants.

### Improved
- Consistent rounded corners without rearranging the settings columns, popup, theme or logo.
- Full regression gating before Windows release publication and expanded native fixtures across all five preset families.

## 1.2.2

- Replace the app, tray/taskbar, executable, installer/shortcut and repository logo with the supplied new artwork.
- Preserve its colors, transparency and aspect ratio; provide multi-resolution Windows icons.
- Logo/version-only runtime update: no changes to dictation, layout, shortcuts, symbols, model handling or updater behavior.
- Distribute through the existing confirmed-install updater, retaining installation location and user data.

## 1.2.1

- Add safe manual/idle native-model unloading, efficient binary audio IPC and runtime-specific GGUF quantization metadata.
- Add editable UTF-8 custom phrase mappings and spoken enter as a literal newline rather than an Enter keypress.
- Add official stable-release download verification, explicit installation consent and a native in-place upgrade/restart helper.
- Retain the existing UI layout, both shortcuts, Auto Enter choice, settings and models.

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
