# Building the Windows release

## Requirements

- Windows 10/11 x64.
- Python 3.12 x64.
- Inno Setup 6 (`ISCC.exe`).
- Visual Studio C++ Build Tools / MSVC x64 (`cl.exe`), run from its developer shell. The small updater is statically linked (`/MT`); it does not require Python/Qt or an adjacent VC runtime at handoff.
- The dependencies in `requirements-build.txt`.

The release uses **PyInstaller onedir**, then Inno Setup. The executable does not self-extract the entire runtime on every launch. Both native ASR runtimes, Qt platform plugins and audio dependencies must remain with the executable.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-build.txt
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Version 1.2.2
```

Outputs are written to `release/`. No model weights are embedded in the installer.

## Automated release

Pushing a tag such as `v1.2.2` starts `.github/workflows/windows-release.yml`:

1. Install the pinned build dependencies on a Windows runner.
2. Compile the native installer handoff helper, then build and collect native libraries and dependency notices.
3. Download revision/hash-pinned Unified Q4_K_M/Q5_K_M, TDT Q4_K/Q8_0, and sample audio.
4. Verify the frozen executable through Unified → Crisp → additional quantizations → Unified inference. Measure worker memory and verify that actual Engine.unload stops each native process.
5. Build the installer and install it silently into a clean temporary directory.
6. Smoke-test the installed executable, including embedded branding and both shortcut controls.
7. Test a real hash-verified 1.2.1-to-new-version in-place upgrade, native checksum rejection/parent wait/restart, and preserved settings, selected model and custom mappings.
8. Verify real HKCU startup registration, launch the exact registered command from System32, and test preference-preserving reinstalls and uninstall cleanup.
9. Create a portable ZIP, SHA-256 checksums and publish or update the selected GitHub release.

The build's `GITHUB_TOKEN` is supplied by GitHub Actions with repository-content permissions. No personal token belongs in the source, workflow, installer or release assets.

## Smoke test interface

This non-interactive interface is intended for packaging verification, not normal dictation:

```powershell
.\dist\Dictate\Dictate.exe --packaging-smoke-test .\release\smoke.json
```

For native inference, add `--model PATH --audio jfk.wav`. `--model` can be repeated to test model switching through the same application instance. The microphone is never opened by this test.

## Branding and Windows identity

- `assets/logo.png` is the exact supplied 174×179 PNG, without stretching or recoloring.
- `assets/app-icon.png` is the 256×256 transparent icon canvas embedded in `dictate.py`; the original aspect ratio is preserved.
- `assets/app.ico` contains the Windows icon sizes used by the executable and installer.
- `CometSuite.Dictate.Desktop` is the application user model ID used by the application and installer shortcuts.

When replacing an older pinned shortcut, unpin it and pin the newly installed application. Windows may retain old cached icons otherwise.

## Verification and signing

```powershell
Get-FileHash .\release\Dictate-Setup-1.2.2-x64.exe -Algorithm SHA256
```

Compare this output with `SHA256SUMS.txt` from the same release. The initial release is **unsigned**. A hash is an integrity check, not an Authenticode signature.

To distribute a signed installer, sign both the application executable and installer using an appropriate code-signing certificate and a timestamp service. Never commit certificate private keys or passwords.

## Native runtime isolation

On Windows, the two speech runtimes contain incompatible DLLs with the same names. Each loaded model therefore runs in its own local worker process. Audio passes through local IPC only; it is not written to a recording file or sent over a network. The packaged entry point calls `multiprocessing.freeze_support()` before starting the GUI, so worker processes do not open duplicate application windows.

## Windows startup verification

`python tools/check_windows_startup.py --exe "PATH WITH SPACES\Dictate.exe" --installer "PATH\Dictate-Setup-1.2.2-x64.exe" --report startup.json` checks the real per-user Run registry value on Windows. Use a disposable installed test copy: the test reinstalls it and finally uninstalls it. Other startup entries and Windows StartupApproved/policy settings are not modified; the prior Dictate Run value is restored.

The test executes the registered command from System32 with the packaged smoke-test argument, rather than assuming a successful registry write means a valid launch command. It checks both saved enabled and saved disabled preferences through silent reinstall. Results are embedded in `smoke-installed.json`. It does **not** reboot the runner or claim a physical sign-in test.

`Dictate.exe --sync-startup` is an internal installer command: it applies the existing saved preference and exits without creating the GUI or opening a microphone. A Run entry adds `--startup`; logon launches skip registry repair so they do not modify the Run key while Windows processes it.

Publish **v1.2.2 as a new release**; do not move the completed v1.2.0/v1.2.1 tags or replace its assets. Never publish before the Windows verification succeeds.

Registry behavior follows [Microsoft's Run/RunOnce documentation](https://learn.microsoft.com/en-us/windows/win32/setupapi/run-and-runonce-registry-keys). Windows startup-app controls are documented in [Microsoft Support](https://support.microsoft.com/en-us/windows/experience/startup-boot/configure-startup-applications-in-windows).

## Native update helper

`packaging/updater.cpp` builds a small Windows GUI executable with MSVC `/MT`. `build.ps1` compiles it before PyInstaller; `Dictate.spec` bundles it plus its GPL source. The app copies it outside the install tree only after consent. It verifies SHA-256 through Windows BCrypt while holding a read/no-write-delete-sharing handle, waits for the parent PID, runs Inno with an explicit existing `/DIR` and no forced app closure, and restarts the updated installed executable.

`tools/check_windows_update.py` is destructive **only to the disposable copy passed to it**. It downloads the final hash-pinned published v1.2.1 installer, installs that older copy into the explicit test directory, scopes APPDATA, tests a blocked bad hash and parent wait, then performs the new installation and verifies restart plus preferences/files. It restores the user's prior Dictate Run value. Its `--quiet` helper flag suppresses error message boxes in CI; production handoff does not use that flag.

See `docs/UPDATES.md` for update policy and security limits. Network/release/consent decision tests are mocked unit tests; Windows handoff/in-place installation is a separate real integration test. Neither claims a physical reboot or microphone recording.
