"""Reproducible integration fixtures. Only imported by tests and the demo launcher.

All reads are in-process fixtures. Unknown URLs fail closed; no network fallback.
"""
import copy
from datetime import datetime, timedelta, timezone

from ..services.external_reader import PublicReader, Read
from ..util import InvalidInput, now_iso
from .widget_demo import seed_widget_examples

SOURCE_URL = 'https://course.example.edu/synthetic/hmaw/assignments'
SPEC = {'version': 1, 'pages': [SOURCE_URL], 'format': 'html', 'records': {
    'selector': 'article.assignment', 'kind': 'assignment', 'fields': {
        'id': {'selector': ':self', 'attribute': 'data-id'}, 'title': {'selector': 'h2'},
        'due_at': {'selector': 'time', 'attribute': 'datetime'},
        'points_possible': {'selector': '.points'}, 'body': {'selector': 'p'}}},
    'limitations': 'Synthetic public-page fixture only. No login, real website, submission or grade access.'}


class SyntheticCourseSource:
    scenarios = ('current', 'updated', 'unavailable', 'template_changed', 'record_removed')

    def __init__(self, now=None):
        self.now = now or datetime.now(timezone.utc)
        self.scenario = 'current'
        self.calls = []

    def select(self, scenario):
        if scenario not in self.scenarios:
            raise InvalidInput('Choose a documented synthetic source scenario')
        self.scenario = scenario

    def html(self):
        updated = self.scenario == 'updated'
        due = (self.now + timedelta(days=4 if updated else 2)).isoformat()
        reflection = '' if self.scenario == 'record_removed' else (
            f'<article class="assignment" data-id="reflection"><h2>Reflection{" revised" if updated else ""} · synthetic</h2>'
            f'<time datetime="{due}">Explicit fixture deadline</time><span class="points">{40 if updated else 30}</span>'
            '<p>Prepare a short reflection. This is synthetic course content.</p></article>')
        return ('<html><body>' + reflection + '<article class="assignment" data-id="seminar">'
                f'<h2>Seminar notes · synthetic</h2><time datetime="{due}">Fixture deadline</time>'
                '<span class="points">10</span><p>Keep your prepared notes.</p></article></body></html>').encode()

    def __call__(self, url):
        if url != SOURCE_URL:
            raise InvalidInput('The synthetic preview never fetches other URLs')
        self.calls.append({'url': url, 'scenario': self.scenario})
        if self.scenario == 'unavailable':
            return Read(url, 503, 'text/plain', b'Synthetic source unavailable')
        if self.scenario == 'template_changed':
            return Read(url, 200, 'text/html', b'<p>Synthetic page structure changed</p>')
        return Read(url, 200, 'text/html', self.html())


def propose_fixture_adapter(hub, aid, sid, transport, *, reanalyze=False):
    source = hub.external_sources.analyze(aid, sid, reanalyze=reanalyze)
    request = source['analysis_request_id']
    agent = 'Synthetic connected source agent'
    hub.requests.claim(aid, request, agent)
    return hub.external_sources.submit_analysis(aid, request, agent, {
        'roles': ['course_website', 'material'], 'confidence': 1.0,
        'evidence_url': SOURCE_URL, 'read_at': now_iso(), 'summary': 'Synthetic HMAW assignment page',
        'content': transport.html().decode(), 'adapter_spec': copy.deepcopy(SPEC)})


def seed_integration_examples(hub, package, *, now=None):
    """Fresh demo/test homes only; all synthetic approvals are recorded normally."""
    now = now or datetime.now(timezone.utc)
    seed = seed_widget_examples(hub, package, now=now)
    aid = seed['aid']
    hub.db.conn.execute('UPDATE accounts SET display_name=? WHERE id=?', ('Integration Demo · synthetic', aid))
    transport = SyntheticCourseSource(now)
    hub.external_sources.reader = PublicReader(transport)
    # The ordinary importer discovers this link without an Agent or public read.
    pkg = copy.deepcopy(package)
    pkg.update(package_id='synthetic-integration-source', generated_at=now.isoformat(), coverage=[])
    pkg['items'] = [{'type': 'page', 'canvas_id': 'integration-source-page', 'course_id': '5555',
        'title': 'HMAW course website · synthetic', 'observed_at': now.isoformat(),
        'url': 'https://canvas.example.edu/courses/5555/pages/course-website',
        'fields': {'title': 'HMAW course website · synthetic'},
        'links': [{'relation': 'body_link', 'to': {'type': 'external_link', 'url': SOURCE_URL,
                   'course_id': '5555', 'title': 'HMAW public course page · synthetic'}}]}]
    hub.importer.import_package(pkg)
    sid = hub.external_sources.list(aid, course_id='5555')[0]['id']
    proposal = propose_fixture_adapter(hub, aid, sid, transport)['adapters'][0]
    source = hub.external_sources.decide(aid, sid, proposal['revision'], proposal['spec_hash'], approve=True)
    iid = next(r['id'] for r in source['records'] if r['title'] == 'Reflection · synthetic')
    hub.components.configure_sources(aid, seed['score'], {'bindings': [
        {'target': 'buckets/reflection/' + target, 'item_id': iid, 'field': field}
        for target, field in [('label', 'title'), ('max', 'points_possible'), ('due', 'due_at')]]})
    hub.components.set_state(aid, seed['score'], {'values': {'reflection': 18}, 'include': {'seminars': False}})
    checklist = hub.components.create(aid, 'checklist', {'title': 'Course website checklist', 'sections': [
        {'id': 'work', 'rows': [{'id': 'outline', 'label': 'Prepare an outline'},
         {'id': 'reflection', 'label': 'Reflection', 'item_id': iid}]}]}, item_id=iid,
        sources={'bindings': [{'target': 'sections/work/rows/reflection/' + target, 'item_id': iid, 'field': field}
                              for target, field in [('label', 'title'), ('due', 'due_at')]]})
    hub.components.set_state(aid, checklist['id'], {'done': {'outline': True}})
    hub.widgets.save(aid, f"component:{checklist['id']}", {'mode': 'pinned'}, expected_version=0)
    board = hub.widgets.board(aid)
    score = next(w for w in board['widgets'] if w['key'] == f"component:{seed['score']}")
    rule = {**score['rule'], 'item_id': iid, 'max_age_days': 7}
    hub.widgets.save(aid, score['key'], {'mode': 'conditional', 'rule': rule}, expected_version=score['version'])
    hub.workspace.save(aid, {'item_id': iid, 'component_id': checklist['id'], 'step_id': 'reflection',
                            'next_action': 'Draft your reflection from the saved course instructions'})
    board = hub.widgets.board(aid)
    order = [f"component:{checklist['id']}", f"component:{seed['score']}", f"component:{seed['daily']}",
             f"component:{seed['seminar']}", 'deadlines', 'continue', 'attention']
    hub.widgets.reorder(aid, order, revision=board['revision'])
    return {**seed, 'source': sid, 'external_item': iid, 'external_checklist': checklist['id']}, transport
