"""Synthetic, explicit widget examples. Call only in a fresh demo/test home."""
import copy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def seed_widget_examples(hub, package, *, now=None):
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo('Asia/Hong_Kong')).date().isoformat()
    package = copy.deepcopy(package)
    package['package_id'] = 'synthetic-widget-demo'
    for item in package['items']:
        item['observed_at'] = now.isoformat()
        if item['type'] == 'assignment':
            item['fields']['due_at'].update(iso=(now + timedelta(days=2)).isoformat(), raw='Synthetic deadline in two days')
    course = copy.deepcopy(next(i for i in package['items'] if i['type'] == 'course'))
    course.update(canvas_id='5555', course_id='5555', title='HMAW1905B · synthetic example')
    course['fields'].update(title=course['title'],course_code='HMAW1905B')
    package['items'].append(course)
    event = {'type':'calendar_event','canvas_id':'widget-seminar','course_id':'4242',
             'title':'Data seminar · synthetic example','url':'https://canvas.example.edu/calendar',
             'observed_at':now.isoformat(), 'fields':{'title':'Data seminar · synthetic example',
             'start_at':{'iso':now.isoformat(),'raw':'Synthetic event today','precision':'instant','tz_evidence':'offset_in_source','source':'canvas_api_iso8601'}}}
    package['items'].append(event)
    instance = hub.identity.add_instance(package['identity']['origin'])
    account = hub.identity.upsert_verified_account(instance['id'], {'canvas_user_id':'100001',
        'display_name':'Widget Demo · synthetic', 'time_zone':'Asia/Hong_Kong'})
    hub.importer.import_package(package)
    aid = account['id']
    eid = hub.db.one("SELECT id FROM items WHERE account_id=? AND canvas_id='widget-seminar'",(aid,))['id']
    iid = hub.db.one("SELECT id FROM items WHERE account_id=? AND canvas_id='7001'",(aid,))['id']
    daily = hub.components.create(aid,'checklist',{'title':'Today’s checklist', 'sections':[{'id':'today','rows':[
        {'id':'read','label':'Read course updates'}, {'id':'plan','label':'Choose one focused study block'},
        {'id':'pack','label':'Pack seminar notes'}]}]})
    seminar = hub.components.create(aid,'checklist',{'title':'Seminar essentials','sections':[{'id':'prepared','rows':[
        {'id':'question','label':'Bring your prepared question','note':'Synthetic prepared reminder: ask about the evaluation method.'},
        {'id':'notes','label':'Open the annotated reading'}]}]},course_id='4242')
    score = hub.components.create(aid,'score_model',{'title':'HMAW score calculator','threshold':60,'buckets':[
        {'id':'earned','label':'Completed activities','status':'earned','max':30,'earned':25},
        {'id':'reflection','label':'Reflection','status':'pending','max':30},
        {'id':'seminars','label':'Seminars','status':'pending','max':20,'gating':'all_or_nothing'},
        {'id':'service','label':'Service hours','status':'pending','max':20,'unit':'hours','full_at':20,'min_value':10,'gating':'min_threshold'}]},course_id='5555')
    hub.widgets.save(aid,f"component:{daily['id']}",{'mode':'pinned'},expected_version=0)
    hub.widgets.save(aid,f"component:{seminar['id']}",{'mode':'conditional','rule':{
        'kind':'event_today','item_id':eid,'time_zone':'Asia/Hong_Kong','max_age_days':7}},expected_version=0)
    hub.widgets.save(aid,f"component:{score['id']}",{'mode':'conditional','rule':{
        'kind':'date_window','start':today,'end':(now.astimezone(ZoneInfo('Asia/Hong_Kong')).date()+timedelta(days=7)).isoformat(),
        'time_zone':'Asia/Hong_Kong','reason':'Your chosen HMAW score planning week (synthetic)'}},expected_version=0)
    board=hub.widgets.board(aid)
    hub.widgets.reorder(aid,[f"component:{daily['id']}",f"component:{score['id']}",f"component:{seminar['id']}",'deadlines','continue','attention'],revision=board['revision'])
    return {'aid':aid,'daily':daily['id'],'seminar':seminar['id'],'score':score['id'],'event':eid,'assignment':iid}
