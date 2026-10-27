"""Disposable Windows integration: v1.2.1 -> built installer, real native handoff.

Never run against a personal install. Uses an explicit temporary installation,
scoped APPDATA, public hash-pinned previous installer, and restores the Run value.
No reboot, microphone recording, or unconfirmed update is simulated as tested.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import traceback
import urllib.request

OLD_URL = 'https://github.com/Comet-Suite/dictate-desktop/releases/download/v1.2.1/Dictate-Setup-1.2.1-x64.exe'
OLD_SHA = 'ac260e1a1cdcb6b7677bcafcb2bc222d5f6f08e113486b8276a99ca32a9d375b'
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
INSTALL_KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{FCFDEEC6-10A9-41D4-9263-FEFD00F42E66}_is1'


def sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as source:
        while block:=source.read(1024*1024):h.update(block)
    return h.hexdigest()


def main():
    parser=argparse.ArgumentParser()
    for name in ('exe','installer','helper','report'):parser.add_argument('--'+name,required=True,type=Path)
    options=parser.parse_args()
    if sys.platform!='win32':parser.error('This integration test requires real Windows.')
    import winreg
    executable=options.exe.resolve();installer=options.installer.resolve();helper=options.helper.resolve()
    output=options.report.resolve();output.parent.mkdir(parents=True,exist_ok=True)
    directory=executable.parent
    assert ' ' in str(directory),'Use a disposable directory containing spaces.'
    report={'status':'running','checks':[],'from_version':'1.2.1','reboot_performed':False}
    def checkpoint(text):
        report['stage']=text;output.write_text(json.dumps(report,indent=2),encoding='utf-8')
        print('Updater test:',text,flush=True)
    def passed(text):
        report['checks'].append(text);checkpoint(text)
    def read_run():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,RUN_KEY) as key:return winreg.QueryValueEx(key,'Dictate')
        except FileNotFoundError:return None
    original_run=read_run()
    def installed(name):
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,INSTALL_KEY,0,winreg.KEY_READ|winreg.KEY_WOW64_64KEY) as key:
            return winreg.QueryValueEx(key,name)[0]
    def run(args,environment,timeout=240):
        result=subprocess.run([str(item) for item in args],env=environment,capture_output=True,text=True,
                              encoding='utf-8',errors='replace',timeout=timeout)
        assert result.returncode==0,(result.returncode,result.stdout,result.stderr)
        return result
    def matching_pids(environment):
        environment=dict(environment,DICTATE_TEST_EXE=str(executable))
        script="Get-CimInstance Win32_Process -Filter \"Name='Dictate.exe'\" | Where-Object { $_.ExecutablePath -eq $env:DICTATE_TEST_EXE } | Select-Object -ExpandProperty ProcessId"
        result=run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],environment,60)
        return [int(line) for line in result.stdout.splitlines() if line.strip().isdigit()]
    def stop_copy(environment):
        for pid in matching_pids(environment):
            subprocess.run(['taskkill','/PID',str(pid),'/T','/F'],capture_output=True,timeout=30)
    environment=dict(os.environ)
    checkpoint('starting')
    try:
        assert executable.is_file() and installer.is_file() and helper.is_file()
        expected_executable_sha=sha256(executable)
        target_version=installed('DisplayVersion')
        assert target_version!='1.2.1',target_version
        report['to_version']=target_version
        with tempfile.TemporaryDirectory(prefix='Dictate Update Test ') as temporary:
            temporary=Path(temporary)
            environment=dict(os.environ,APPDATA=str(temporary/'User Profile'))
            profile=Path(environment['APPDATA'])/'Dictate';profile.mkdir(parents=True)
            # The native helper is outside the installation, exactly as in production.
            launch=temporary/'External updater';launch.mkdir()
            copied_helper=launch/helper.name;shutil.copy2(helper,copied_helper)
            previous=temporary/'Dictate-Setup-1.2.1-x64.exe'
            checkpoint('downloading hash-pinned previous installer')
            request=urllib.request.Request(OLD_URL,headers={'User-Agent':'Dictate-Upgrade-Verification'})
            with urllib.request.urlopen(request,timeout=90) as response,previous.open('wb') as out:
                while block:=response.read(1024*1024):out.write(block)
            assert sha256(previous)==OLD_SHA,'Previous installer checksum mismatch'
            passed('published_v121_installer_sha256_verified')
            run([previous,'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/NOICONS',f'/DIR={directory}'],environment)
            assert installed('DisplayVersion')=='1.2.1'
            assert sha256(executable)!=expected_executable_sha
            passed('real_previous_version_installed_in_existing_custom_directory')
            model_directory=temporary/'Existing model library';model_directory.mkdir()
            model_proof=model_directory/'keep-this-model.gguf';model_proof.write_bytes(b'private model sentinel, not a speech model')
            symbols=profile/'My symbols café.txt'
            symbols.write_text('my signature = "Regards,\\nTest user"\nsmile = ✓\n',encoding='utf-8')
            symbols_bytes=symbols.read_bytes();model_bytes=model_proof.read_bytes()
            actual_model=Path(__file__).resolve().parents[1]/'.ci-models'/'unified.gguf'
            settings={'dark_mode':False,'start_on_startup':False,'hotkey':'Ctrl+Alt+D','dict_hotkey':'Ctrl+Alt+Space',
                      'model_path':str(actual_model) if actual_model.is_file() else '',
                      'download_dir':str(model_directory),'unload_min':10,'auto_enter':False,'stop_mode':'manual',
                      'custom_symbols_path':str(symbols),'check_updates':False,
                      'user_upgrade_sentinel':{'number':'005','keep':True}}
            settings_file=profile/'settings.json';settings_file.write_text(json.dumps(settings),encoding='utf-8')
            new_sha=sha256(installer)
            bad=subprocess.run([str(copied_helper),str(os.getpid()),str(installer),str(directory),'0'*64,'--quiet'],
                               env=environment,capture_output=True,timeout=20)
            assert bad.returncode==4,bad.returncode
            assert installed('DisplayVersion')=='1.2.1'
            passed('native_helper_rejects_tampered_checksum_before_installation')
            parent=subprocess.Popen([sys.executable,'-c','import sys; sys.stdin.buffer.read()'],stdin=subprocess.PIPE,env=environment)
            handoff=None
            try:
                checkpoint('starting verified handoff while parent is still alive')
                handoff=subprocess.Popen([str(copied_helper),str(parent.pid),str(installer),str(directory),new_sha,'--quiet'],
                                         cwd=str(launch),env=environment)
                try:
                    handoff.wait(timeout=1)
                    raise AssertionError(f'Helper exited before parent shutdown: {handoff.returncode}')
                except subprocess.TimeoutExpired:
                    pass
                assert parent.poll() is None,'Helper killed the parent instead of waiting'
                assert installed('DisplayVersion')=='1.2.1'
                passed('helper_waits_for_parent_without_terminating_it')
                parent.stdin.close();parent.wait(timeout=10)
                code=handoff.wait(timeout=300);report['helper_exit_code']=code
                if code and (launch/'installer.log').is_file():
                    report['installer_log_tail']=(launch/'installer.log').read_text(encoding='utf-8-sig',errors='replace')[-20000:]
                assert code==0,f'Native upgrade/restart handoff failed: {code}'
            finally:
                if parent.poll() is None:parent.kill();parent.wait(timeout=10)
                if handoff is not None and handoff.poll() is None:handoff.kill();handoff.wait(timeout=10)
            assert installed('DisplayVersion')==target_version
            assert Path(installed('InstallLocation')).resolve()==directory
            assert sha256(executable)==expected_executable_sha
            passed('new_version_installed_into_the_same_registered_directory')
            # WaitForInputIdle in the native helper lets the launched Qt instance
            # initialize before this process inspection. No real microphone is opened.
            pids=matching_pids(environment)
            assert pids,'Updated Dictate was not restarted'
            report['restarted_pids']=pids
            passed('updated_installed_application_restarted')
            saved=json.loads(settings_file.read_text(encoding='utf-8'))
            assert all(saved.get(key)==value for key,value in settings.items()),saved
            assert symbols.read_bytes()==symbols_bytes and model_proof.read_bytes()==model_bytes
            passed('settings_shortcuts_model_selection_and_custom_mappings_preserved')
            stop_copy(environment)
        report['status']='ok'
    except Exception:
        report['status']='error';report['error']=traceback.format_exc()
    finally:
        try:stop_copy(environment)
        except Exception:pass
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,RUN_KEY,0,winreg.KEY_SET_VALUE) as key:
                if original_run is None:
                    try:winreg.DeleteValue(key,'Dictate')
                    except FileNotFoundError:pass
                else:winreg.SetValueEx(key,'Dictate',0,original_run[1],original_run[0])
        finally:output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2),flush=True)
    return 0 if report['status']=='ok' else 1


if __name__=='__main__':sys.exit(main())
