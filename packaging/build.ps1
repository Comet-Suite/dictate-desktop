param(
    [string]$Version = "1.2.0",
    [switch]$TestNativeModels,
    [switch]$TestInstaller
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Version must be x.y.z" }

function Run-Command {
    param([string]$File, [string[]]$Arguments)
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$File failed with exit code $LASTEXITCODE" }
}
function Run-Smoke {
    param([string]$Exe, [string]$Report, [switch]$Native)
    $Arguments = "--packaging-smoke-test `"$Report`""
    if ($Native) {
        $Arguments += " --model `"$Root\.ci-models\unified.gguf`" --model `"$Root\.ci-models\tdt.gguf`" --model `"$Root\.ci-models\unified.gguf`" --audio `"$Root\.ci-models\jfk.wav`""
    }
    $Process = Start-Process -FilePath $Exe -ArgumentList $Arguments -PassThru -NoNewWindow -RedirectStandardOutput "$Report.stdout.log" -RedirectStandardError "$Report.stderr.log"
    if (-not $Process.WaitForExit(120000)) {
        $Process.Kill()
        if (Test-Path $Report) { Get-Content $Report }
        if (Test-Path "$Report.startup.log") { Get-Content "$Report.startup.log" }
        throw "Packaged smoke test timed out"
    }
    if (Test-Path "$Report.startup.log") { Get-Content "$Report.startup.log" }
    if (Test-Path "$Report.stderr.log") { Get-Content "$Report.stderr.log" }
    if (Test-Path $Report) { Get-Content $Report }
    if ($Process.ExitCode -ne 0) { throw "Smoke test exited with $($Process.ExitCode)" }
    if (-not (Test-Path $Report)) { throw "Smoke test report is missing" }
    $Result = Get-Content $Report -Raw | ConvertFrom-Json
    if ($Result.status -ne "ok") { throw "Smoke test failed" }
}

New-Item -ItemType Directory -Force release | Out-Null
Run-Command "python" @("tools/collect_licenses.py")
Run-Command "python" @("-m", "PyInstaller", "--noconfirm", "--clean", "packaging/Dictate.spec")
Run-Command "python" @("-m", "pip", "freeze")
$Inventory = & python -m pip freeze
$Inventory | Set-Content "release/requirements-build-resolved.txt" -Encoding utf8

if ($TestNativeModels) { Run-Command "python" @("tools/download_smoke_models.py") }
Run-Smoke -Exe "$Root\dist\Dictate\Dictate-Diagnostics.exe" -Report "$Root\release\smoke-diagnostic.json" -Native:$TestNativeModels
Run-Smoke -Exe "$Root\dist\Dictate\Dictate.exe" -Report "$Root\release\smoke-frozen.json"
Remove-Item "$Root\dist\Dictate\Dictate-Diagnostics.exe"

$Compiler = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $Compiler)) {
    $Command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($Command) { $Compiler = $Command.Source }
    else { throw "Install Inno Setup 6 and add ISCC.exe to PATH." }
}
Run-Command $Compiler @("/DAppVersion=$Version", "packaging/Dictate.iss")
$Setup = "$Root\release\Dictate-Setup-$Version-x64.exe"
if (-not (Test-Path $Setup)) { throw "Installer was not created" }

if ($TestInstaller) {
    $BaseTemp = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
    $InstallRoot = Join-Path $BaseTemp "Dictate Install Smoke"
    $Installer = Start-Process -FilePath $Setup -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /NOICONS /DIR=`"$InstallRoot`"" -PassThru
    if (-not $Installer.WaitForExit(240000)) { $Installer.Kill(); throw "Installer timed out" }
    if ($Installer.ExitCode -ne 0) { throw "Installer failed: $($Installer.ExitCode)" }
    Run-Smoke -Exe "$InstallRoot\Dictate.exe" -Report "$Root\release\smoke-installed.json" -Native:$TestNativeModels
    Run-Command "python" @("tools/check_windows_startup.py", "--exe", "$InstallRoot\Dictate.exe", "--installer", $Setup, "--report", "$Root\release\smoke-windows-startup.json")
    $InstalledReport = Get-Content "$Root\release\smoke-installed.json" -Raw | ConvertFrom-Json
    $StartupReport = Get-Content "$Root\release\smoke-windows-startup.json" -Raw | ConvertFrom-Json
    $InstalledReport | Add-Member -NotePropertyName windows_startup -NotePropertyValue $StartupReport
    $InstalledReport | ConvertTo-Json -Depth 10 | Set-Content "$Root\release\smoke-installed.json" -Encoding utf8
}

$Archive = "$Root\release\Dictate-$Version-Windows-x64.zip"
if (Test-Path $Archive) { Remove-Item $Archive }
Compress-Archive -Path "$Root\dist\Dictate" -DestinationPath $Archive -CompressionLevel Optimal
$Hashes = Get-ChildItem "$Root\release" -File | Where-Object { $_.Extension -in ".exe", ".zip" } | ForEach-Object {
    $Hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$Hash  $($_.Name)"
}
$Hashes | Set-Content "$Root\release\SHA256SUMS.txt" -Encoding ascii
Write-Host "Created installer, portable package and checksums."
