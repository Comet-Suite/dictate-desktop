# Dictate 1.2.0 for Windows

Offline dictation with smooth complete-message insertion, background transcription and natural symbols.

## Downloads

- **Dictate-Setup-1.2.0-x64.exe** — per-user Windows installer; Python is included.
- **Dictate-1.2.0-Windows-x64.zip** — portable folder; extract everything before running `Dictate.exe`.
- **SHA256SUMS.txt** — verify the downloaded files.
- **Build and smoke-test reports** — exact dependencies and packaged native-runtime checks.

## Included

- Two configurable shortcuts: show/hide and start/commit dictation.
- True Parakeet Unified and compatible CrispASR model support.
- Background processing for long recordings without typing before you stop.
- One full-text paste per recording; Auto Enter, if enabled, sends once.
- Natural amounts such as “four hundred dollars” → `$400` and “400 rupees” → `₹400`.
- The same supplied logo in the app, tray, installer, executable and taskbar shortcuts.

## Before you install

Quit any running Dictate instance from its tray menu. The installer is unsigned, so Windows SmartScreen may warn. Check the SHA-256 values and only run downloads you trust.

A speech model is downloaded separately in Settings. Existing preferences and model files are not deleted by the installer. Windows clipboard history/sync may retain dictated text according to your system settings.

GPL-3.0 source is available in this tagged repository. Dependency source links and license notices are included in the package. Model weights retain their separate licenses.
