import errno
import threading
import subprocess
import sys
from pathlib import Path

import pytest

from canvas_hub.assistant import AssistantService
from canvas_hub.boundary import HubAPI
from canvas_hub.db import Database
from canvas_hub.util import HubError, InvalidInput
from canvas_hub.assistant.providers import ProviderError, ToolCall, Turn


def test_short_lived_threads_release_connections_and_run_commit_hooks(tmp_path):
    db = Database(tmp_path / 'test.sqlite3')
    baseline = len(db._all)
    calls = []
    def work():
        with db.tx():
            with db.tx():
                db.after_commit(lambda: calls.append('committed'))
    for _ in range(100):
        thread = threading.Thread(target=work)
        thread.start()
        thread.join()
    assert len(calls) == 100
    assert len(db._all) == baseline
    db.close()


def test_settings_read_error_does_not_erase_saved_configuration(hub, monkeypatch):
    svc = AssistantService(hub, HubAPI(hub))
    svc.update_config({'preset': 'codex'})
    original = svc._path.read_bytes()
    real = Path.read_text
    def fail(path, *args, **kwargs):
        if path == svc._path:
            raise OSError(errno.EMFILE, 'too many files')
        return real(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', fail)
    with pytest.raises(HubError, match='could not be read') as exc:
        svc.update_config({'name': 'changed'})
    assert exc.value.code == 'assistant_settings_unavailable'
    assert svc._path.read_bytes() == original


@pytest.mark.skipif(sys.platform == 'win32', reason='resource limits are Unix-specific')
def test_limit_uplift_is_bounded_in_child_process():
    code = '''
import resource
from canvas_hub.resource_limits import configure_nofile
_, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
if hard != resource.RLIM_INFINITY and hard < 128:
    raise SystemExit(77)
resource.setrlimit(resource.RLIMIT_NOFILE, (64, hard))
configure_nofile()
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
assert soft == (1024 if hard == resource.RLIM_INFINITY else min(1024, hard))
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    if result.returncode == 77:
        pytest.skip('hard descriptor limit too low for isolated test')
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not Path('/proc/self/fd').is_dir(), reason='requires Linux descriptor accounting')
def test_database_worker_descriptors_stay_bounded_in_child(tmp_path):
    code = '''
import os, sys, threading
from canvas_hub.db import Database
db = Database(sys.argv[1])
count = lambda: len(os.listdir('/proc/self/fd'))
before = count()
def work():
    db.one('SELECT 1')
for _ in range(120):
    t = threading.Thread(target=work)
    t.start()
    t.join()
assert count() <= before + 2, (before, count())
db.close()
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path / 'fd.sqlite3')], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_failed_turn_preserves_partial_and_conversation(hub, account, monkeypatch):
    class FailingProvider:
        def complete(self, system, transcript, tools, on_text):
            on_text('A partial answer')
            raise ProviderError('Temporary failure', retryable=True)
        def close(self):
            pass
    svc = AssistantService(hub, HubAPI(hub))
    svc.update_config({'preset': 'codex'})
    monkeypatch.setattr('canvas_hub.assistant.make_provider', lambda cfg: FailingProvider())
    cid = svc.send(account['id'], 'Question')['conversation_id']
    svc.wait(cid, 5)
    result = svc.get_conversation(account['id'], cid)
    assert result['conversation']['run_state'] == 'error'
    assert [m['role'] for m in result['messages']] == ['user', 'assistant']
    assert result['messages'][-1]['text'] == 'A partial answer'
    class SuccessProvider:
        def complete(self, system, transcript, tools, on_text):
            assert tools == []
            return Turn('Finished')
        def close(self):
            pass
    monkeypatch.setattr('canvas_hub.assistant.make_provider', lambda cfg: SuccessProvider())
    svc.retry(account['id'], cid)
    svc.wait(cid, 5)
    result = svc.get_conversation(account['id'], cid)
    assert result['conversation']['run_state'] == 'idle'
    assert [m['role'] for m in result['messages']].count('user') == 1
    assert result['messages'][-1]['text'] == 'Finished'


def test_admission_rejects_before_inserting_message(hub, account):
    svc = AssistantService(hub, HubAPI(hub))
    svc.update_config({'preset': 'codex'})
    svc._threads.update({i: object() for i in range(4)})
    with pytest.raises(InvalidInput) as exc:
        svc.send(account['id'], 'Do not add this')
    assert exc.value.code == 'assistant_busy'
    assert svc.list_conversations(account['id']) == []


def test_retry_never_executes_unexpected_tools(hub, account, monkeypatch):
    class Provider:
        def complete(self, system, transcript, tools, on_text):
            if tools:
                raise ProviderError('interrupted')
            return Turn('', stop='tool_use', tool_calls=[ToolCall('c1', 'create_note', {})])
        def close(self):
            pass
    svc = AssistantService(hub, HubAPI(hub))
    svc.update_config({'preset': 'codex'})
    monkeypatch.setattr('canvas_hub.assistant.make_provider', lambda cfg: Provider())
    monkeypatch.setattr('canvas_hub.assistant.run_tool_display', lambda *a: pytest.fail('tool executed'))
    cid = svc.send(account['id'], 'Question')['conversation_id']
    svc.wait(cid, 5)
    svc.retry(account['id'], cid)
    svc.wait(cid, 5)
    result = svc.get_conversation(account['id'], cid)
    assert result['conversation']['run_state'] == 'error'
    assert result['messages'][-1]['results'][0]['is_error']
