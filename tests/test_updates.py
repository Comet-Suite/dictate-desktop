"""No network/executable launches: deterministic release-update security and consent tests."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as m
from test_targeted_fix import Engine
from test_windows_runtime import spawn_stub

PAYLOAD=b'MZ'+b'verified official installer test bytes'*30
DIGEST=hashlib.sha256(PAYLOAD).hexdigest()


def release(version='1.2.3'):
    base=f'https://github.com/{m._UPDATE_REPOSITORY}/releases/download/v{version}/'
    name=f'Dictate-Setup-{version}-x64.exe'
    return {'tag_name':'v'+version,'draft':False,'prerelease':False,'assets':[
        {'name':name,'size':len(PAYLOAD),'digest':'sha256:'+DIGEST,'browser_download_url':base+name},
        {'name':'SHA256SUMS.txt','size':100,'browser_download_url':base+'SHA256SUMS.txt'}]}


class Response(io.BytesIO):
    def __init__(self, data, length=None, url=''):
        super().__init__(data);self.headers={} if length is None else {'Content-Length':str(length)};self.url=url
    def geturl(self):return self.url


def downloads(monkeypatch, candidate, data=PAYLOAD, manifest=None, length=None):
    urls=[]
    if manifest is None:manifest=f'{DIGEST}  {candidate["name"]}\n'.encode()
    def open_(url):
        urls.append(url)
        return Response(manifest) if url.endswith('SHA256SUMS.txt') else Response(data,length)
    monkeypatch.setattr(m,'_update_open',open_)
    return urls


@pytest.mark.parametrize('old,new', [('1.2.1','1.2.3'),('1.2.9','1.2.10'),('1.9.9','1.10.0'),('1.99.99','2.0.0')])
def test_version_comparison_is_numeric_not_lexicographic(old,new):
    assert m._update_candidate(release(new),old)['version']==new


@pytest.mark.parametrize('version',['1.2.2','1.2.1','1.2.0','0.99.99'])
def test_same_version_and_downgrades_are_not_updates(version):
    assert m._update_candidate(release(version)) is None


@pytest.mark.parametrize('bad',['latest','v1.2','1.2.3-beta','1.2.3+build','01.2.3','-1.2.3','1.2.3/evil','1.2.3\n','1.2.9999999'])
def test_only_strict_stable_semver_is_accepted(bad):
    with pytest.raises(ValueError):m._release_version(bad)


@pytest.mark.parametrize('flag',['draft','prerelease'])
def test_unpublished_and_preview_releases_are_ignored(flag):
    value=release();value[flag]=True;assert m._update_candidate(value) is None


@pytest.mark.parametrize('mutation',[
    lambda r:r['assets'].pop(),
    lambda r:r['assets'].append(deepcopy(r['assets'][0])),
    lambda r:r['assets'][0].update(browser_download_url='https://attacker.example/Dictate.exe'),
    lambda r:r['assets'][0].update(browser_download_url='https://github.com/Someone/Other/releases/download/v1.2.3/file.exe'),
    lambda r:r['assets'][0].update(size=0),
    lambda r:r['assets'][0].update(size=m._UPDATE_MAX_BYTES+1),
    lambda r:r['assets'][0].update(size=True),
    lambda r:r['assets'][0].pop('digest'),
    lambda r:r['assets'][0].update(digest='sha256:not-a-checksum'),
    lambda r:r['assets'][0].update(name='../arbitrary.exe'),
])
def test_release_assets_are_exact_whitelisted_and_digest_required(mutation):
    value=release();mutation(value)
    with pytest.raises(ValueError):m._update_candidate(value)


@pytest.mark.parametrize('url',[
    'http://github.com/Comet-Suite/dictate-desktop/releases/download/v1.2.3/file',
    'file:///C:/Windows/calc.exe','https://github.com.evil.example/path',
    'https://github.com@evil.example/path','https://evil@github.com/Comet-Suite/dictate-desktop/releases/download/v1/x',
    'https://github.com:8443/Comet-Suite/dictate-desktop/releases/download/v1/x',
    'https://github.com/Comet-Suite/other/releases/download/v1/x',
    'https://release-assets.githubusercontent.com.evil.example/x',
    'https://github.com/Comet-Suite/dictate-desktop/releases/download/v1/x#fragment'])
def test_unsafe_initial_and_redirect_urls_are_rejected(url):
    with pytest.raises(ValueError):m._approved_update_url(url,redirects=True)


@pytest.mark.parametrize('host',['release-assets.githubusercontent.com','objects.githubusercontent.com','github-releases.githubusercontent.com'])
def test_github_asset_redirects_are_allowed_but_not_as_initial_urls(host):
    url=f'https://{host}/github-production-release-asset/123?token=temporary'
    assert m._approved_update_url(url,redirects=True)==url
    with pytest.raises(ValueError):m._approved_update_url(url)


def test_invalid_url_is_rejected_before_any_network_request(monkeypatch):
    import urllib.request
    opener=Mock();monkeypatch.setattr(urllib.request,'build_opener',opener)
    with pytest.raises(ValueError):m._update_open('https://attacker.example/executable')
    opener.assert_not_called()


def test_update_json_bounded_and_object_only(monkeypatch):
    monkeypatch.setattr(m,'_update_open',lambda _:Response(json.dumps(release()).encode()))
    assert m._update_json()['tag_name']=='v1.2.3'
    monkeypatch.setattr(m,'_update_open',lambda _:Response(b'[]'))
    with pytest.raises(ValueError):m._update_json()
    monkeypatch.setattr(m,'_update_open',lambda _:Response(b' '* (2*1024*1024+1)))
    with pytest.raises(ValueError,match='too large'):m._update_json()


def test_verified_download_is_atomic_cached_and_reports_progress(tmp_path,monkeypatch):
    candidate=m._update_candidate(release());urls=downloads(monkeypatch,candidate,length=len(PAYLOAD));progress=[]
    path=m._download_update(candidate,tmp_path,progress=progress.append)
    assert path.read_bytes()==PAYLOAD and progress[-1]==100
    assert not list(tmp_path.glob('*.partial'))
    assert m._download_update(candidate,tmp_path)==path
    assert urls.count(candidate['url'])==1  # cached copy is rehashed, not redownloaded


@pytest.mark.parametrize('mode',['wrong-hash','short','long','bad-length','manifest-disagreement','missing-checksum','duplicate-checksum'])
def test_bad_downloads_never_become_installable(tmp_path,monkeypatch,mode):
    candidate=m._update_candidate(release());data=PAYLOAD;manifest=None;length=None
    if mode=='wrong-hash':data=b'X'*len(PAYLOAD)
    elif mode=='short':data=PAYLOAD[:-1]
    elif mode=='long':data=PAYLOAD+b'X'
    elif mode=='bad-length':length=len(PAYLOAD)+1
    elif mode=='manifest-disagreement':manifest=f'{"0"*64}  {candidate["name"]}'.encode()
    elif mode=='missing-checksum':manifest=f'{DIGEST}  other.exe'.encode()
    elif mode=='duplicate-checksum':manifest=(f'{DIGEST}  {candidate["name"]}\n'*2).encode()
    downloads(monkeypatch,candidate,data,manifest,length)
    with pytest.raises(ValueError):m._download_update(candidate,tmp_path)
    assert not list(tmp_path.iterdir())


def test_corrupt_cached_installer_is_replaced_only_after_verification(tmp_path,monkeypatch):
    candidate=m._update_candidate(release());path=tmp_path/candidate['name'];path.write_bytes(b'x'*len(PAYLOAD))
    downloads(monkeypatch,candidate)
    assert m._download_update(candidate,tmp_path).read_bytes()==PAYLOAD


def test_cancellation_and_path_traversal_do_not_write_or_contact_network(tmp_path,monkeypatch):
    candidate=m._update_candidate(release());open_=Mock();monkeypatch.setattr(m,'_update_open',open_)
    cancel=threading.Event();cancel.set()
    with pytest.raises(InterruptedError):m._download_update(candidate,tmp_path,cancel)
    candidate['name']='../unsafe.exe'
    with pytest.raises(ValueError):m._download_update(candidate,tmp_path)
    open_.assert_not_called()


def application(candidate=None):
    app=m.App.__new__(m.App)
    app.engine=SimpleNamespace(recording=False,pending=0,busy=False,loading=False,speaking=False,
                               listening=False,manual=False,_shortcut_mode=True)
    app.updater=SimpleNamespace(candidate=candidate or m._update_candidate(release()),installer=Path('verified.exe'),
                                deferred_version='',manual=False,status='',_status=Mock())
    app._quitting=app._update_prompt_open=app._dictation_after_load=False
    app.settings={'check_updates':True};app.dialog=None;app.say=Mock();app.quit=Mock()
    return app


def dialog(monkeypatch,answer,during=None):
    factory=Mock()
    class Box:
        Icon=SimpleNamespace(Question=1);StandardButton=SimpleNamespace(Yes=1,No=2)
        def __init__(self,*args):factory(*args)
        def setTextFormat(self,*args):pass
        def setDefaultButton(self,*args):pass
        def button(self,*args):return Mock()
        def exec(self):
            if during:during()
            return answer
    monkeypatch.setattr(m,'QMessageBox',Box)
    return factory


@pytest.mark.parametrize('flag',['recording','pending','busy','loading','speaking'])
def test_confirmation_does_not_interrupt_dictation(monkeypatch,flag):
    app=application();setattr(app.engine,flag,True)
    box=dialog(monkeypatch,1);launch=Mock();monkeypatch.setattr(m,'_launch_update_helper',launch)
    app.consider_update();box.assert_not_called();launch.assert_not_called();app.quit.assert_not_called()


def test_continuous_auto_listening_is_not_interrupted(monkeypatch):
    app=application();app.engine.listening=True;app.engine._shortcut_mode=False
    box=dialog(monkeypatch,1);app.consider_update();box.assert_not_called()


def test_later_means_no_install_no_quit_and_no_repeated_prompt(monkeypatch,tmp_path):
    app=application();box=dialog(monkeypatch,2)
    monkeypatch.setattr(m,'_installed_update_directory',lambda:tmp_path)
    launch=Mock();monkeypatch.setattr(m,'_launch_update_helper',launch)
    app.consider_update();app.consider_update()
    assert app.updater.deferred_version=='1.2.3' and box.call_count==1
    launch.assert_not_called();app.quit.assert_not_called()


def test_consent_launches_only_verified_helper_in_existing_directory(monkeypatch,tmp_path):
    app=application();box=dialog(monkeypatch,1)
    monkeypatch.setattr(m,'_installed_update_directory',lambda:tmp_path)
    launch=Mock();monkeypatch.setattr(m,'_launch_update_helper',launch)
    app.consider_update()
    launch.assert_called_once_with(app.updater.candidate,app.updater.installer,tmp_path)
    app.quit.assert_called_once();assert str(tmp_path) in box.call_args.args[2]


def test_recording_started_inside_modal_confirmation_prevents_install(monkeypatch,tmp_path):
    app=application();dialog(monkeypatch,1,during=lambda:setattr(app.engine,'recording',True))
    monkeypatch.setattr(m,'_installed_update_directory',lambda:tmp_path)
    launch=Mock();monkeypatch.setattr(m,'_launch_update_helper',launch)
    app.consider_update();launch.assert_not_called();app.quit.assert_not_called()


def test_nested_timer_cannot_show_two_confirmation_dialogs(monkeypatch,tmp_path):
    app=application();box=dialog(monkeypatch,2,during=app.consider_update)
    monkeypatch.setattr(m,'_installed_update_directory',lambda:tmp_path)
    app.consider_update();assert box.call_count==1


def test_automatic_updates_off_does_not_check_but_manual_check_is_allowed(monkeypatch):
    app=application();app.settings['check_updates']=False;app.updater.check=Mock()
    monkeypatch.setattr(m,'os',SimpleNamespace(name='nt'))
    app.check_updates();app.updater.check.assert_not_called()
    app.check_updates(manual=True);app.updater.check.assert_called_once_with(manual=True)


def test_packaged_helper_has_no_shell_uses_existing_folder_and_is_reverified(tmp_path,monkeypatch):
    candidate=m._update_candidate(release());installer=tmp_path/candidate['name'];installer.write_bytes(PAYLOAD)
    installed=tmp_path/'My existing folder';installed.mkdir();(installed/'Dictate.exe').write_bytes(b'MZold')
    resources=tmp_path/'resources';resources.mkdir();(resources/'Dictate-Updater.exe').write_bytes(b'MZhelper')
    monkeypatch.setattr(m.sys,'frozen',True,raising=False);monkeypatch.setattr(m.sys,'_MEIPASS',str(resources),raising=False)
    monkeypatch.setattr(m,'APP_DIR',tmp_path/'settings')
    monkeypatch.setattr(m,'os',SimpleNamespace(name='nt',getpid=lambda:1234,urandom=lambda n:b'X'*n))
    popen=Mock();monkeypatch.setattr(m.subprocess,'Popen',popen)
    m._launch_update_helper(candidate,installer,installed)
    args=popen.call_args.args[0]
    assert args[1:]==['1234',str(installer),str(installed),DIGEST]
    assert 'shell' not in popen.call_args.kwargs
    assert installed not in Path(args[0]).parents
    installer.write_bytes(b'corrupted')
    with pytest.raises(ValueError):m._launch_update_helper(candidate,installer,installed)
    assert popen.call_count==1


def test_native_worker_is_force_closed_during_shutdown(qt,monkeypatch):
    parent,_,process=spawn_stub(monkeypatch);session=m._WindowsNativeSession('model.gguf');process.alive=True
    e=Engine();e.model=session;e.shutdown()
    assert process.terminated and parent.closed and not process.is_alive()
    assert ('close',None) not in parent.sent  # no waiting on a stuck native inference


def test_background_check_downloads_but_never_installs(qt,monkeypatch,tmp_path):
    monkeypatch.setattr(m,'_update_json',lambda:release())
    monkeypatch.setattr(m,'_download_update',lambda candidate,*a,**kw:tmp_path/candidate['name'])
    launch=Mock();monkeypatch.setattr(m,'_launch_update_helper',launch)
    updater=m.ReleaseUpdater(tmp_path);updater.deferred_version='1.2.3';updater.check(manual=True)
    updater._thread.join(2)
    assert not updater.busy and updater.candidate['version']=='1.2.3' and not updater.deferred_version
    assert 'requires your confirmation' in updater.status
    launch.assert_not_called();updater.shutdown()


def test_failed_check_never_offers_an_unverified_installer(qt,monkeypatch,tmp_path):
    monkeypatch.setattr(m,'_update_json',lambda:release())
    monkeypatch.setattr(m,'_download_update',Mock(side_effect=ValueError('hash mismatch')))
    updater=m.ReleaseUpdater(tmp_path);updater.check();updater._thread.join(2)
    assert updater.candidate is None and updater.installer is None and 'failed' in updater.status
    updater.shutdown()


def test_installed_directory_comes_from_existing_inno_registration(tmp_path,monkeypatch):
    folder=tmp_path/'old custom install';folder.mkdir();(folder/'Dictate.exe').write_bytes(b'MZ')
    registry=SimpleNamespace(HKEY_CURRENT_USER=1,HKEY_LOCAL_MACHINE=2,KEY_READ=1,KEY_WOW64_64KEY=2,
        OpenKey=Mock(),QueryValueEx=lambda key,name:(str(folder),1))
    registry.OpenKey.return_value.__enter__=Mock(return_value=object())
    registry.OpenKey.return_value.__exit__=Mock(return_value=False)
    monkeypatch.setitem(sys.modules,'winreg',registry);monkeypatch.setattr(m,'os',SimpleNamespace(name='nt'))
    assert m._installed_update_directory()==folder
    assert registry.OpenKey.call_args.args[1]==m._UNINSTALL_KEY
