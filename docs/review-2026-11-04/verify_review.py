"""Offline review evidence. Uses temporary files, mocks and loopback HTTP only."""
from pathlib import Path
import json
import logging
import math
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import requests
import run as cli
from guet import cancel, scheduler, wizard
from guet.client import TronClassError
from guet.config import AppConfig, AccountConfig, RuntimeConfig, validate
from guet.gui import server as gui
from guet.runner import AccountRunner, TaskResult, VideoJob
from guet.ticker import run_ticks
from guet.util import shutdown_loggers
from tests.test_realtime import FakeRunner, FakeClock, PerActivityServer, Task

logging.disable(logging.CRITICAL)
evidence = {}

with tempfile.TemporaryDirectory() as raw:
    root = Path(raw)
    state = gui.AppState(root)
    state.ensure_files()
    state.add_account('acc01')
    server = gui.build_server(state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    http = requests.Session()
    http.trust_env = False
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        state.status = 'running'
        cancel.reset()
        response = http.get(base + '/api/stop', params={'t': state.token}, timeout=3)
        evidence['stop_GET'] = {'http': response.status_code, 'cancelled': cancel.requested()}
        assert response.status_code == 404 and not cancel.requested()
        response = http.post(base + '/api/stop', params={'t': state.token}, json={}, timeout=3)
        assert response.status_code == 200 and cancel.requested()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        http.close()
        cancel.reset()

    # A synchronous thread stub keeps the test deterministic and never starts real work.
    class InlineThread:
        def __init__(self, target, **kw): self.target = target
        def start(self): self.target()

    result = TaskResult(1, 2, 'material', 'sample', 'material', 'done', 100)
    def fake_run(app, accounts, on_result):
        runner = NS(state=NS(update_activity=lambda *a, **k: None), log=Mock(), on_result=on_result)
        AccountRunner._record(runner, result)
        return [scheduler.AccountOutcome('acc01', False, 'simulated failure')]

    state.status = 'idle'
    with patch.object(gui.threading, 'Thread', InlineThread), patch.object(scheduler, 'run_all', fake_run):
        gui.api_start(state, {'accounts': ['acc01'], 'courses': [1]})
    evidence['GUI_callback_and_outcomes'] = {'events': state.events, 'progress': state.progress,
                                           'error': state.error, 'status': state.status}
    assert not state.events and not state.error

    state.status = 'stopping'
    cancel.request()
    with patch.object(gui.threading, 'Thread'):
        gui.api_start(state, {'accounts': ['acc01'], 'courses': [1], 'target': 101, 'speed': 'invalid'})
    evidence['GUI_start_validation'] = {'target': state.app.runtime.target_percent,
                                       'speed': state.app.runtime.speed, 'status': state.status,
                                       'cancelled': cancel.requested()}
    assert state.app.runtime.target_percent == 101 and not cancel.requested()
    state.reload()
    state.status = 'running'
    with patch.object(gui.session, 'login', return_value={}):
        gui.api_login(state, {'name': 'acc01'})
    evidence['GUI_login_overwrites_run_status'] = state.status
    assert state.status == 'idle'
    state.add_account('bad name')
    evidence['GUI_invalid_account_persisted'] = {'configured': state.app is not None,
                                               'saved_invalid_name': 'bad name' in state.accounts_path.read_text('utf-8')}
    assert state.app is None

    app = AppConfig(root, RuntimeConfig(), [AccountConfig('acc01', course_ids=[1, 2])])
    fake_client = NS(check_session=lambda: True, my_completeness=lambda cid: {})
    with patch.object(scheduler, 'make_client', return_value=(fake_client, Mock())), \
         patch.object(AccountRunner, 'run_course', side_effect=[[result], TronClassError('second course fails')]):
        outcome = scheduler.run_one(app, app.accounts[0], scheduler.CourseCache(root/'cache.json'), None)
    evidence['earlier_course_results_lost'] = {'results': outcome.results, 'done': outcome.done, 'error': outcome.error}
    assert outcome.results == {}

    app.accounts = [AccountConfig('..')]
    validate(app)
    evidence['dotdot_account_profile'] = str(app.profile_path(app.accounts[0]).resolve().relative_to(root))
    assert app.profile_path(app.accounts[0]).resolve() == root
    app.accounts = [AccountConfig('acc01', per_account_jobs=8)]
    cli.apply_overrides(app, cli.build_parser().parse_args(['run', '--per-account-jobs', '1']))
    evidence['CLI_per_account_override'] = app.accounts[0].per_account_jobs or app.runtime.per_account_jobs
    assert evidence['CLI_per_account_override'] == 8
    cli.apply_overrides(app, cli.build_parser().parse_args(['run', '--rps', 'inf']))
    validate(app)
    evidence['CLI_nonfinite_rate_accepted'] = math.isinf(app.runtime.rps)
    assert evidence['CLI_nonfinite_rate_accepted']

    def fake_course_client(label):
        return NS(course_meta=lambda cid: {}, activities=lambda cid: [{'id': label}], modules=lambda cid: [])
    cache = scheduler.CourseCache(root/'scope-cache.json')
    cache.bundle(1, fake_course_client('site-or-subcourse-A'))
    reused = cache.bundle(1, fake_course_client('site-or-subcourse-B'))
    evidence['cache_scope'] = reused['activities']
    assert reused['activities'][0]['id'] == 'site-or-subcourse-A'
    # --yes still invokes interactive account selection.
    state.accounts_path.write_text('accounts:\n  - name: a\n  - name: b\n', encoding='utf-8')
    prompts = []
    with patch.object(wizard, 'browser_ready', return_value=(True,'fake')), \
         patch.object(wizard, 'ensure_sessions', return_value=[]), patch.object(wizard.report, 'info'):
        wizard.run_wizard(root, assume_yes=True, ask=lambda p: prompts.append(p) or '')
    evidence['wizard_yes_prompts'] = len(prompts)
    assert prompts
    # A missing explicitly named accounts file is replaced by the default file.
    loaded = []
    original_load = wizard.load_config
    def capture_load(*args):
        loaded.append(str(args[2]))
        return original_load(*args)
    with patch.object(wizard, 'browser_ready', return_value=(True,'fake')), \
         patch.object(wizard, 'ensure_sessions', return_value=[]), \
         patch.object(wizard, 'load_config', side_effect=capture_load), patch.object(wizard.report, 'info'):
        wizard.run_wizard(root, accounts_path=root/'missing-accounts.yaml', ask=lambda p: '')
    evidence['wizard_missing_accounts_fallback'] = Path(loaded[0]).name
    assert Path(loaded[0]).name == 'accounts.yaml'
    shutdown_loggers()

from guet.transport import HttpTransport
transport = HttpTransport(RuntimeConfig(), {'session':'FAKE_REVIEW_ONLY'})
prepared = transport.session.prepare_request(requests.Request('GET','https://unrelated.invalid/'))
evidence['cookie_sent_to_unrelated_domain'] = 'session=FAKE_REVIEW_ONLY' in prepared.headers.get('Cookie','')
assert evidence['cookie_sent_to_unrelated_domain']
transport.close()

# Syntax-check tracked Python sources without importing helper scripts or creating bytecode.
tracked = subprocess.check_output(['git','ls-files','*.py'],cwd=ROOT,text=True).splitlines()
for name in tracked:
    compile((ROOT/name).read_text(encoding='utf-8-sig'),name,'exec')
evidence['python_syntax_files'] = len(tracked)

clock = FakeClock()
runner = FakeRunner(PerActivityServer(300))
cancel.reset()
def stop_during_sleep(seconds):
    cancel.request()
    clock.sleep(seconds)
results = run_ticks(runner, [Task(1, 300)], 100, 100, 120,
                    clock=clock.now, sleep=stop_during_sleep, beat_seconds=0)
evidence['cancelled_tick'] = {'results': len(results), 'records': len(runner.records),
                             'server_ranges': runner.client.ranges, 'sleep_requested': clock.t}
assert not results and not runner.records and runner.client.ranges[1] == [[0,100]]
cancel.reset()

class InconsistentServer:
    def __init__(self): self.calls=[]
    def activity_read(self, *a):
        return {'completeness': 'part', 'data': {'completeness':99, 'ranges': [[0,300]]}}
    def report_video(self, aid, start, end, cid):
        self.calls.append((start,end))
        raise TronClassError('maximum is 125')
bad = InconsistentServer()
job = VideoJob(FakeRunner(bad), Task(1,300), 120, 100)
job.prepare()
for _ in range(8): job.step()
evidence['oversize_retry_loop'] = {'calls': bad.calls, 'failed': job.failed, 'stalled': job.stalled}
assert len(bad.calls) == 8 and set(bad.calls) == {(0,300)} and not job.failed and job.stalled == 0

# Refreshing a small hole then rewinding to its start replays already covered time.
server = PerActivityServer(1000)
server.ranges[1] = [[0,100], [200,800], [900,1000]]
runner = FakeRunner(server)
clock = FakeClock()
from guet.realtime import run_lanes
results = run_lanes(runner,[Task(1,1000)],100,100,1,clock=clock.now,sleep=clock.sleep,beat_seconds=0)
evidence['fragmented_resume'] = {'seconds_for_two_100_second_holes': clock.t, 'calls': server.calls}

suite = subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-q'],cwd=ROOT,
                       capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=60)
evidence['suite'] = {'exit': suite.returncode, 'summary': [l for l in suite.stderr.splitlines()
                     if l.startswith('Ran ') or l in ('OK','FAILED')]}
assert suite.returncode == 0, suite.stderr[-3000:]
out=Path(__file__).with_name('evidence.json')
out.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(evidence,ensure_ascii=False,indent=2))
