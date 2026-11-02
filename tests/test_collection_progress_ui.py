"""Local browser QA for truthful phase progress; never contacts Canvas."""
import json
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from canvas_hub.services.identity import normalize_scope
from canvas_hub.util import InvalidInput
from .test_components_ui import serve


def test_experimental_concurrency_is_opt_in():
    assert normalize_scope(None)['experimental_network_concurrency'] is False
    assert normalize_scope({'experimental_network_concurrency': True})['experimental_network_concurrency'] is True
    with pytest.raises(InvalidInput):
        normalize_scope({'experimental_network_concurrency': 'yes'})


@pytest.mark.browser
@pytest.mark.parametrize('width', [1280, 380])
def test_phase_progress_and_scope_settings(hub, account, width, tmp_path):
    job = hub.jobs.create(account_id=account['id'], instance_id=account['instance_id'],
                          kind='collect', mode='baseline', scope=account['scope'])
    job.update(state='running', cancellable=True, resumable=False,
               message='Core metadata updated; attachments are still pending.',
               stats={'core_status': 'updated', 'core_seconds': 0.125},
               phases={
                   'core': {'total': 12, 'done': 12},
                   'downloads': {'total': 4, 'done': 1, 'pending': 1, 'running': 1, 'failed': 1},
                   'processing': {'total': 1, 'pending': 1},
                   'verification': {'total': 1, 'pending': 1},
               })
    with serve(hub, []) as (base, token, _), sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': width, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/api/accounts/*/jobs?*', lambda route: route.fulfill(
            content_type='application/json', body=json.dumps({'jobs': [job], 'next_cursor': None})))
        page.goto(f'{base}/?token={token}#/collect')
        card = page.locator(f'.collection-job[data-job-id="{job["id"]}"]')
        expect(card).to_contain_text('Core data updated')
        expect(card.locator('[data-phase="downloads"]')).to_contain_text('1 failed')
        expect(card.locator('[data-phase="downloads"]')).to_contain_text('1 pending')
        expect(card.locator('[data-phase="processing"]')).to_contain_text('1 pending')
        expect(card.locator('[data-phase="verification"]')).to_contain_text('1 pending')
        expect(card.get_by_role('meter')).to_have_count(4)
        assert '100%' not in card.inner_text()
        expect(card.get_by_role('button', name='Cancel', exact=True)).to_be_visible()
        expect(card.get_by_role('button', name='Pause', exact=True)).to_be_visible()
        toggle = page.get_by_role('checkbox', name='Concurrent Canvas requests (Experimental)', exact=True)
        expect(toggle).not_to_be_checked()
        toggle.check()
        page.get_by_role('button', name='Save scope', exact=True).click()
        expect(page.get_by_text('Scope saved', exact=True)).to_be_visible()
        assert hub.identity.get_account(account['id'])['scope']['experimental_network_concurrency'] is True
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        screenshots = Path('/tmp/collection-pipeline-qa')
        screenshots.mkdir(exist_ok=True)
        page.evaluate("scrollTo(0,0)")
        page.screenshot(path=str(screenshots / f'collection-phases-{width}.png'), full_page=True)
        job.update(state='completed_with_gaps', resumable=True, cancellable=False)
        job['stats']['core_status'] = 'gaps'
        expect(card.get_by_role('button', name='Retry failed tasks', exact=True)).to_be_visible()
        expect(card).to_contain_text('Core collection finished with gaps')
        expect(card.get_by_role('button', name='Cancel', exact=True)).to_have_count(0)
        card.get_by_role('link', name='Open workspace', exact=True).click()
        expect(page).to_have_url(f'{base}/#/overall')
        assert not errors, errors
        browser.close()
