"""One combined settings and context UI flow at desktop and phone widths."""
import os
from pathlib import Path

import pytest

from .test_allowance_gateway import gateway, setup, new_api_allowance, INVITE, SESSION, SUMMARY
from .test_components_ui import serve
from .test_assistant_context import conversation

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('width', [1280, 380])
def test_gateway_opt_in_and_context_flow(hub, account, gateway, width, tmp_path):
    from playwright.sync_api import expect, sync_playwright
    _, cid = conversation(hub, account)
    def response(request):
        system = request['body']['messages'][0]['content']
        text = SUMMARY if system.startswith('Summarize historical') else 'Synthetic answer'
        return gateway.events(request, text)
    gateway.action = response
    with serve(hub, []) as (base, token, api), sync_playwright() as pw:
        service, pid = setup(api, gateway, consent=False)
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={'width': width, 'height': 1000})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(f'{base}/?token={token}')
            page.goto(f'{base}/#/assistant/settings?p={pid}')
            card = page.locator('.card').filter(has=page.get_by_role('heading', name='Offline allowance', exact=True))
            check = card.get_by_role('button', name='Check allowance', exact=True)
            def check_allowance():
                # The tool count reflects the draft checkbox selection immediately.
                # Wait for the saved config and the complete allowance operation
                # before reading persisted settings or using the refreshed catalog.
                with page.expect_response(lambda response: response.url == base+'/api/assistant/config'
                                          and response.request.method == 'PUT') as saved:
                    check.click()
                assert saved.value.status == 200
                expect(check).to_be_enabled()
            expect(card.get_by_label('Automatically summarize older turns', exact=False)).not_to_be_checked()
            expect(card.get_by_role('button', name='Connect with invitation')).to_be_disabled()
            card.get_by_label('I allow selected conversation and course excerpts', exact=False).check()
            card.get_by_label('Invitation', exact=True).fill(INVITE)
            assert not gateway.enrollments and not gateway.requests
            card.get_by_role('button', name='Connect with invitation').click()
            expect(card).to_contain_text('Gateway connected', timeout=10000)
            expect(card.get_by_label('Invitation', exact=True)).to_have_value('')
            expect(page.locator('[data-setup-summary]').filter(has_text='mock-text')).to_contain_text('session saved')
            expect(card).to_contain_text('9000000 micro-USD')
            assert len(gateway.enrollments) == 1 and not gateway.requests
            gateway.tools = ['get_component_context', 'patch_component_state']
            check_allowance()
            expect(card).to_contain_text('Canvas gateway allowance')
            card.get_by_text('3 enabled tools · model limits', exact=True).click()
            stale = card.get_by_label('list_components (unavailable; uncheck to remove)', exact=True)
            expect(stale).to_be_checked()
            stale.uncheck()
            check_allowance()
            expect(card).to_contain_text('2 enabled tools · model limits')
            assert service._load(pid)['gateway_tools'] == gateway.tools
            assert not gateway.requests and SESSION not in page.content()
            artifact = Path(os.environ.get('CANVAS_HUB_NEXT_SCREENSHOTS', str(tmp_path / 'screenshots')))
            artifact.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(artifact / f'gateway-settings-{width}.png'), full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), page.evaluate('''() =>
                [...document.querySelectorAll('body *')].filter(el => el.getBoundingClientRect().right > innerWidth + 1)
                .slice(0, 15).map(el => ({tag:el.tagName, cls:el.className, width:el.getBoundingClientRect().width}))''')

            page.goto(f'{base}/#/assistant/{cid}')
            page.get_by_text('Context & usage', exact=True).click()
            page.get_by_role('button', name='Compact conversation', exact=True).click()
            expect(page.get_by_text('Older turns compacted through message', exact=False)).to_be_visible(timeout=10000)
            assert len(gateway.requests) == 1 and gateway.requests[0]['body']['max_output_tokens'] == 1024
            page.screenshot(path=str(artifact / f'gateway-context-{width}.png'), full_page=True)

            assert not errors, errors
        finally:
            browser.close()


def test_gateway_account_and_relay_configuration_display(hub, account, gateway, monkeypatch):
    from playwright.sync_api import expect, sync_playwright
    with serve(hub, []) as (base, token, api):
        service, pid = setup(api, gateway)
        uid = service._load(pid)['gateway_user_id']
        metadata = new_api_allowance(gateway, uid)
        monkeypatch.setattr(gateway, 'allowance', lambda: metadata)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page(viewport={'width': 380, 'height': 1000})
                page.goto(f'{base}/?token={token}')
                page.goto(f'{base}/#/assistant/settings?p={pid}')
                card = page.locator('.card').filter(has=page.get_by_role('heading', name='Offline allowance', exact=True))
                expect(card).to_contain_text(f'Canvas gateway account ID: {uid}')
                card.get_by_role('button', name='Check allowance', exact=True).click()
                expect(card).to_contain_text('Canvas gateway allowance')
                expect(card).to_contain_text('account link configured')
                expect(card).to_contain_text('upstream access and quota have not been checked')
                expect(card).to_contain_text('balance is not synchronized here')
                card.get_by_text('3 enabled tools · model limits', exact=True).click()
                expect(card).to_contain_text('maximum output cap (1024 tokens), the full conservative charge is 900 micro-USD')
                assert SESSION not in page.content() and not gateway.requests
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                page.reload()
                expect(card).to_contain_text('Check allowance to refresh its configuration')
                expect(card).not_to_contain_text('account link configured')
                metadata['models'][0]['new_api']['binding_status'] = 'unbound'
                card.get_by_role('button', name='Check allowance', exact=True).click()
                expect(card).to_contain_text('account link pending; contact your gateway operator')
                assert not gateway.requests
            finally:
                browser.close()
