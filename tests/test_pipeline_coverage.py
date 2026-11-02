"""Attachment completion must preserve inventory and exclusion evidence."""
from canvas_hub.collector.traversal import CollectionRun, Package


def run_for(hub, account):
    job = hub.jobs.create(account_id=account['id'], instance_id=account['instance_id'],
                         kind='collect', mode='thorough', scope=account['scope'], state='running')
    return CollectionRun(hub, None, job['id'])


def done_download(run, fid='1'):
    run.enqueue(f'download:{fid}', 'download', course_id='101', category='files', payload={'id': fid})
    task = run.hub.db.one('SELECT id FROM job_tasks WHERE job_id=? AND task_key=?', (run.job_id, f'download:{fid}'))
    run.hub.jobs.finish_task(task['id'], 'done')
    return task['id']


def coverage(run, category='files'):
    return dict(run.hub.db.one('SELECT * FROM coverage WHERE job_id=? AND course_canvas_id=? AND category=?',
                               (run.job_id, '101', category)))


def test_successful_download_does_not_erase_partial_inventory(hub, account):
    run = run_for(hub, account)
    inventory = {'status': 'partial', 'index': 'partial', 'body': 'not_applicable',
                 'expected': 8, 'collected': 5, 'reason': 'Folder listing incomplete', 'detail': {'folders': 2}}
    hub.jobs.update_stats(run.job_id, **{'file_inventory:101': inventory})
    done_download(run)
    assert run._finish() == 'completed_with_gaps'
    cov = coverage(run)
    assert cov['status'] == cov['index_depth'] == cov['attachment_depth'] == 'partial'
    assert cov['expected_count'] == 8 and cov['collected_count'] == 5
    assert cov['body_depth'] == 'not_applicable'
    assert cov['reason'] == 'Folder listing incomplete'


def test_attachment_failure_preserves_complete_inventory_and_retry_updates_summary(hub, account):
    run = run_for(hub, account)
    hub.jobs.update_stats(run.job_id, **{'file_inventory:101': {
        'status': 'complete', 'index': 'complete', 'body': 'not_applicable', 'expected': 1, 'collected': 1}})
    tid = done_download(run)
    hub.jobs.finish_task(tid, 'failed')
    # Simulate a per-file gap overwriting the shared coverage row.
    p = Package(run, 'download-gap')
    p.coverage('files', 'partial', course_id='101', index='partial', detail={'task': 'download:1'})
    p.commit()
    assert run._finish() == 'completed_with_gaps'
    assert coverage(run)['index_depth'] == 'complete'
    hub.jobs.set_state(run.job_id, 'running')
    hub.jobs.finish_task(tid, 'done')
    assert run._finish() == 'completed'
    cov = coverage(run)
    assert cov['status'] == cov['attachment_depth'] == cov['index_depth'] == 'complete'
    assert cov['expected_count'] == cov['collected_count'] == 1


def test_locked_oversized_and_gated_files_remain_visible_gaps(hub, account):
    run = run_for(hub, account)
    run.must_view['file:4'] = {'completed': False}
    files = [{'id': 2, 'size': 101 * 1024 * 1024}, {'id': 3, 'locked_for_user': True}, {'id': 4}]
    done_download(run)
    for file in files:
        run._maybe_download('101', file)
        run._maybe_download('101', file)
    tasks = hub.jobs.tasks(run.job_id)
    assert len(tasks) == 4
    assert [t['state'] for t in tasks] == ['done', 'skipped', 'blocked', 'skipped']
    assert all(t['attempts'] == 1 for t in tasks)
    assert all(t['last_error'] for t in tasks[1:])
    assert run._finish() == 'completed_with_gaps'
    assert coverage(run)['attachment_depth'] == 'partial'


def test_linked_metadata_success_does_not_claim_failed_attachment_complete(hub, account):
    run = run_for(hub, account)
    run.enqueue('fetch:file:1', 'fetch_file', course_id='101', category='linked_content', payload={'id': '1'})
    task = hub.jobs.next_task(run.job_id)
    hub.jobs.finish_task(task['id'], 'done')
    tid = done_download(run)
    hub.jobs.finish_task(tid, 'failed')
    run._linked_coverage()
    assert coverage(run, 'linked_content')['attachment_depth'] == 'partial'
    hub.jobs.finish_task(tid, 'done')
    run._linked_coverage()
    assert coverage(run, 'linked_content')['attachment_depth'] == 'complete'


def test_failed_inventory_task_not_hidden_by_successful_download(hub, account):
    run = run_for(hub, account)
    run.enqueue('files:101', 'files', course_id='101', category='files')
    task = hub.jobs.next_task(run.job_id)
    hub.jobs.finish_task(task['id'], 'failed')
    run._task_gap(task, 'partial', 'TransientError')
    done_download(run)
    assert run._finish() == 'completed_with_gaps'
    cov = coverage(run)
    assert cov['status'] == cov['index_depth'] == cov['attachment_depth'] == 'partial'


def test_linked_summary_preserves_view_gated_body_gap(hub, account):
    run = run_for(hub, account)
    run.enqueue('fetch:file:1', 'fetch_file', course_id='101', category='linked_content', payload={'id': '1'})
    task = hub.jobs.next_task(run.job_id)
    run._skip_view(task, {'title': 'File', 'module': 'Week 1', 'module_item_id': '10'})
    hub.jobs.finish_task(task['id'], 'done')
    run._linked_coverage()
    cov = coverage(run, 'linked_content')
    assert cov['status'] == cov['body_depth'] == 'partial'
