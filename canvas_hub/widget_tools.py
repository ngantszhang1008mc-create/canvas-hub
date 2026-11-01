"""Agent-facing placement proposals. Acceptance is only available to the user UI."""


def widget_tools(AgentTool, account, scoped_component):
    def list_widgets(ctx, args):
        board = ctx.api.hub.widgets.board(account(ctx, args))
        return {**board, 'widgets': [{k: v for k, v in w.items() if k != 'component'} for w in board['widgets']]}

    def propose(ctx, args):
        aid = account(ctx, args)
        scoped_component(ctx, aid, args['component_id'])
        return ctx.api.hub.widgets.propose(aid, args['component_id'], args['config'])

    return [
        AgentTool('list_overall_widgets', 'Read saved Overall widget placements, visibility reasons and pending proposals. Reading never calls AI.',
                  'read', {}, [], list_widgets),
        AgentTool('propose_overall_widget',
                  'Propose a home placement for an existing saved component. User reviews it in Overall → Manage widgets before it takes effect. '
                  'config: {mode:pinned|conditional|library, expanded?:boolean, rule?:object}. '
                  'Use only explicit user dates or stored evidence, never inferred grade urgency. Rules: '
                  '{kind:date_window,start:YYYY-MM-DD,end:YYYY-MM-DD,time_zone:IANA,reason:text,item_id?:integer,max_age_days?:1..90}; '
                  '{kind:event_today,item_id:stored calendar-event ID,time_zone:IANA,max_age_days?:1..90}; '
                  '{kind:active_work,item_id:ID} or {kind:active_work,course_id:ID}. '
                  'Dates are inclusive local calendar dates; event sources older than max_age_days (default 7) are uncertain. '
                  'Checklists retain progress; this does not reset daily tasks or change source definitions.',
                  'ui', {'component_id': {'type': 'integer'}, 'config': {'type': 'object'}},
                  ['component_id', 'config'], propose),
    ]
