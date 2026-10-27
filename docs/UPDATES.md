# Release updates

## First upgrade from 1.2.0

Version 1.2.0 has no built-in release updater. **Install 1.2.1 manually once**: finish dictation, quit the existing copy from the tray, run `Dictate-Setup-1.2.1-x64.exe`, and keep the existing installation directory. The Inno Setup application ID is unchanged, so previous installation location and preferences are retained.

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

The automated release pipeline tests real Windows installation, a hash-pinned 1.2.0-to-new-version upgrade through the native helper, in-place paths, preference/file preservation and restart. Unit tests separately exercise release parsing, blocked assets, failed verification, cancellation, consent and recording-safety decisions. This does not represent a physical reboot, a microphone test, or an already-performed upgrade to a future release.
