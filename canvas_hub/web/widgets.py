"""Widget routes kept separate from collection/adapters."""
from fastapi import Request
from ..util import InvalidInput


def install_widget_routes(app, api, body):
    def fields(data, allowed):
        if set(data) - set(allowed):
            raise InvalidInput("Unknown widget request fields")
        return data

    @app.get('/api/accounts/{aid}/widgets')
    def widget_board(aid: int, component_id: int | None = None):
        return api.hub.widgets.board(aid, component_id=component_id)

    @app.put('/api/accounts/{aid}/widgets/order')
    async def widget_order(aid: int, request: Request):
        b = fields(await body(request), ['keys', 'revision'])
        return api.hub.widgets.reorder(aid, b.get('keys'), revision=b.get('revision'))

    @app.put('/api/accounts/{aid}/widgets/time-zone')
    async def widget_timezone(aid: int, request: Request):
        b = fields(await body(request), ['time_zone'])
        return api.hub.widgets.set_timezone(aid, b.get('time_zone'))

    @app.put('/api/accounts/{aid}/widgets/{key}')
    async def widget_save(aid: int, key: str, request: Request):
        b = fields(await body(request), ['config', 'expected_version'])
        return api.hub.widgets.save(aid, key, b.get('config'), expected_version=b.get('expected_version'))

    @app.post('/api/accounts/{aid}/widgets/{key}/review')
    async def widget_review(aid: int, key: str, request: Request):
        b = fields(await body(request), ['proposal_id', 'approve'])
        return api.hub.widgets.review(aid, key, proposal_id=b.get('proposal_id'), approve=b.get('approve'))
