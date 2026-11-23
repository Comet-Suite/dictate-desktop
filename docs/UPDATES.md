# Release updates

## First upgrade from 1.2.0

Version 1.2.0 has no built-in release updater. **Install the current version manually once**: finish dictation, quit the existing copy from the tray, run `Dictate-Setup-1.2.6-x64.exe`, and keep the existing installation directory. The Inno Setup application ID is unchanged, so previous installation location and preferences are retained.

## Hover controls and Win-key shortcuts: 1.2.5 → 1.2.6

Approve **Install and restart**, or quit Dictate and run the current official installer into the same folder. The mic now starts explicit recording, loading first when needed; showing/preloading the popup still never records. Recording normally shows only moving bars. Hover for ✕ / Pause–Resume / ✓ over the waveform, and right-click for Settings.

Distinct shortcut preferences and both defaults are retained. Supported assigned Win chords, including Win+H/Win+V, suppress their matched native feature only while Dictate runs; assigning a duplicate clears the older Dictate binding. Security combinations are protected and no permanent Windows policies are changed. [Shortcut guide →](SHORTCUTS.md)

The Windows upgrade fixture starts from the final hash-pinned published **1.2.5** installer. It checks native checksum rejection, parent wait, same-directory upgrade and restart, then reads the old file with the new binary to verify lock, all appearance fields, size, position and the single unload policy without rewriting preferences. Model/custom mapping files, installation identity and startup preference are retained.

## Popup/settings update: 1.2.4 → 1.2.5

Use **Check for updates → Install and restart**, or run the official current installer after quitting the existing app. The installation folder, both shortcuts, selected/downloaded models, custom-symbol files and startup preference are retained. The 1.2.4 readable updater colors and consent policy are unchanged.

Version 1.2.5 adds default-on saved position lock, Tiny/Extra small, voice-bar color/height and popup opacity. It replaces the two model-memory switches and manual unload control with one ordered **5/10/15/Never/Immediately after pasting** choice. Old post-paste-on settings become Immediately; with that switch off, the old idle value is kept. Old settings with only an idle value retain it. Showing the popup preloads without recording; startup stays lazy. No model files move or redownload for this settings migration.

The historical 1.2.5 Windows upgrade fixture started from the final hash-pinned published **1.2.4** installer, performs the native in-place handoff and restart, checks file/preference preservation, then verifies the new binary reads the old memory policy and custom popup position correctly without overwriting the saved file.

## Historical contrast patch: 1.2.3 → 1.2.4

The updater has explicit native palettes and text/control colors in all window states. Secondary text and primary-button colors have stronger contrast, and changing Dark mode updates an open progress/confirmation window. Automated Qt rendering checks cover dark/light UI against deliberately unreadable inherited application palettes; frozen and installed Windows builds run the same checks.

Choose **Check for updates → Install and restart**. If an old 1.2.1/1.2.2 confirmation has black-on-black text, the current running app still owns that window until restart. Use its Install and restart button or quit and run the official installer into the registered folder. Releasing a new package cannot repaint code already running in the old process.

## Feature update: 1.2.2 → 1.2.3

Use **Check for updates → Install and restart**. The existing 1.2.2 client uses its existing interface for this upgrade; 1.2.3 includes the new progress UI for later releases. In 1.2.3, both model-memory switches defaulted on. Those historical switches are superseded by the single timing choice in 1.2.5, as described above.

## Measured progress in 1.2.3

Open **Update progress…** in Settings or the tray, or choose Check for updates. Background checks do not open a progress window over active dictation. The Settings card and dedicated rounded window share the same worker state:

1. Check the official stable release and checksum manifest.
2. Stream measured bytes, with percentage, transferred/total MiB, average speed and estimated time. Animation interpolates reported work; it does not manufacture transfer progress.
3. Re-read the persisted installer and verify SHA-256 separately. Cached installers are also rehashed; they are not shown as a new download.
4. Offer **Install and restart** or **Later** only after successful verification and when dictation is safe. Enter defaults to Later. Closing/hiding download progress is not install consent; Cancel download cancels the worker and removes its partial file. A network call already in progress may take its timeout before cancellation finishes.
5. After approval, the native helper re-verifies the bytes, waits for this app to close, then launches Inno Setup with **/SILENT** so the real file-install progress is visible. No Python/Qt runtime locks are carried into installation. It restarts the app after success.

A same-version/downgrade, bad hash, failed download or cancellation never becomes an installable update. Network errors do not disable offline dictation. Retry with Check for updates. Later suppresses repeated automatic prompts for that version; explicitly checking/choosing installation can offer it again.

## Historical logo update: 1.2.1 → 1.2.2

The updater in 1.2.1 can receive 1.2.2 directly. Use **Check for updates** in Settings or the tray to check immediately, or leave automatic checks on. Approve **Install and restart** after the verified download. The new logo appears when the updated app restarts; dictation behavior, selected model, mappings and settings are unchanged. Windows may cache pinned icons—unpin/re-pin only if an old icon remains.

## From 1.2.1 onward

**Settings → Release updates** is enabled by default. Dictate checks the official stable GitHub release shortly after launch and approximately every six hours. You can also choose **Check for updates** in Settings or the tray.

1. Read the latest stable release of `Comet-Suite/dictate-desktop` over HTTPS.
2. Compare numeric `major.minor.patch` versions. Same versions, older versions, drafts and prereleases are not installed.
3. Download only the exactly named Windows x64 installer. The GitHub SHA-256 asset digest and `SHA256SUMS.txt` must agree; downloaded bytes and size must match.
4. Wait until dictation, pending transcription/insertion and model loading are finished. Continuous legacy auto-listening must have its microphone turned off.
5. **Ask for confirmation**, showing the existing destination folder. **Later** does not close Dictate or install anything, and suppresses repeat prompts for that version during the current session. Check manually to ask again.
6. After **Install and restart**, recheck the dictation state and the downloaded file. A small native helper outside the installation verifies SHA-256 again, waits for the app to exit, installs into the existing folder and restarts that copy.

No Python or Qt DLLs remain loaded from the installation during the native handoff. The helper does not kill the parent or other Dictate instances, and the installer is instructed not to close applications. If a new instance holds files open, installation may fail rather than forcibly interrupt it.

The registered Inno installation directory takes priority. Installed-folder fallback is allowed only for a folder containing Dictate and its Inno uninstaller. Portable/source copies without an installed copy should run the verified installer manually. **Microsoft Store/WindowsApps paths are not overwritten**; those installations must use their store. Protected installation directories still obey Windows permissions; Dictate does not bypass them.

## What is retained

- `%APPDATA%\Dictate\settings.json`, including both shortcuts, startup preference and model selection.
- The selected custom-symbol file and its saved path.
- Downloaded models in your chosen directory.
- The existing installation location.

Downloads are streamed to `%APPDATA%\Dictate\updates` and a partial download is not offered for installation. Failed verification blocks installation. A network/download failure does not stop offline dictation; use Check for updates to retry. Turning off the switch stops automatic checks/downloads; manual checking remains available.

If installation fails after consent, the native helper reports the failure. Its Inno log is under `%APPDATA%\Dictate\updates\launch-*\installer.log`; keep logs private unless you have reviewed their local paths.

## Trust and limitations

The updater uses the official repository and HTTPS, restricts asset redirects to GitHub's release infrastructure, and checks hashes twice. **A hash is an integrity check, not an Authenticode signature or an independent signing authority.** Installers are currently unsigned. Repository compromise or local compromise of your account/application is outside the protection provided by a checksum.

Release checks contact GitHub; model downloads contact the selected model host. Neither operation uploads microphone audio or transcripts. Dictation remains local and works offline after the model is installed.

The automated release pipeline tests real Windows installation, a hash-pinned 1.2.5-to-new-version upgrade through the native helper, in-place paths, preference/file preservation and restart. Unit tests separately exercise release parsing, blocked assets, failed verification, cancellation, consent and recording-safety decisions. This does not represent a physical reboot, a microphone test, or an already-performed upgrade to a future release.
