"""Deterministic local collection pipeline fixtures; no Canvas or browser access."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from canvas_hub.collector.client import Cancelled, TransientError
from canvas_hub.collector.traversal import CollectionRun, Package
from canvas_hub.util import NotFound


class FakeProvider:
    """Every worker gets its own client; only fixture control counters are shared."""
    def __init__(self, control, should_stop=lambda: False):
        self.control = control
        self.should_stop = should_stop
        self.owner = threading.get_ident()

    def background_client(self, should_stop):
        return FakeProvider(self.control, should_stop)

    def api_get(self, path):
        assert threading.get_ident() == self.owner
        fid = path.rsplit('/', 1)[-1]
        return {'id': fid, 'url': f'https://fixture.invalid/{fid}', 'display_name': f'{fid}.txt',
                'modified_at': '2026-01-01T00:00:00Z', 'size': 10, 'content-type': 'text/plain'}

    def download(self, url, max_bytes):
        assert threading.get_ident() == self.owner
        c = self.control
        fid = url.rsplit('/', 1)[-1]
        with c.lock:
            c.active += 1
            c.peak = max(c.active, c.peak)
            c.calls.append(fid)
        try:
            while not c.release.wait(.01):
                if self.should_stop():
                    raise Cancelled('fixture stopped')
            if fid in c.fail:
                raise TransientError('fixture failure')
            return (f'{c.prefix}:{fid}'.encode(), 'text/plain', url)
        finally:
            with c.lock:
                c.active -= 1


class Control:
    def __init__(self, *, released=False, fail=(), prefix='account-one'):
        self.release = threading.Event()
        if released:
            self.release.set()
        self.lock = threading.Lock()
        self.calls = []
        self.active = self.peak = 0
        self.fail = set(fail)
        self.prefix = prefix


def wait_for(predicate, timeout=5):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            pytest.fail('fixture condition did not become true')
        time.sleep(.01)


def build(hub, account, control, *, count=4, mode='baseline'):
    job = hub.jobs.create(account_id=account['id'], instance_id=account['instance_id'], kind='collect',
                          mode=mode, scope={**account['scope'], 'download_files': True})
    run = CollectionRun(hub, FakeProvider(control), job['id'])
    run.HANDLERS = {**run.HANDLERS, 'fixture_metadata': 'fixture_metadata'}
    def metadata(task):
        p = Package(run, task['task_key'])
        p.item('course', course_id='101', canvas_id='101', fields={'title': 'Metadata ready'}, title='Metadata ready')
        p.commit()
    run.fixture_metadata = metadata
    for fid in range(1, count+1):
        run.enqueue(f'download:{fid}', 'download', course_id='101', category='files', payload={'id': str(fid)})
    # Deliberately enqueue core AFTER slow attachments, reproducing the old FIFO blockage.
    run.enqueue('later-course', 'fixture_metadata', course_id='101')
    return run


@pytest.fixture
def fast_extraction(hub, monkeypatch):
    def extract(sha, filename, *, should_stop, **kwargs):
        if should_stop():
            raise Cancelled('fixture stopped')
        return hub.resources._save_extraction(sha, ('ok', None, 'synthetic extracted text', 'fixture', None))
    monkeypatch.setattr(hub.resources, 'extract_bounded', extract)


def test_synthetic_benchmark_metadata_passes_blocked_downloads_and_bounds(hub, account, fast_extraction, record_property):
    c = Control()
    run = build(hub, account, c, count=8)
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run.run)
        try:
            wait_for(lambda: hub.jobs.get(run.job_id)['stats'].get('core_status') == 'updated')
            metadata_seconds = time.monotonic() - start
            wait_for(lambda: len(c.calls) == 2)
            job = hub.jobs.get(run.job_id)
            assert not c.release.is_set() and not future.done()
            assert job['state'] == 'running'
            assert job['phases']['core']['done'] == 1
            assert job['phases']['downloads']['running'] == 2
            assert job['phases']['downloads']['pending'] == 6
            assert c.peak == 2
            assert hub.db.one('SELECT 1 FROM items WHERE account_id=? AND item_type=\'course\'', (account['id'],))
            record_property('metadata_ready_seconds', metadata_seconds)
            record_property('blocked_downloads_at_metadata_ready', 8)
            print(f'PIPELINE_BENCH metadata_ready={metadata_seconds:.4f}s; downloads completed=0/8; peak=2')
        finally:
            c.release.set()
        assert future.result(timeout=10) == 'completed'
    assert len(c.calls) == len(set(c.calls)) == 8
    assert hub.jobs.get(run.job_id)['phases']['processing']['done'] == 8
    assert not [t for t in threading.enumerate() if t.name.startswith(('canvas-download', 'canvas-document'))]


def test_download_failure_does_not_block_other_files_and_retry_is_deduplicated(hub, account, fast_extraction):
    c = Control(released=True, fail={'1'})
    run = build(hub, account, c, count=3)
    assert run.run() == 'completed_with_gaps'
    job = hub.jobs.get(run.job_id)
    assert job['phases']['core']['done'] == 1
    assert job['phases']['downloads']['failed'] == 1
    assert job['phases']['processing']['done'] == 2
    assert job['resumable']
    before = hub.db.one('SELECT COUNT(*) n FROM item_observations WHERE job_id=?', (run.job_id,))['n']
    c.fail.clear()
    hub.jobs.resume(run.job_id)
    CollectionRun(hub, FakeProvider(c), run.job_id).run()
    assert c.calls.count('1') == 2 and c.calls.count('2') == 1 and c.calls.count('3') == 1
    after = hub.db.one('SELECT COUNT(*) n FROM item_observations WHERE job_id=?', (run.job_id,))['n']
    assert after == before + 2
    assert hub.jobs.get(run.job_id)['phases']['downloads']['done'] == 3


def test_pause_preserves_pending_work_and_resume_does_not_duplicate_enqueue(hub, account, fast_extraction):
    c = Control()
    run = build(hub, account, c, count=3)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run.run)
        try:
            wait_for(lambda: len(c.calls) == 2)
            hub.jobs.set_state(run.job_id, 'paused')
            assert future.result(timeout=5) == 'paused'
        finally:
            c.release.set()
    tasks = hub.jobs.tasks(run.job_id)
    assert not any(t['state'] == 'running' for t in tasks)
    assert sum(t['state'] == 'pending' and t['kind'] == 'download' for t in tasks) == 3
    assert not run.enqueue('download:1', 'download', payload={'id': '1'})
    hub.jobs.resume(run.job_id)
    resumed = CollectionRun(hub, FakeProvider(c), run.job_id)
    resumed.HANDLERS = run.HANDLERS
    resumed.fixture_metadata = run.fixture_metadata
    resumed.run()
    assert hub.jobs.get(run.job_id)['phases']['downloads']['done'] == 3
    assert hub.db.one('SELECT COUNT(*) n FROM resources')['n'] == 3


def test_restart_resets_running_queue_rows(hub, account):
    run = build(hub, account, Control(released=True), count=1)
    hub.jobs.set_state(run.job_id, 'running')
    task = hub.jobs.next_task(run.job_id)
    hub.jobs.mark_running(task['id'])
    assert hub.jobs.recover_after_restart() == [run.job_id]
    assert hub.jobs.get(run.job_id)['state'] == 'interrupted'
    assert hub.jobs.next_task(run.job_id)['id'] == task['id']
    hub.jobs.resume(run.job_id)
    assert not run.enqueue('download:1', 'download', payload={'id': '1'})


def test_file_observations_and_resources_are_account_isolated(hub, account, fast_extraction):
    first = build(hub, account, Control(released=True), count=1)
    first.run()
    second_account = hub.identity.upsert_verified_account(account['instance_id'], {'canvas_user_id': '200002', 'display_name': 'Second'})
    second = build(hub, second_account, Control(released=True, prefix='account-two'), count=1)
    second.run()
    rows = hub.db.query("SELECT i.account_id, ir.resource_id FROM items i JOIN item_resources ir ON ir.item_id=i.id WHERE i.item_type='file'")
    assert len(rows) == 2 and rows[0]['resource_id'] != rows[1]['resource_id']
    for row in rows:
        other = second_account['id'] if row['account_id'] == account['id'] else account['id']
        with pytest.raises(NotFound):
            hub.resources.describe(other, row['resource_id'])


def test_baseline_skips_unchanged_file_but_thorough_revalidates(hub, account, fast_extraction):
    first = build(hub, account, Control(released=True), count=1)
    first.run()
    meta = FakeProvider(Control()).api_get('/api/v1/files/1')
    for mode, expected in [('baseline', 0), ('thorough', 1)]:
        run = build(hub, account, Control(released=True), count=0, mode=mode)
        run._maybe_download('101', meta)
        run._maybe_download('101', meta)
        assert len(hub.jobs.pending_tasks(run.job_id, kinds=['download'])) == expected
        hub.jobs.set_state(run.job_id, 'cancelled')


def test_slow_conversion_has_separate_bound_and_does_not_block_downloads(hub, account, monkeypatch):
    downloads = Control(released=True)
    conversion = Control()
    def extract(sha, name, *, should_stop):
        with conversion.lock:
            conversion.active += 1
            conversion.peak = max(conversion.peak, conversion.active)
            conversion.calls.append(name)
        try:
            while not conversion.release.wait(.01):
                if should_stop():
                    raise Cancelled('fixture stopped')
            if name == '1.txt':
                raise TimeoutError('fixture conversion timed out')
            return hub.resources._save_extraction(sha, ('ok', None, 'fixture text', 'fixture', None))
        finally:
            with conversion.lock:
                conversion.active -= 1
    monkeypatch.setattr(hub.resources, 'extract_bounded', extract)
    run = build(hub, account, downloads, count=6)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run.run)
        try:
            wait_for(lambda: hub.jobs.get(run.job_id)['phases']['downloads'].get('done') == 6)
            wait_for(lambda: len(conversion.calls) == 2)
            job = hub.jobs.get(run.job_id)
            assert job['stats']['core_status'] == 'updated'
            assert job['phases']['processing']['running'] == 2
            assert job['phases']['processing']['pending'] == 4
            assert conversion.peak == 2
            assert not future.done()
        finally:
            conversion.release.set()
        assert future.result(timeout=5) == 'completed_with_gaps'
    assert hub.jobs.get(run.job_id)['phases']['processing']['failed'] == 1
    assert hub.jobs.get(run.job_id)['phases']['processing']['done'] == 5
    assert len(downloads.calls) == 6


def test_explicit_cancel_stops_workers_and_retains_durable_pending_tasks(hub, account, fast_extraction):
    c = Control()
    run = build(hub, account, c, count=3)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run.run)
        try:
            wait_for(lambda: len(c.calls) == 2)
            hub.jobs.request_cancel(run.job_id)
            assert future.result(timeout=5) == 'cancelled'
        finally:
            c.release.set()
    assert c.active == 0
    job = hub.jobs.get(run.job_id)
    assert job['phases']['downloads']['pending'] == 3
    assert not job['phases']['downloads'].get('running')
    assert hub.db.one('SELECT COUNT(*) n FROM resources')['n'] == 0


def test_linked_file_changed_timestamp_queues_download_before_metadata_overwrite(hub, account, fast_extraction):
    first = build(hub, account, Control(released=True), count=1)
    first.run()
    run = build(hub, account, Control(released=True), count=0)
    meta = FakeProvider(Control()).api_get('/api/v1/files/1')
    meta['modified_at'] = '2026-02-01T00:00:00Z'
    run.client.api_get = lambda path: meta
    run.enqueue('fetch:file:1', 'fetch_file', course_id='101', payload={'id': '1'})
    task = hub.jobs.pending_tasks(run.job_id, kinds=['fetch_file'])[0]
    run.t_fetch_file(task)
    assert len(hub.jobs.pending_tasks(run.job_id, kinds=['download'])) == 1


@pytest.mark.parametrize('crash_before_import', [True, False])
def test_download_checkpoint_restart_does_not_repeat_network(hub, account, fast_extraction, monkeypatch, crash_before_import):
    control = Control(released=True)
    run = build(hub, account, control, count=1)
    metadata = hub.jobs.pending_tasks(run.job_id, kinds=['fixture_metadata'])[0]
    run.fixture_metadata(metadata)
    hub.jobs.finish_task(metadata['id'], 'done')
    hub.jobs.set_state(run.job_id, 'running')
    task = hub.jobs.pending_tasks(run.job_id, kinds=['download'])[0]
    hub.jobs.mark_running(task['id'])
    result = run.download_file(task, FakeProvider(control))
    original_import = hub.importer.import_package
    if crash_before_import:
        def interrupted_import(*args, **kwargs):
            raise RuntimeError('synthetic process interruption')
        monkeypatch.setattr(hub.importer, 'import_package', interrupted_import)
        with pytest.raises(RuntimeError, match='synthetic'):
            run.commit_download(task, result)
        monkeypatch.setattr(hub.importer, 'import_package', original_import)
    else:
        run.commit_download(task, result)
    # Simulate app restart after original storage but before task acknowledgment.
    import json
    payload = json.loads(hub.db.one('SELECT payload_json FROM job_tasks WHERE id=?', (task['id'],))[0])
    assert payload['artifact']['sha']
    assert 'url' not in payload['artifact']['meta']
    hub.jobs.recover_after_restart()
    hub.jobs.resume(run.job_id)
    client = FakeProvider(control)
    client.background_client = lambda should_stop: pytest.fail('download checkpoint unexpectedly repeated network')
    resumed = CollectionRun(hub, client, run.job_id)
    assert resumed.run() == 'completed'
    assert control.calls == ['1']
    assert hub.jobs.get(run.job_id)['phases']['downloads']['done'] == 1
    assert hub.jobs.get(run.job_id)['phases']['processing']['done'] == 1
    assert hub.db.one("SELECT COUNT(*) n FROM item_observations WHERE job_id=? AND observation_key LIKE '%download:1%'", (run.job_id,))['n'] == 1
    if crash_before_import:
        assert hub.jobs.get(run.job_id)['stats']['download_checkpoint_cache_hits'] == 1


def test_completed_conversion_text_is_searchable(hub, account, fast_extraction):
    run = build(hub, account, Control(released=True), count=1)
    assert run.run() == 'completed'
    hits = hub.search.search(account['id'], 'synthetic extracted text')['results']
    assert hits
    assert any(hit.get('resource_id') for hit in hits)


def test_identical_thorough_revalidation_does_not_toggle_extraction_status_or_add_events(hub, account, fast_extraction):
    first = build(hub, account, Control(released=True), count=1)
    assert first.run() == 'completed'
    events_before = hub.db.one('SELECT COUNT(*) n FROM change_events')['n']
    second = build(hub, account, Control(released=True), count=1, mode='thorough')
    assert second.run() == 'completed'
    assert second.stored_field('file', '1', 'extraction_status') == 'ok'
    assert hub.db.one('SELECT COUNT(*) n FROM change_events')['n'] == events_before
    assert hub.jobs.get(second.job_id)['stats']['extraction_cache_hits'] == 1


def test_conversion_retry_updates_current_status_and_replayed_result_is_idempotent(hub, account, monkeypatch):
    statuses = iter(['failed', 'ok'])
    def extraction(sha, filename, *, should_stop):
        status = next(statuses)
        return hub.resources._save_extraction(sha, (status, None, 'recovered searchable text' if status == 'ok' else '', 'fixture', None))
    monkeypatch.setattr(hub.resources, 'extract_bounded', extraction)
    control = Control(released=True)
    run = build(hub, account, control, count=1)
    assert run.run() == 'completed_with_gaps'
    assert run.stored_field('file', '1', 'extraction_status') == 'failed'
    observations_before = hub.db.one('SELECT COUNT(*) n FROM item_observations WHERE job_id=?', (run.job_id,))['n']
    hub.jobs.resume(run.job_id)
    resumed = CollectionRun(hub, FakeProvider(control), run.job_id)
    resumed.run()
    assert resumed.stored_field('file', '1', 'extraction_status') == 'ok'
    assert control.calls == ['1']
    assert hub.db.one('SELECT COUNT(*) n FROM item_observations WHERE job_id=?', (run.job_id,))['n'] == observations_before + 1
    assert hub.search.search(account['id'], 'recovered searchable text')['results']
    # Replaying the same acknowledged result must not duplicate history or events.
    import json
    row = dict(hub.db.one("SELECT * FROM job_tasks WHERE job_id=? AND kind='process_document'", (run.job_id,)))
    row['payload'] = json.loads(row['payload_json'])
    resource = dict(hub.db.one('SELECT * FROM resources WHERE id=?', (row['payload']['sha'],)))
    before = tuple(hub.db.one('SELECT (SELECT COUNT(*) FROM change_events), (SELECT COUNT(*) FROM item_observations)'))
    resumed.commit_document(row, resource)
    resumed.commit_document(row, resource)
    after = tuple(hub.db.one('SELECT (SELECT COUNT(*) FROM change_events), (SELECT COUNT(*) FROM item_observations)'))
    assert before == after
