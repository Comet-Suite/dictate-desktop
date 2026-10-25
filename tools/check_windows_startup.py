"""Exercise the installed app's opt-in startup registration on a Windows runner.

The runner is not rebooted. The test reads back HKCU's actual Run entry and
executes that exact command from System32 with a packaging-smoke argument.
The user's existing Dictate Run value is restored in finally; no other startup
value, StartupApproved state, or policy is changed.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "Dictate"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    options = parser.parse_args()
    if sys.platform != "win32":
        parser.error("This integration test needs a real Windows registry.")
    import winreg

    executable = options.exe.resolve()
    installer = options.installer.resolve()
    output = options.report.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"status": "running", "checks": [], "reboot_performed": False}

    def checkpoint(name):
        report["stage"] = name
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("Startup test:", name, flush=True)

    def read_entry():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                return winreg.QueryValueEx(key, APP_NAME)
        except FileNotFoundError:
            return None

    def delete_entry():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY,
                                0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, APP_NAME)
        except FileNotFoundError:
            pass

    def write_entry(value, kind=winreg.REG_SZ):
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY,
                               0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, APP_NAME, 0, kind, value)

    original = read_entry()
    checkpoint("starting")
    try:
        assert executable.is_file() and installer.is_file()
        # A space in the install path catches missing Windows command quoting.
        assert " " in str(executable), "Use an installation directory containing spaces."
        with tempfile.TemporaryDirectory(prefix="Dictate Startup Test ") as temporary:
            environment = dict(os.environ, APPDATA=temporary)
            settings_file = Path(temporary) / APP_NAME / "settings.json"
            settings_file.parent.mkdir(parents=True)
            system_directory = Path(os.environ["WINDIR"]) / "System32"
            expected = f'"{executable}" --startup'
            saved_settings = {}

            def preference(enabled):
                saved_settings.clear()
                saved_settings.update({"start_on_startup": enabled, "model_path": "",
                                       "hotkey": "Ctrl+Alt+D", "dict_hotkey": "Ctrl+Alt+Space",
                                       "auto_enter": False, "unload_min": 10})
                settings_file.write_text(json.dumps(saved_settings), encoding="utf-8")

            def run(command, *, timeout=120):
                result = subprocess.run(command, env=environment, cwd=system_directory,
                                        capture_output=True, text=True, timeout=timeout,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                assert result.returncode == 0, (result.returncode, result.stdout[-3000:],
                                                result.stderr[-3000:])

            def check_preference_preserved():
                assert json.loads(settings_file.read_text(encoding="utf-8")) == saved_settings

            def check_enabled():
                assert read_entry() == (expected, winreg.REG_SZ), read_entry()
                assert "_internal" not in expected and "python.exe" not in expected.lower()
                check_preference_preserved()

            def install():
                run([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                     "/NOICONS", "/DIR=" + str(executable.parent),
                     "/LOG=" + str(output.with_suffix(".installer.log"))], timeout=240)

            checkpoint("enable and verify exact installed executable command")
            preference(True)
            delete_entry()
            run([str(executable), "--sync-startup"])
            check_enabled()
            report["registered_command"] = expected
            report["checks"].append("enabled preference creates the current-user Run entry")

            checkpoint("execute exact Run command from System32")
            launch_report = output.with_name("smoke-startup-launch.json")
            command = read_entry()[0] + " --packaging-smoke-test " + subprocess.list2cmdline([str(launch_report)])
            run(command)
            launched = json.loads(launch_report.read_text(encoding="utf-8"))
            assert launched["status"] == "ok" and launched["frozen"] is True
            check_enabled()
            report["checks"].append("quoted Run command launches the frozen app from System32")

            checkpoint("repair removed entry without toggling saved preference")
            delete_entry()
            run([str(executable), "--sync-startup"])
            check_enabled()
            report["checks"].append("missing registration is repaired from the saved enabled preference")

            checkpoint("upgrade repairs stale path before any interactive launch")
            write_entry('"C:\\Missing old Dictate folder\\Dictate.exe"')
            install()
            check_enabled()
            report["checks"].append("silent reinstall repairs a stale path with the saved switch on")

            checkpoint("upgrade respects saved disabled preference")
            preference(False)
            install()
            assert read_entry() is None
            check_preference_preserved()
            report["checks"].append("silent reinstall does not force startup on")

            checkpoint("disable removes only Dictate's startup entry")
            preference(True)
            run([str(executable), "--sync-startup"])
            check_enabled()
            preference(False)
            run([str(executable), "--sync-startup"])
            assert read_entry() is None
            check_preference_preserved()
            report["checks"].append("disabling startup removes Dictate's Run value")

            checkpoint("uninstall cleans enabled startup registration")
            preference(True)
            run([str(executable), "--sync-startup"])
            check_enabled()
            uninstaller = executable.parent / "unins000.exe"
            assert uninstaller.is_file()
            run([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=240)
            assert read_entry() is None
            report["checks"].append("uninstall removes the startup entry")
        report["status"] = "ok"
        checkpoint("complete")
    except Exception:
        report["status"] = "error"
        report["error"] = traceback.format_exc()
        raise
    finally:
        try:
            if original is None:
                delete_entry()
            else:
                write_entry(*original)
        finally:
            output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
