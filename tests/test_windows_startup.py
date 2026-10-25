"""Startup registration must follow the saved opt-in, including upgrades."""
import ast
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as m


class Key:
    def __init__(self, registry):
        self.registry = registry
        self.closed = False
        registry.handles.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class Registry:
    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE, KEY_QUERY_VALUE = 2, 1
    REG_SZ, REG_EXPAND_SZ = 1, 2

    def __init__(self):
        self.exists = False
        self.values = {}
        self.handles = []
        self.creates = []
        self.writes = []
        self.fail_write = False
        self.ignore_write = False

    def OpenKey(self, hive, path, reserved, access):
        assert hive == self.HKEY_CURRENT_USER and path == m._WINDOWS_RUN_KEY
        if not self.exists:
            raise FileNotFoundError
        return Key(self)

    def CreateKeyEx(self, hive, path, reserved, access):
        assert hive == self.HKEY_CURRENT_USER and path == m._WINDOWS_RUN_KEY
        self.creates.append(access)
        self.exists = True
        return Key(self)

    def QueryValueEx(self, key, name):
        assert not key.closed
        if name not in self.values:
            raise FileNotFoundError
        return self.values[name]

    def SetValueEx(self, key, name, reserved, kind, value):
        assert not key.closed
        if self.fail_write:
            raise PermissionError("Registry update denied")
        self.writes.append((name, value, kind))
        if not self.ignore_write:
            self.values[name] = (value, kind)

    def DeleteValue(self, key, name):
        assert not key.closed
        if name not in self.values:
            raise FileNotFoundError
        del self.values[name]


@pytest.fixture
def registry(monkeypatch):
    registry = Registry()
    monkeypatch.setitem(sys.modules, "winreg", registry)
    monkeypatch.setattr(m, "_startup_command", lambda: '"C:\\Users\\Person Name\\Dictate\\Dictate.exe" --startup')
    yield registry
    assert all(key.closed for key in registry.handles)


def test_enable_creates_missing_current_user_run_key(registry):
    assert m.set_startup(True)
    assert registry.creates == [registry.KEY_SET_VALUE | registry.KEY_QUERY_VALUE]
    assert registry.values[m.APP_NAME] == (m._startup_command(), registry.REG_SZ)


def test_enable_populates_existing_key_without_deleting_other_apps(registry):
    registry.exists = True
    registry.values["Other application"] = ('"C:\\other.exe"', registry.REG_SZ)
    assert m.set_startup(True)
    assert registry.values["Other application"] == ('"C:\\other.exe"', registry.REG_SZ)


@pytest.mark.parametrize("stale", ['"C:\\Old\\Dictate.exe"', '"pythonw.exe" "old.py"', '"C:\\Dictate\\_internal\\dictate.py"'])
def test_saved_enabled_preference_repairs_stale_path(registry, stale):
    registry.exists = True
    registry.values[m.APP_NAME] = (stale, registry.REG_SZ)
    assert m.set_startup(True)
    assert registry.values[m.APP_NAME] == (m._startup_command(), registry.REG_SZ)


def test_valid_entry_is_not_rewritten(registry):
    registry.exists = True
    registry.values[m.APP_NAME] = (m._startup_command(), registry.REG_SZ)
    assert m.set_startup(True)
    assert not registry.creates and not registry.writes


def test_wrong_registry_value_type_is_repaired(registry):
    registry.exists = True
    registry.values[m.APP_NAME] = (m._startup_command(), registry.REG_EXPAND_SZ)
    assert m.set_startup(True)
    assert registry.values[m.APP_NAME][1] == registry.REG_SZ


def test_disable_removes_only_this_application(registry):
    registry.exists = True
    registry.values = {m.APP_NAME: ("old", registry.REG_SZ), "Other": ("keep", registry.REG_SZ)}
    assert m.set_startup(False)
    assert registry.values == {"Other": ("keep", registry.REG_SZ)}


@pytest.mark.parametrize("exists", [True, False])
def test_disable_is_idempotent_and_does_not_create_run_key(registry, exists):
    registry.exists = exists
    assert m.set_startup(False)
    assert m.set_startup(False)
    assert not registry.creates and not registry.values


def test_write_denied_is_reported_and_handle_closed(registry):
    registry.fail_write = True
    assert not m.set_startup(True)
    assert all(key.closed for key in registry.handles)


def test_registration_is_read_back_instead_of_assuming_success(registry):
    registry.exists = True
    registry.values[m.APP_NAME] = ("stale", registry.REG_SZ)
    registry.ignore_write = True
    assert not m.set_startup(True)


@pytest.mark.parametrize("enabled", [True, False])
def test_manual_launch_reconciles_saved_preference(monkeypatch, enabled):
    setter = Mock(return_value=True)
    monkeypatch.setattr(m, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(m, "set_startup", setter)
    assert m.sync_startup({"start_on_startup": enabled})
    setter.assert_called_once_with(enabled)


def test_registration_failure_is_propagated(monkeypatch):
    monkeypatch.setattr(m, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(m, "set_startup", Mock(return_value=False))
    assert not m.sync_startup({"start_on_startup": True})


@pytest.mark.parametrize("platform,login", [("posix", False), ("nt", True)])
def test_non_windows_and_logon_launches_do_not_modify_registry(monkeypatch, platform, login):
    setter = Mock()
    monkeypatch.setattr(m, "os", SimpleNamespace(name=platform))
    monkeypatch.setattr(m, "set_startup", setter)
    assert m.sync_startup({"start_on_startup": True}, started_at_login=login)
    setter.assert_not_called()


def test_packaged_command_quotes_executable_not_internal_script(monkeypatch, tmp_path):
    exe = tmp_path / "Folder With Spaces" / "Dictate.exe"
    monkeypatch.setattr(m.sys, "executable", str(exe))
    monkeypatch.setattr(m.sys, "frozen", True, raising=False)
    assert m._startup_command() == f'"{exe}" --startup'
    assert "_internal" not in m._startup_command()


@pytest.mark.parametrize("windowless_available", [True, False])
def test_source_command_uses_absolute_script_and_matching_python(monkeypatch, tmp_path, windowless_available):
    exe = tmp_path / "Python With Spaces" / "python.exe"
    exe.parent.mkdir()
    if windowless_available:
        exe.with_name("pythonw.exe").touch()
    monkeypatch.setattr(m.sys, "executable", str(exe))
    monkeypatch.setattr(m.sys, "frozen", False, raising=False)
    interpreter = exe.with_name("pythonw.exe") if windowless_available else exe
    assert m._startup_command() == f'"{interpreter}" "{Path(m.__file__).resolve()}" --startup'


def test_overlong_windows_run_command_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(m.sys, "executable", str(tmp_path / ("x" * 261) / "Dictate.exe"))
    monkeypatch.setattr(m.sys, "frozen", True, raising=False)
    with pytest.raises(ValueError, match="too long"):
        m._startup_command()


def test_default_stays_opt_in_and_does_not_reset_saved_choice(monkeypatch, tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text('{"start_on_startup": true, "model_path": "keep.gguf"}')
    monkeypatch.setattr(m, "SETTINGS_FILE", settings)
    assert m.DEFAULTS["start_on_startup"] is False
    assert m.load_settings()["start_on_startup"] is True
    assert m.load_settings()["model_path"] == "keep.gguf"


def test_application_reconciliation_and_frozen_worker_dispatch_are_wired():
    root = Path(__file__).resolve().parents[1]
    source = (root / "dictate.py").read_text()
    tree = ast.parse(source)
    app = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "App")
    initializer = next(node for node in app.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    calls = [node for node in ast.walk(initializer) if isinstance(node, ast.Call)]
    assert any(isinstance(call.func, ast.Name) and call.func.id == "sync_startup" for call in calls)
    main = tree.body[-1]
    assert isinstance(main, ast.If)
    assert ast.unparse(main.body[0]) == "multiprocessing.freeze_support()"
    assert 'sys.exit(0 if sync_startup(load_settings()) else 1)' in ast.get_source_segment(source, main)
    assert 'App(started_at_login=started_at_login).run()' in ast.get_source_segment(source, main)


def test_installer_applies_saved_preference_without_forcing_opt_in():
    root = Path(__file__).resolve().parents[1]
    source = (root / "packaging/Dictate.iss").read_text()
    run = source.split("[Run]", 1)[1]
    sync = next(line for line in run.splitlines() if 'Parameters: "--sync-startup"' in line)
    assert "waituntilterminated" in sync and "runhidden" in sync
    assert "skipifsilent" not in sync and "postinstall" not in sync
    assert run.index('--sync-startup') < run.index('Description: "Launch Dictate"')
    assert 'ValueName: "{#AppName}"; Flags: uninsdeletevalue' in source
