"""Local gateway contract + real Assistant loop, with synthetic credentials only."""
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from canvas_hub.assistant import AssistantService
from canvas_hub.assistant.gateway import AllowanceGatewayProvider, GatewayError, base_url
from canvas_hub.assistant.providers import Stopped
from canvas_hub.boundary import HubAPI
from canvas_hub.util import InvalidInput

SESSION = 'ses_' + 'S' * 43
INVITE = 'inv_' + 'I' * 43
SUMMARY = json.dumps({'goals': ['Keep the study checklist current.'], 'decisions': ['Use stored source evidence.'],
                      'open_questions': [], 'references': [], 'tool_outcomes': ['Earlier steps completed.']})


@pytest.fixture
def gateway():
    class Gateway:
        mode = 'mock'
        action = None
        slow_metadata = False
        pause = False
        tools = ['list_components', 'get_component_context', 'patch_component_state']
        def __init__(self):
            self.requests = []
            self.enrollments = []
            self.metadata = []
            self.entered = threading.Event()
            self.release = threading.Event()

        def allowance(self):
            window = {'limit_microusd': '9000000', 'spent_microusd': '0', 'held_microusd': '0',
                      'remaining_microusd': '9000000', 'resets_at': 1800000000000}
            return {'mode': self.mode, 'tools': self.tools, 'models': [{'id': 'mock-text', 'min_output_tokens': 1, 'max_output_tokens': 1024}],
                    'capabilities': {'protocol_version': 1, 'mode_header': 'X-Gateway-Mode', 'max_tools': 128, 'max_tool_calls_per_response': 32,
                                     'max_messages': 128, 'max_body_bytes': 65536},
                    'gateway_paused': self.pause, 'daily': window, 'monthly': window, 'reset_timezone': 'UTC'}

        def events(self, request, text='Synthetic response', calls=None, finish=None):
            rid = request['key']
            events = [{'type': 'meta', 'request_id': rid, 'reserved_microusd': '1000'}]
            if text:
                events += [{'type': 'text.delta', 'delta': text[:1]}, {'type': 'text.delta', 'delta': text[1:]}]
            for i, (name, args) in enumerate(calls or []):
                arg = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else args
                events += [{'type': 'tool_call.delta', 'index': i, 'id': 'call_' + uuid.uuid4().hex, 'name': name, 'arguments_delta': arg[:1]},
                           {'type': 'tool_call.delta', 'index': i, 'arguments_delta': arg[1:]}]
            events.append({'type': 'done', 'request_id': rid, 'status': 'completed', 'model': 'mock-text',
                           'usage': {'input_tokens': 100, 'output_tokens': 20, 'cached_input_tokens': 10, 'reasoning_tokens': 0},
                           'reserved_microusd': '1000', 'charged_microusd': '120',
                           'finish_reason': finish or ('tool_calls' if calls else 'stop')})
            return events

    g = Gateway()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def json(self, body, status=200):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            g.metadata.append(dict(self.headers))
            if g.slow_metadata:
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
                g.entered.set()
                try:
                    while not g.release.wait(.03):
                        self.wfile.write(b' '); self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self.json(g.allowance())

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            if self.path == '/v1/enroll':
                g.enrollments.append(data)
                assert data == {'invite': INVITE}
                self.json({'token': SESSION, 'expires_at': int((time.time() + 3600) * 1000),
                           'user_id': str(uuid.uuid4()), 'session_id': str(uuid.uuid4())}, 201)
                return
            request = {'key': self.headers.get('Idempotency-Key'), 'headers': dict(self.headers), 'body': data}
            g.requests.append(request)
            action = g.action(request) if g.action else g.events(request)
            if isinstance(action, dict):
                self.json(action['body'], action.get('status', 200))
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-ndjson')
            self.send_header('X-Request-ID', request['key'])
            self.end_headers()
            try:
                for event in action:
                    if event == 'wait':
                        g.entered.set(); g.release.wait(5); continue
                    self.wfile.write((json.dumps(event, ensure_ascii=False) + '\n').encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    g.url = f'http://127.0.0.1:{server.server_port}'
    try:
        yield g
    finally:
        g.release.set(); server.shutdown(); server.server_close(); thread.join(2)


def setup(api, gateway, *, consent=True):
    service = api.assistant = AssistantService(api.hub, api)
    profile = service.update_config({'profile_id': 'new', 'preset': 'allowance_gateway', 'name': 'Offline allowance',
                                    'base_url': gateway.url, 'model': 'mock-text', 'gateway_mode': 'mock', 'gateway_consent': consent})['saved_profile']
    if consent:
        service.gateway_enroll(profile, INVITE, consent=True)
    service.activate_profile(profile)
    return service, profile


def test_opt_in_private_session_metadata_probe_and_destination_change(hub, gateway, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'never-send-this-BYOK-key')
    api = HubAPI(hub)
    service, pid = setup(api, gateway, consent=False)
    with pytest.raises(InvalidInput):
        service.gateway_enroll(pid, INVITE, consent=True)
    assert not gateway.enrollments and not gateway.requests
    service.update_config({'profile_id': pid, 'gateway_consent': True})
    public = service.gateway_enroll(pid, INVITE, consent=True)
    assert public['ok'] and public['profiles'][-1]['gateway_session_saved']
    assert SESSION not in json.dumps(public) and INVITE not in service._path.read_text()
    assert service.test(pid)['ok'] and not gateway.requests
    with pytest.raises(InvalidInput):
        service.gateway_enroll(pid, INVITE, consent=True)
    assert len(gateway.enrollments) == 1
    assert all(h['Authorization'] == 'Bearer ' + SESSION for h in gateway.metadata)
    service.update_config({'profile_id': pid, 'base_url': 'http://127.0.0.1:1', 'gateway_consent': True})
    assert not service._load(pid).get('gateway_token') and not service.public_config()['configured']
    for url in ('http://127.0.0.1.evil.test', 'http://localhost.evil.test', 'https://u:p@example.test',
                'https://example.test/v1', 'https://example.test/?token=x'):
        with pytest.raises(GatewayError):
            base_url(url, 'mock')


def test_real_assistant_guarded_tracker_tool_rounds_and_history(hub, account, gateway):
    aid = account['id']
    component = hub.components.create(aid, 'checklist', {'title': 'Synthetic tracker', 'sections': [
        {'id': 's', 'title': 'Work', 'rows': [{'id': 'one', 'label': 'Read'}, {'id': 'two', 'label': 'Write'}]}]})
    hub.components.set_state(aid, component['id'], {'done': {'two': True}})
    service, _ = setup(HubAPI(hub), gateway)

    def answer(request):
        previous = [m for m in request['body']['messages'] if m['role'] == 'tool']
        if not previous:
            return gateway.events(request, '', [('get_component_context', {'component_id': component['id'], 'row_ids': ['one']})])
        state = json.loads(previous[-1]['content'])
        if 'computed_locally' in state and 'row_count' in state:
            return gateway.events(request, '', [('patch_component_state', {'component_id': component['id'],
                'expected_version': state['version'], 'state_token': state['state_token'], 'patch': {'done': {'one': True}}})])
        return gateway.events(request, '已更新 tracker。')

    gateway.action = answer
    chosen = ['get_component_context', 'patch_component_state']
    cid = service.send(aid, 'Finish the Read step without changing Write.',
                       tool_selection=['tool:' + name for name in chosen])['conversation_id']
    service.wait(cid, 10)
    result = service.get_conversation(aid, cid)
    assert result['conversation']['run_state'] == 'idle', result
    assert hub.components.get(aid, component['id'])['state']['done'] == {'one': True, 'two': True}
    assert len(gateway.requests) == 3 and len({r['key'] for r in gateway.requests}) == 3
    for request in gateway.requests:
        assert set(request['body']) == {'model', 'messages', 'max_output_tokens', 'tools'}
        assert request['body']['max_output_tokens'] == 1024
        assert set(request['body']['tools']) == set(chosen)
        assert SESSION not in json.dumps(request['body'])
        assert request['headers']['Authorization'] == 'Bearer ' + SESSION
        assert request['headers']['X-Gateway-Mode'] == 'mock'
    journal = list((hub.paths.runtime / 'gateway-attempts').glob('*.json'))
    assert len(journal) == 3
    assert all(json.loads(p.read_text())['state'] == 'completed' and 'body' not in json.loads(p.read_text()) for p in journal)
    assert result['context']['total_usage']['input_tokens'] == 300
    assert result['context']['total_usage']['cache_read_input_tokens'] == 30


@pytest.mark.parametrize('change', ['consent', 'session', 'tools', 'delete'])
def test_saved_gateway_revocation_blocks_an_existing_provider(hub, gateway, change):
    service, pid = setup(HubAPI(hub), gateway)
    provider = AllowanceGatewayProvider(service._load(pid))
    if change == 'delete':
        service.delete_profile(pid)
    else:
        changes = {'consent': {'gateway_consent': False}, 'session': {'clear_gateway_session': True},
                   'tools': {'gateway_tools': []}}
        service.update_config({'profile_id': pid, **changes[change]})
    with pytest.raises(GatewayError, match='authorization changed'):
        provider.complete('Synthetic', [{'role': 'user', 'sent_text': 'Read'}], [])
    assert not gateway.requests
    assert not list((hub.paths.runtime / 'gateway-attempts').glob('*.json'))


def test_gateway_revocation_interrupts_a_silent_inflight_request(hub, gateway):
    service, pid = setup(HubAPI(hub), gateway)
    provider = AllowanceGatewayProvider(service._load(pid))
    gateway.action = lambda req: [gateway.events(req)[0], 'wait']
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(provider.complete, 'Synthetic', [{'role': 'user', 'sent_text': 'Wait'}], [])
        assert gateway.entered.wait(2)
        # Metadata checks may hold this lock during network I/O. A generation
        # must still observe the saved revocation without blocking its loop.
        with service._cfg_lock:
            service.update_config({'profile_id': pid, 'gateway_consent': False})
            with pytest.raises(GatewayError, match='authorization changed'):
                pending.result(timeout=2)
    assert len(gateway.requests) == 1
    record = json.loads(next((hub.paths.runtime / 'gateway-attempts').glob('*.json')).read_text())
    assert record['state'] == 'uncertain' and record['request_id'] and record['body']


@pytest.mark.parametrize('boundary', ['before_tool', 'after_tool'])
def test_gateway_revocation_between_completed_rounds(hub, account, gateway, monkeypatch, boundary):
    import canvas_hub.assistant as runner
    service, pid = setup(HubAPI(hub), gateway)
    gateway.action = lambda req: gateway.events(req, '', [('list_components', {})])
    executed = []
    original_run, original_append = runner.run_tool_display, service._append

    def append(cid, role, data):
        result = original_append(cid, role, data)
        if boundary == 'before_tool' and role == 'assistant' and data.get('tool_calls'):
            service.update_config({'profile_id': pid, 'gateway_tools': []})
        return result

    def run(*args):
        executed.append(args[1])
        result = original_run(*args)
        if boundary == 'after_tool':
            service.delete_profile(pid)
        return result

    monkeypatch.setattr(service, '_append', append)
    monkeypatch.setattr(runner, 'run_tool_display', run)
    cid = service.send(account['id'], 'List my trackers.', tool_selection=['tool:list_components'])['conversation_id']
    service.wait(cid, 5)
    result = service.get_conversation(account['id'], cid)
    assert result['conversation']['run_state'] == 'error'
    assert 'authorization changed' in result['conversation']['run_error']
    assert len(gateway.requests) == 1
    assert executed == ([] if boundary == 'before_tool' else ['list_components'])
    transcript = service._transcript(cid)
    assert [m['role'] for m in transcript] == ['user', 'assistant', 'tool']
    assert transcript[-1]['results'][0]['is_error'] == (boundary == 'before_tool')


@pytest.mark.parametrize('failure', ['quota', 'replay', 'incomplete', 'truncated', 'oversized', 'wrong_tool', 'wrong_mode', 'bad_usage'])
def test_no_tool_execution_or_hidden_retry_after_gateway_failure(hub, gateway, failure):
    service, pid = setup(HubAPI(hub), gateway)
    if failure == 'wrong_mode':
        gateway.mode = 'production'
        with pytest.raises(GatewayError):
            AllowanceGatewayProvider(service._load(pid))
        assert not gateway.requests
        return
    provider = AllowanceGatewayProvider(service._load(pid))
    def answer(request):
        if failure == 'quota':
            return {'status': 429, 'body': {'error': 'user_daily_limit'}}
        if failure == 'replay':
            return {'body': {'request_id': request['key'], 'replay': True, 'output_replay_available': False, 'status': 'completed'}}
        events = gateway.events(request, '保留', [('list_components', {'text': 'x' * 17000} if failure == 'oversized' else {})],
                                finish='length' if failure == 'truncated' else None)
        if failure == 'wrong_tool':
            events[3]['name'] = 'delete_everything'
        if failure == 'bad_usage':
            events[-1]['usage']['cached_input_tokens'] = 101
        return events[:-1] if failure == 'incomplete' else events
    gateway.action = answer
    tools = [{'name': 'list_components'}]
    if failure == 'truncated':
        turn = provider.complete('Synthetic', [{'role': 'user', 'sent_text': 'Hello'}], tools)
        assert turn.stop == 'truncated' and not turn.tool_calls
    else:
        with pytest.raises(GatewayError):
            provider.complete('Synthetic', [{'role': 'user', 'sent_text': 'Hello'}], tools)
    assert len(gateway.requests) == 1
    assert all(SESSION not in p.read_text() for p in (hub.paths.runtime / 'gateway-attempts').glob('*.json'))


def test_cancel_silent_stream_retains_attempt_without_second_dispatch(hub, gateway):
    service, pid = setup(HubAPI(hub), gateway)
    provider = AllowanceGatewayProvider(service._load(pid))
    gateway.action = lambda req: [gateway.events(req)[0], 'wait']
    cancel = threading.Event()
    def on_text(_):
        if cancel.is_set():
            raise Stopped()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(provider.complete, 'Synthetic', [{'role': 'user', 'sent_text': 'Wait'}], [], on_text)
        assert gateway.entered.wait(2)
        cancel.set()
        with pytest.raises(Stopped):
            pending.result(timeout=2)
    assert len(gateway.requests) == 1
    record = json.loads(next((hub.paths.runtime / 'gateway-attempts').glob('*.json')).read_text())
    assert record['state'] == 'cancelled_unknown' and record['request_id'] and record['body']


def test_metadata_and_generation_have_absolute_deadlines(hub, gateway, monkeypatch):
    service, pid = setup(HubAPI(hub), gateway)
    monkeypatch.setattr('canvas_hub.assistant.gateway.METADATA_SECONDS', .2)
    gateway.slow_metadata = True
    before = time.monotonic()
    assert not service.test(pid)['ok']
    assert time.monotonic() - before < 2
    gateway.release.set(); gateway.slow_metadata = False
    provider = AllowanceGatewayProvider(service._load(pid))
    gateway.release.clear()
    gateway.action = lambda req: [gateway.events(req)[0], 'wait']
    monkeypatch.setattr('canvas_hub.assistant.gateway.GENERATION_SECONDS', .2)
    with pytest.raises(GatewayError):
        provider.complete('Synthetic', [{'role': 'user', 'sent_text': 'Wait'}], [])
    assert len(gateway.requests) == 1


def test_retry_history_and_compaction_error_do_not_expand_dispatch(hub, account, gateway):
    from .test_assistant_context import conversation
    from canvas_hub.assistant.context import Policy
    _, cid = conversation(hub, account)
    service, pid = setup(HubAPI(hub), gateway)
    cfg = service._load(pid)
    provider = AllowanceGatewayProvider(cfg)
    history = [{'role': 'user', 'sent_text': 'Read'}, {'role': 'assistant', 'text': '', 'tool_calls': [
        {'id': 'old_call', 'name': 'list_components', 'input': {}}]},
        {'role': 'tool', 'results': [{'id': 'old_call', 'name': 'list_components', 'content': '{}'}]},
        {'role': 'user', 'sent_text': 'Continue without tools.'}]
    provider.complete('Synthetic', history, [])
    assert gateway.requests[-1]['body']['tools'] == []
    assert gateway.requests[-1]['body']['messages'][2]['tool_calls'][0]['id'] == 'old_call'
    cfg = {**provider.cfg, 'context': {'auto_compact': True, 'auto_threshold_percent': 50}}
    transcript = service._transcript(cid)
    # Set only the near-limit decision; actual summary request/provider stays real.
    from unittest.mock import patch
    measure = Policy.measure
    def near(policy, *args):
        return {**measure(policy, *args), 'near_limit': True, 'over_budget': False}
    gateway.action = lambda req: {'status': 429, 'body': {'error': 'user_daily_limit'}}
    with patch.object(Policy, 'measure', near), pytest.raises(GatewayError):
        service._prepare_context(cid, account['id'], cfg, 'Synthetic', transcript, [], lambda c: AllowanceGatewayProvider(c))
    assert len(gateway.requests) == 2  # One retry answer and one failed summary; no fallback answer.


@pytest.mark.parametrize('failure', ['tool', 'trailing', 'cancel', 'journal'])
def test_reported_terminal_usage_survives_later_failure_and_resets_before_next_call(hub, gateway, monkeypatch, failure):
    service, pid = setup(HubAPI(hub), gateway)
    provider = AllowanceGatewayProvider(service._load(pid))
    def answer(request):
        events = gateway.events(request, '', [('list_components', 'not json' if failure == 'tool' else {})])
        if failure == 'trailing':
            events.append({'type': 'unexpected'})
        return events
    gateway.action = answer
    journal = provider._journal
    def save(record):
        if failure == 'journal' and record['state'] == 'completed':
            raise OSError('synthetic journal write failure')
        journal(record)
    monkeypatch.setattr(provider, '_journal', save)
    def on_text(_):
        if failure == 'cancel' and provider.last_usage is not None:
            raise Stopped()
    with pytest.raises((GatewayError, Stopped)):
        provider.complete('Synthetic', [{'role': 'user', 'sent_text': 'List trackers'}], [{'name': 'list_components'}], on_text)
    usage = provider.last_usage
    assert usage['input_tokens'] == 100 and usage['output_tokens'] == 20
    assert usage['final'] and usage['source'] == 'gateway' and usage['cache_read_input_tokens'] == 10
    service.update_config({'profile_id': pid, 'gateway_consent': False})
    with pytest.raises(GatewayError):
        provider.complete('Synthetic', [{'role': 'user', 'sent_text': 'Another request'}], [])
    assert provider.last_usage is None and len(gateway.requests) == 1


@pytest.mark.parametrize('terminal', ['reported', 'missing', 'invalid'])
def test_failed_gateway_attempt_counts_only_valid_terminal_usage_once(hub, account, gateway, monkeypatch, terminal):
    service, _ = setup(HubAPI(hub), gateway)
    def answer(request):
        events = gateway.events(request, '', [('list_components', {})])
        if terminal == 'missing':
            return events[:-1]
        if terminal == 'invalid':
            events[-1]['usage'].update(cached_input_tokens=60, cache_write_input_tokens=60)
        return [*events, {'type': 'unexpected'}]
    gateway.action = answer
    executed = []
    monkeypatch.setattr('canvas_hub.assistant.run_tool_display', lambda *args: executed.append(args))
    cid = service.send(account['id'], 'List trackers')['conversation_id']
    service.wait(cid, 5)
    result = service.get_conversation(account['id'], cid)
    total = result['context']['total_usage']
    assert result['conversation']['run_state'] == 'error' and not executed
    assert len(gateway.requests) == total['requests'] == 1
    assert total['input_tokens'] == (100 if terminal == 'reported' else 0)
    assert total['unreported_requests'] == (0 if terminal == 'reported' else 1)
