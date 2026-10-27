# Dictate 1.2.2 — New logo

**A branding-only update using the new supplied logo. No dictation or updater behavior changes.**

## Updated artwork

- New logo in the application icon, system tray and taskbar.
- Updated Windows executable, installer, uninstall and Start-menu/desktop shortcut artwork.
- Updated repository/README branding and embedded single-file artwork.
- Preserved the supplied colors, transparency and aspect ratio, with Windows icon sizes from 16 to 256 pixels.

The layout/style, both shortcuts, whole-session Stop-to-paste, spoken Enter/newlines, custom symbols, exact numbers, speech models, memory unloading, startup settings and confirmed-install updater are unchanged from 1.2.1. An automated source check verifies that the runtime differs only in its version and embedded logo.

## Update from 1.2.1

In Dictate, choose **Check for updates** from the tray or **Settings → Release updates**. Version 1.2.1 will recognize 1.2.2 as newer, download and verify it, then ask you to **Install and restart** when dictation is idle. Automatic periodic checks work as before.

The update retains the existing installation folder, preferences, both shortcuts, selected model, downloaded model files and custom mappings. The new logo is used when the updated app restarts. If Windows retains an old pinned icon, unpin and re-pin the updated app; Windows sometimes caches shortcut icons.

Users still on 1.2.0 must install the new installer manually once, because 1.2.0 has no updater.

## Downloads

- **Dictate-Setup-1.2.2-x64.exe** — per-user Windows x64 installer, including Python and both speech runtimes.
- **Dictate-1.2.2-Windows-x64.zip** — portable folder; extract everything before launch.
- **SHA256SUMS.txt** — verify downloads.
- **Smoke-test reports/resolved dependencies** — packaged/installed checks and real 1.2.1-to-1.2.2 in-place handoff/preservation/restart verification.

The installer remains **unsigned**; SmartScreen may warn. SHA-256 over the official HTTPS release provides integrity checking, not an Authenticode signature. Only run downloads you trust. GPL-3.0 source is available in the v1.2.2 tag. Previous releases/tags are not replaced.
