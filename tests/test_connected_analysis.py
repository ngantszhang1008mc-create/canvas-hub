"""Authenticated source analysis with fixtures only; no live site or model calls."""
import json

import pytest

from canvas_hub.assistant.providers import Turn
from canvas_hub.services.connected_content import sanitize_connected_text
from canvas_hub.util import Conflict, InvalidInput, sha256_text
from .test_external_sources import source, URL
from .test_source_analysis import builtin, finish

COURSE = ('Course lecture notes and assignment dates for this semester. ' * 12 +
          '\nPassword: NeverSendThisPassword\nCookie: session=NeverSendThisCookie\n'
          'https://site.example/path?token=NeverSendThisToken\nhttps://site.example/public\ncontact person@example.edu')

class Connections:
    def __init__(self, account, text=COURSE): self.text=text; self.calls=[]; self.owner=sha256_text(account['origin']+"\0"+account['canvas_user_id'])
    def action(self,aid,sid,action):
        self.calls.append((aid,sid,action))
        return {'content':self.text,'url':URL,'owner':self.owner}


def test_scrub_is_bounded_and_removes_sensitive_forms_of_text():
    text=sanitize_connected_text(COURSE)
    for secret in ('NeverSendThisPassword','NeverSendThisCookie','NeverSendThisToken','person@example.edu'):
        assert secret not in text
    assert '[sensitive line removed]' in text and '[link removed]' in text
    with pytest.raises(InvalidInput): sanitize_connected_text('password: x')
    with pytest.raises(InvalidInput): sanitize_connected_text('x'*200001)


def test_connected_analysis_consent_redaction_provenance_and_source_snapshot(builtin,source,account,hub,monkeypatch):
    service,api,_=builtin;svc,sid,reads=source;aid=account['id'];calls=[]
    class Provider:
        def complete(self,system,transcript,tools):
            calls.append((system,transcript,tools))
            return Turn(json.dumps({'roles':['course_website','material'],'confidence':0.9,'summary':'untrusted'}))
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider',lambda cfg:Provider())
    tracker=hub.components.create(aid,'checklist',{'title':'Keep','sections':[{'id':'s','title':'Work','rows':[{'id':'r','label':'Manual'}]}]},course_id='4242')
    hub.components.set_state(aid,tracker['id'],{'done':{'r':True}})
    connection=Connections(account)
    offer=service.connected_disclosure(aid,sid,connection)
    assert offer['available'] and offer['access_mode']=='connected_local' and not calls and not reads
    with pytest.raises(InvalidInput): service.start(aid,sid,offer['token'],connected=True)
    with pytest.raises(Conflict): service.start(aid,sid,offer['token'],consent=True,reanalyze=True)
    assert not calls
    service.start(aid,sid,offer['token'],consent=True,reanalyze=True,connected=True)
    finish(service)
    s=svc.get(aid,sid)
    assert s['request']['status']=='done' and s['classification']['access_mode']=='connected_local_reviewed'
    assert s['reads'][0]['status']=='connected_read' and len(calls)==1 and not reads
    payload=json.dumps(calls)+json.dumps(s)
    for secret in ('NeverSendThisPassword','NeverSendThisCookie','NeverSendThisToken','person@example.edu'):
        assert secret not in payload
    assert 'course.example.edu/2026' not in json.dumps(calls)
    assert not s['adapters'] and s['records']
    item=hub.query.get_item(aid,s['records'][0]['id'])
    assert 'Course lecture notes' in json.dumps(item)
    assert 'NeverSendThis' not in json.dumps(item)
    assert svc.item_issue(aid,s['records'][0]['id']) is None
    updated=hub.components.configure_sources(aid,tracker['id'],{'review_sources':[{'item_id':s['records'][0]['id'],'fields':['body']}]})
    assert updated['source_config']['review_sources'][0]['item_id']==s['records'][0]['id']
    assert hub.components.get(aid,tracker['id'])['state']['done']['r'] is True

    connection.text=COURSE+'\nThe assignment schedule has changed.'
    review=service.connected_disclosure(aid,sid,connection)
    service.start(aid,sid,review['token'],consent=True,reanalyze=True,connected=True); finish(service)
    refreshed=hub.components.get(aid,tracker['id'])
    assert refreshed['source_status']['needs_review'] and refreshed['state']['done']['r'] is True
    s=svc.get(aid,sid); item=hub.query.get_item(aid,s['records'][0]['id'])
    assert 'schedule has changed' in json.dumps(item)

    class Failure:
        def complete(self,*args,**kwargs): raise RuntimeError('private provider detail')
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider',lambda cfg:Failure())
    prior=json.dumps(item); previous_classification=s['classification']
    retry=service.connected_disclosure(aid,sid,connection)
    service.start(aid,sid,retry['token'],consent=True,reanalyze=True,connected=True); finish(service)
    after=svc.get(aid,sid)
    assert after['classification']==previous_classification
    assert json.dumps(hub.query.get_item(aid,s['records'][0]['id']))==prior
    assert svc.item_issue(aid,s['records'][0]['id']) is None
    assert hub.components.get(aid,tracker['id'])['state']['done']['r'] is True
    assert 'private provider detail' not in json.dumps(after)


def test_changed_provider_or_expired_session_never_sends_or_replaces_old_data(builtin,source,account,monkeypatch):
    service,api,calls=builtin;svc,sid,_=source;aid=account['id']
    offer=service.connected_disclosure(aid,sid,Connections(account))
    api.assistant.update_config({'model':'changed-model'})
    with pytest.raises(Conflict): service.start(aid,sid,offer['token'],consent=True,reanalyze=True,connected=True)
    assert not calls and not svc.get(aid,sid)['records']
    class Expired:
        def action(self,*args): raise Conflict('Session expired; reconnect')
    with pytest.raises(Conflict): service.connected_disclosure(aid,sid,Expired())
    assert not calls and not svc.get(aid,sid)['records']


def test_changed_page_invalidates_exact_text_approval(builtin,source,account):
    service,api,calls=builtin;svc,sid,_=source;aid=account['id'];connection=Connections(account)
    offer=service.connected_disclosure(aid,sid,connection)
    connection.text=COURSE + '\nDifferent assignment information.'
    with pytest.raises(Conflict): service.start(aid,sid,offer['token'],consent=True,reanalyze=True,connected=True)
    assert not calls and not svc.get(aid,sid)['records']


@pytest.mark.parametrize('changed',['owner','url','session'])
def test_disclosure_rechecks_account_source_and_access_before_model(builtin,source,account,changed):
    service,api,calls=builtin;svc,sid,_=source;aid=account['id'];connection=Connections(account)
    offer=service.connected_disclosure(aid,sid,connection)
    original=connection.action
    def altered(*args):
        if changed=='session': raise Conflict('session expired')
        result=original(*args)
        result[changed]='different'
        return result
    connection.action=altered
    with pytest.raises(Conflict): service.start(aid,sid,offer['token'],consent=True,reanalyze=True,connected=True)
    assert not calls and not svc.get(aid,sid)['records']


def test_connected_analysis_refuses_public_adapter_proposal(builtin,source,account,monkeypatch):
    service,api,_=builtin;svc,sid,_=source;aid=account['id'];calls=[]
    class Provider:
        def complete(self,system,transcript,tools):
            calls.append(1)
            return Turn(json.dumps({'roles':['material'],'confidence':0.9,'summary':'x',
                                    'adapter_spec':{'version':1,'pages':['https://evil.example/'],'format':'text'}}))
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider',lambda cfg:Provider())
    offer=service.connected_disclosure(aid,sid,Connections(account))
    service.start(aid,sid,offer['token'],consent=True,reanalyze=True,connected=True); finish(service)
    result=svc.get(aid,sid)
    assert calls==[1] and not result['records'] and not result['adapters']
