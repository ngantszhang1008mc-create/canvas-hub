"""The registry of links to sites outside Canvas: how they are found, told apart and kept safe.

Reading them is covered by ``test_link_inspection``.
"""
import copy
import json

import pytest

from canvas_hub.external_urls import clean_url
from canvas_hub.util import InvalidInput, NotFound

URL = 'https://course.example.edu/2026/assignments?term=fall'


@pytest.fixture
def source(hub, account, fixture_pkg):
    """One external link, found the way collection finds it: in the body of a Canvas item."""
    fixture_pkg['items'][2].setdefault('links',[]).append({'relation':'body_link','to':{
        'type':'external_link','course_id':'4242','url':URL,'title':'Course site'}})
    hub.importer.import_package(fixture_pkg)
    return hub.external_sources, hub.external_sources.list(account['id'])[0]['id']


def test_a_link_is_one_source_per_account_course_and_address(source,hub,account,fixture_pkg):
    svc,sid=source;aid=account['id']
    found=svc.get(aid,sid)
    assert (found['url'],found['title'],found['course_id'],found['status'])==(URL,'Course site','4242','unclassified')
    assert found['origins'] and not hub.requests.list(aid)          # finding a link asks nobody to read it
    other=hub.identity.upsert_verified_account(account['instance_id'],{'canvas_user_id':'other'})
    with pytest.raises(NotFound):svc.get(other['id'],sid)
    # A fragment or a tracking parameter is the same page; a different term is not.
    assert svc.register(aid,URL+'#section',course_id='4242')==sid
    assert svc.register(aid,URL+'&utm_source=canvas',course_id='4242')==sid
    assert svc.register(aid,URL.replace('fall','spring'),course_id='4242')!=sid
    assert clean_url(URL+'/')[0]!=clean_url(URL)[0]
    # The same address in another course is that course's own link.
    c=copy.deepcopy(fixture_pkg['items'][0]);c.update(canvas_id='888',course_id='888');c['fields']['title']='Second course'
    pkg=copy.deepcopy(fixture_pkg);pkg['package_id']='second-course';pkg['items']=[c];pkg['coverage']=[]
    hub.importer.import_package(pkg)
    assert svc.register(aid,URL,course_id='888')!=sid
    with pytest.raises(InvalidInput):svc.register(aid,URL,course_id='no-such-course')
    count=len(svc.list(aid));svc.backfill(aid);svc.backfill(aid)
    assert len(svc.list(aid))==count
    # Signed or secret query values are never stored.
    safe,secret=clean_url(URL+'&access_token=private-value&X-Amz-Signature=abc')
    assert safe==URL and secret
    svc.register(aid,URL+'&access_token=private-value',course_id='4242')
    assert 'private-value' not in json.dumps(svc.list(aid))


def test_unsafe_addresses_are_refused():
    for url in ('file:///etc/passwd','http://localhost/x','http://127.0.0.1/x','https://10.0.0.1/x','https://224.0.0.1/x',
                'https://[64:ff9b::7f00:1]/x','https://[::1]/x','https://user:pass@course.example.edu/x','https://foo.internal/x',
                'https://course.example.edu/logout','https://course.example.edu/%73ubmit','https://course.example.edu/a/../x',
                'https://course.example.edu/a%2f..%2fx','https://course.example.edu:9999/x','https://course.example.edu/x?action=delete'):
        with pytest.raises(InvalidInput):clean_url(url)


def test_external_url_identity_survives_canvas_discovery():
    from canvas_hub.urls import classify_link
    one=classify_link('https://course.example.edu/Term/?x=1&x=2&token=secret#part','https://canvas.example.edu')
    two=classify_link('https://course.example.edu/Term?x=2&x=1','https://canvas.example.edu')
    assert one.url=='https://course.example.edu/Term/?x=1&x=2'
    assert one.url!=two.url


def test_schema12_migration_preserves_existing_context_and_progress(tmp_path,monkeypatch,fixture_pkg):
    from canvas_hub import db
    from canvas_hub.services import Hub
    from canvas_hub.services import context_sources
    paths=tmp_path/'previous-store'
    migrations=db._migration_files
    monkeypatch.setattr(db,'_migration_files',lambda:[m for m in migrations() if m[0]<=12])
    old=Hub.open(paths)
    old.importer.after_discovery=None
    old.importer.after_changed_import=None  # Simulate the pre-dependency-index application.
    index_source=context_sources.index_source
    # Schema 12 used the original search index, before the schema 17 context
    # hook existed. Disable only that newer hook while seeding the old store.
    monkeypatch.setattr(context_sources,'index_source',lambda *args:None)
    fixture_pkg['items'][2].setdefault('links',[]).append({'relation':'body_link','to':{
        'type':'external_link','course_id':'4242','url':URL,'title':'Legacy course website'}})
    aid=old.importer.import_package(fixture_pkg,allow_new_account=True)['account_id']
    iid=old.db.one("SELECT id FROM items WHERE item_type='assignment' LIMIT 1")['id']
    cmp=old.components.create(aid,'checklist',{'sections':[{'id':'s','rows':[{'id':'r','label':'Keep my progress','item_id':iid}]}]},item_id=iid)
    old.components.set_state(aid,cmp['id'],{'done':{'r':True}})
    assert db.schema_version(old.db.conn)==12
    old.close()
    monkeypatch.setattr(db,'_migration_files',migrations)
    monkeypatch.setattr(context_sources,'index_source',index_source)
    new=Hub.open(paths)
    try:
        assert db.schema_version(new.db.conn)==max(version for version, *_ in migrations())
        assert new.components.get(aid,cmp['id'])['state']['done']['r']
        assert new.query.get_item(aid,iid)['course_canvas_id']=='4242'
        indexed=context_sources.ContextSources(new.db).catalog(aid,item_id=iid)
        assert indexed['sources'] and indexed['sources'][0]['version']
        assert new.db.one('SELECT COUNT(*) AS n FROM context_sources')['n'] > 0
        new.external_sources.backfill(aid)
        sources=new.external_sources.list(aid)
        assert len(sources)==1 and sources[0]['course_id']=='4242' and sources[0]['status']=='unclassified'
        assert not new.requests.list(aid)
        new.delete_account_data(aid,confirm='DELETE')
        assert not new.db.query('SELECT * FROM external_sources')
    finally:new.close()
