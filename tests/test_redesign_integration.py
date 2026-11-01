"""Public boundary and imported observations use the same persistent workspace data."""
import copy
import json

import pytest
from canvas_hub.agent_tools import ToolContext, run_tool_display
from canvas_hub.boundary import HubAPI
from canvas_hub.util import Conflict
from .conftest import variant


def test_import_refreshes_bound_source_without_ai_and_retains_override(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    aid = account['id']
    iid = hub.db.one("SELECT id FROM items WHERE item_type='assignment' AND canvas_id='7001'")['id']
    view = hub.components.create(aid,'score_model',{'buckets':[{'id':'p','label':'Proposal','max':1}]},
        sources={'bindings':[{'target':'buckets/p/max','item_id':iid,'field':'points_possible'}]})
    hub.components.set_state(aid,view['id'],{'values':{'p':6}})
    changed = variant(fixture_pkg,'source-change-hook','2026-11-20T00:00:00Z')
    next(i for i in changed['items'] if i['type']=='assignment' and i['canvas_id']=='7001')['fields']['points_possible']=20
    hub.importer.import_package(changed)
    result=hub.components.get(aid,view['id'])
    assert result['spec']['buckets'][0]['max']==20 and result['state']['values']['p']==6
    hub.components.set_source_override(aid,view['id'],'buckets/p/max',30)
    changed=variant(changed,'source-change-again','2026-11-21T00:00:00Z')
    next(i for i in changed['items'] if i['type']=='assignment' and i['canvas_id']=='7001')['fields']['points_possible']=25
    hub.importer.import_package(changed)
    assert hub.components.get(aid,view['id'])['spec']['buckets'][0]['max']==30
    assert hub.components.set_source_override(aid,view['id'],'buckets/p/max',follow_source=True)['spec']['buckets'][0]['max']==25


def test_scoped_creation_reuses_component_and_rejects_wrong_item(hub,account,fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    aid=account['id'];api=HubAPI(hub)
    iid=hub.db.one("SELECT id FROM items WHERE item_type='assignment' AND canvas_id='7001'")['id']
    ctx=ToolContext(api=api,account_id=aid,scope_course_id='4242',scope_item_id=iid)
    args={'sections':[{'id':'s','rows':[{'id':'r','label':'Read'}]}]}
    first=run_tool_display(ctx,'show_checklist',args,{'ui'})
    second=run_tool_display(ctx,'show_checklist',args,{'ui'})
    assert not first[1] and not second[1]
    assert first[2]['component_id']==second[2]['component_id']
    assert first[2]['view']['item_id']==iid
    assert run_tool_display(ctx,'show_checklist',{**args,'item_id':999999},{'ui'})[1]
    assert run_tool_display(ctx,'show_checklist',{**args,'course_id':'wrong'},{'ui'})[1]
    cid=first[2]['component_id']
    proposed=run_tool_display(ctx,'update_component',{'component_id':cid,'spec':{**args,'title':'Revised'}},{'ui'})
    assert not proposed[1] and proposed[2]['view']['pending_spec']
    assert hub.components.get(aid,cid)['title']!='Revised'
    api.approve_component_spec(aid,cid,{'expected_version':proposed[2]['view']['version']})
    assert hub.components.get(aid,cid)['title']=='Revised'


def test_grading_proposal_cannot_overwrite_new_manual_rules(hub,account,fixture_pkg):
    hub.importer.import_package(fixture_pkg);api=HubAPI(hub);aid=account['id']
    proposal=api.propose_grading_rules(aid,'4242',{'threshold':60})
    hub.grading.set(aid,'4242',{'threshold':70})
    with pytest.raises(Conflict):
        api.accept_grading_proposal(aid,'4242',proposal['proposal']['base_hash'])
    assert hub.grading.get(aid,'4242')['threshold']==70


def test_scoped_interpretation_records_evidence_even_if_provider_omits_recipe(hub,account,fixture_pkg):
    hub.importer.import_package(fixture_pkg);api=HubAPI(hub);aid=account['id']
    iid=hub.db.one("SELECT id FROM items WHERE item_type='assignment' AND canvas_id='7001'")['id']
    ctx=ToolContext(api=api,account_id=aid,scope_item_id=iid,scope_course_id='4242')
    result,error,display=run_tool_display(ctx,'show_checklist',{'sections':[{'rows':[{'label':'Read requirements'}]}]},{'ui'})
    assert not error,result
    changed=variant(fixture_pkg,'requirements-edit','2026-11-25T00:00:00Z')
    next(i for i in changed['items'] if i['type']=='assignment' and i['canvas_id']=='7001')['fields']['description']='<p>New required appendix</p>'
    hub.importer.import_package(changed)
    assert hub.components.get(aid,display['component_id'])['source_status']['needs_review']
