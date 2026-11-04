# Release updates

## First upgrade from 1.2.0

Version 1.2.0 has no built-in release updater. **Install the current version manually once**: finish dictation, quit the existing copy from the tray, run `Dictate-Setup-1.2.3-x64.exe`, and keep the existing installation directory. The Inno Setup application ID is unchanged, so previous installation location and preferences are retained.

## Feature update: 1.2.2 → 1.2.3

Use **Check for updates → Install and restart**. The existing 1.2.2 client uses its existing interface for this upgrade; 1.2.3 includes the new progress UI for later releases. Both model-memory switches are on by default after migration; explicit existing values are preserved.

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

The automated release pipeline tests real Windows installation, a hash-pinned 1.2.2-to-new-version upgrade through the native helper, in-place paths, preference/file preservation and restart. Unit tests separately exercise release parsing, blocked assets, failed verification, cancellation, consent and recording-safety decisions. This does not represent a physical reboot, a microphone test, or an already-performed upgrade to a future release.
