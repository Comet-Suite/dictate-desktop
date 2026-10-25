# Building the Windows release

## Requirements

- Windows 10/11 x64.
- Python 3.12 x64.
- Inno Setup 6 (`ISCC.exe`).
- The dependencies in `requirements-build.txt`.

The release uses **PyInstaller onedir**, then Inno Setup. The executable does not self-extract the entire runtime on every launch. Both native ASR runtimes, Qt platform plugins and audio dependencies must remain with the executable.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-build.txt
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Version 1.2.0
```

Outputs are written to `release/`. No model weights are embedded in the installer.

## Automated release

Pushing a tag such as `v1.2.0` starts `.github/workflows/windows-release.yml`:

1. Install the pinned build dependencies on a Windows runner.
2. Build and collect native libraries and dependency notices.
3. Download hash-verified Unified and TDT test models and sample audio.
4. Verify the frozen executable with both native runtimes.
5. Build the installer and install it silently into a clean temporary directory.
6. Smoke-test the installed executable, including embedded branding and both shortcut controls.
7. Create a portable ZIP, SHA-256 checksums and a GitHub release.

The build's `GITHUB_TOKEN` is supplied by GitHub Actions with repository-content permissions. No personal token belongs in the source, workflow, installer or release assets.

## Smoke test interface

This non-interactive interface is intended for packaging verification, not normal dictation:

```powershell
.\dist\Dictate\Dictate.exe --packaging-smoke-test .\release\smoke.json
```

For native inference, add `--model PATH --audio jfk.wav`. `--model` can be repeated to test both runtimes in the same process. The microphone is never opened by this test.

## Branding and Windows identity

- `assets/logo.png` is the supplied artwork.
- `assets/app-icon.png` is the square, transparent icon canvas embedded in `dictate.py`.
- `assets/app.ico` contains the Windows icon sizes used by the executable and installer.
- `CometSuite.Dictate.Desktop` is the application user model ID used by the application and installer shortcuts.

When replacing an older pinned shortcut, unpin it and pin the newly installed application. Windows may retain old cached icons otherwise.

## Verification and signing

```powershell
Get-FileHash .\release\Dictate-Setup-1.2.0-x64.exe -Algorithm SHA256
```

Compare this output with `SHA256SUMS.txt` from the same release. The initial release is **unsigned**. A hash is an integrity check, not an Authenticode signature.

To distribute a signed installer, sign both the application executable and installer using an appropriate code-signing certificate and a timestamp service. Never commit certificate private keys or passwords.
