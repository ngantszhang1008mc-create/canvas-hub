"""One explicitly authorized Basic sign-in continuation, with no live providers."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from canvas_hub.util import Conflict, InvalidInput
from canvas_hub.web.app import create_app
from .test_connected_analysis import Connections
from .test_external_sources import source
from .test_source_analysis import builtin, finish


def test_login_consent_single_dispatch_and_distinct_provenance(builtin, source, account, monkeypatch):
    service, _, calls = builtin
    from canvas_hub.assistant.providers import Turn
    class Provider:
        def complete(self, *args):
            calls.append(args)
            return Turn(json.dumps({'roles':['course_website'], 'confidence':.9, 'summary':'Fixture course page'}))
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider', lambda cfg: Provider())
    aid, sid = account['id'], source[1]
    connection = Connections(account)
    offer = service.login_disclosure(aid, sid)
    assert offer['access_mode'] == 'connected_local_authorized'
    assert offer['model'] == 'fixture-model' and not calls and not connection.calls
    with pytest.raises(InvalidInput):
        service.inspect_after_login(aid, sid, offer['token'], connection)
    with pytest.raises(Conflict):
        service.start(aid, sid, offer['token'], consent=True)
    assert not calls and not connection.calls
    result = service.inspect_after_login(aid, sid, offer['token'], connection, consent=True)
    assert result['started']
    finish(service)
    saved = source[0].get(aid, sid)
    assert saved['request']['status'] == 'done'
    assert saved['classification']['access_mode'] == 'connected_local_authorized'
    assert saved['reads'][0]['evidence'][0]['access_mode'] == 'connected_local_authorized'
    assert saved['records'] and not saved['adapters']
    assert len(calls) == 1
    assert 'NeverSendThis' not in json.dumps(calls) + json.dumps(saved)
    assert 'user-reviewed' not in json.dumps(calls)
    before = list(connection.calls)
    assert service.inspect_after_login(aid, sid, offer['token'], connection, consent=True)['started']
    finish(service)
    assert connection.calls == before and len(calls) == 1


@pytest.mark.parametrize('change', ['provider', 'source', 'identity', 'expired'])
def test_changed_disclosure_stops_before_read_or_model(builtin, source, account, monkeypatch, change):
    service, api, calls = builtin
    aid, sid = account['id'], source[1]
    connection = Connections(account)
    offer = service.login_disclosure(aid, sid)
    if change == 'provider':
        api.assistant.update_config({'model':'different-fixture-model'})
    elif change == 'source':
        with service.db.tx() as c:
            c.execute('UPDATE external_sources SET url=? WHERE id=?', ('https://other.example.edu/course', sid))
    elif change == 'identity':
        original = service.hub.identity.get_account
        monkeypatch.setattr(service.hub.identity, 'get_account', lambda a: {**original(a), 'canvas_user_id':'different-owner'})
    else:
        service._login_offers[offer['token']]['expires'] = 0
    with pytest.raises(Conflict):
        service.inspect_after_login(aid, sid, offer['token'], connection, consent=True)
    assert not calls and not connection.calls


def test_cancel_and_duplicate_while_verifying_never_dispatch(builtin, source, account):
    service, _, calls = builtin
    aid, sid = account['id'], source[1]
    entered, release = threading.Event(), threading.Event()
    class Slow(Connections):
        def action(self, *args):
            if args[-1] == 'authorized_snapshot':
                entered.set()
                assert release.wait(5)
            return super().action(*args)
    connection = Slow(account)
    offer = service.login_disclosure(aid, sid)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(service.inspect_after_login, aid, sid, offer['token'], connection, consent=True)
        try:
            assert entered.wait(3)
            with pytest.raises(Conflict):
                service.inspect_after_login(aid, sid, offer['token'], connection, consent=True)
            assert service.cancel_login_inspection(aid, sid, offer['token'])['state'] == 'cancelled'
        finally:
            release.set()
        result = pending.result(timeout=5)
    assert result['state'] == 'cancelled' and not result['started'] and not calls
    assert not source[0].get(aid, sid)['reads']


def test_invalid_or_changed_snapshot_and_expired_access_preserve_saved_data(builtin, source, account):
    service, _, calls = builtin
    aid, sid = account['id'], source[1]
    before = source[0].get(aid, sid)
    class Changed(Connections):
        def action(self, *args):
            result = super().action(*args)
            if args[-1] == 'analysis_snapshot':
                result['content'] += '\nChanged after the access check.'
            return result
    offer = service.login_disclosure(aid, sid)
    with pytest.raises(Conflict):
        service.inspect_after_login(aid, sid, offer['token'], Changed(account), consent=True)
    class Replaced(Connections):
        def action(self, *args):
            return {**super().action(*args), 'connection_id':args[-1]}
    with pytest.raises(Conflict):
        service.inspect_after_login(aid, sid, offer['token'], Replaced(account), consent=True)
    class Expired(Connections):
        def action(self, *args):
            raise Conflict('Sign-in did not make the requested page readable')
    with pytest.raises(Conflict):
        service.inspect_after_login(aid, sid, offer['token'], Expired(account), consent=True)
    assert not calls and source[0].get(aid, sid) == before


def test_internal_snapshot_is_not_an_http_action(builtin, source, account):
    _, api, calls = builtin
    app = create_app(api, token='fixture', port=8765)
    path = f'/api/accounts/{account["id"]}/external-sources/{source[1]}'
    with TestClient(app, base_url='http://127.0.0.1:8765', headers={'Authorization':'Bearer fixture','X-Canvas-Hub':'1'}) as client:
        for action in ('analysis_snapshot', 'authorized_snapshot'):
            assert client.post(path+'/site-connection/'+action, json={}).status_code == 400
        offer = client.get(path+'/login-inspection').json()
        assert client.post(path+'/login-inspection', json={'token':offer['token']}).status_code == 400
    assert not calls
