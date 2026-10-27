# Dictate 1.2.1 for Windows

Immediate model unloading, lower-retention audio handling, additional GGUF quantizations, editable custom symbols, spoken newlines and consent-based release updates.

## Downloads

- **Dictate-Setup-1.2.1-x64.exe** — per-user Windows x64 installer, including Python and both native speech runtimes.
- **Dictate-1.2.1-Windows-x64.zip** — portable folder; extract the entire archive before launching.
- **SHA256SUMS.txt** — verify installer/portable downloads.
- **Smoke-test reports and resolved dependencies** — packaged, installed, native-runtime, memory-release, startup and in-place upgrade verification.

## What's new

- **Unload now:** release model RAM immediately from Settings or the tray when dictation/loading is finished. Existing Never/5/10/15-minute idle choices remain. Windows tears down the isolated native worker, including model weights and allocator caches; the next recording can reload the selected model.
- **Memory efficiency:** float32 audio uses binary local IPC instead of pickled NumPy payloads. Native workers read the received byte buffer directly. Completed audio chunks/transcript buffers are released rather than retaining the last job while idle; shutdown also stops model workers.
- **Better GGUF selection:** additional quantization choices, deterministic low-memory preference and clear errors instead of silently downloading an unavailable quantization's replacement. Architecture, detected quantization and file size are shown. CrispASR and Unified quantization metadata codes are interpreted separately. Native runtime/architecture compatibility is still required.
- **Custom symbols from `.txt`:** select/edit/reload UTF-8 `phrase = replacement` mappings without rebuilding. Matching is case-insensitive and longest-first; replacements are literal, with escaped newlines and safe quoted text. Invalid edits retain the session's last good mappings.
- **Spoken “enter”:** creates a newline inside the complete text, with following speech on the next line. It does **not** press Enter or send a message. Say “literal enter” for the word. Whole-session Stop-to-paste and the existing optional Auto Enter behavior remain separate.
- **Release updater:** automatically checks/downloads strictly newer stable GitHub releases, verifies GitHub's SHA-256 digest against the checksum manifest and downloaded bytes, then **asks before closing/installing**. Active dictation, pending work and model loading defer installation. A native helper independently rechecks the installer, upgrades the existing installation folder, preserves preferences/model files and restarts. WindowsApps/Store paths are not overwritten.

The existing layout/style, both shortcuts, true Parakeet Unified and CrispASR engines, 167 explicit symbols, natural currencies, exact numbers/leading zeros, bulk insertion, focus safeguards and Windows startup repair are retained.

## Upgrade from 1.2.0

**1.2.0 has no updater: install 1.2.1 manually once.** Finish your current dictation, quit Dictate from the tray, run this installer and keep the existing folder. Saved shortcuts, startup preference, model selection, custom data and downloaded models are retained. Automatic update checks become available in 1.2.1 and can be turned off.

## Verification and trust

The Windows pipeline exercises packaged/installed inference with Unified Q4_K_M/Q5_K_M and TDT Q4_K/Q8_0, worker exit/RAM release, both shortcut controls, startup registration, and a real hash-pinned 1.2.0-to-1.2.1 in-place installer handoff with preference/file preservation and restart. Unit tests separately cover text commands, mapping reload/errors, quantization selection, binary IPC, release validation, failed hashes and installation consent/recording guards. No physical reboot or microphone test is claimed.

The installer is **unsigned**; SmartScreen may warn. HTTPS and matching hashes provide integrity checks, not an Authenticode signature or an independent signing authority. Only install downloads you trust. Dictation audio remains local and is not uploaded by release checks. Model weights are downloaded separately and retain their own licenses.

GPL-3.0 source, including the native helper, is available in this tagged repository. Bundled dependency notices/source links are included in the Windows package. See [custom-symbol documentation](docs/CUSTOM_SYMBOLS.md) and [update policy/security details](docs/UPDATES.md).
