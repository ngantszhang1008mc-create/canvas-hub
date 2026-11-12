"""Pause for the earlier AI link analysis, adapters and connected-agent site reads.

Reading an external link is done by ``services.link_inspection``, which is not
affected by this flag: it opens the page in the app's own browser when the user
asks, and needs no model. What stays paused here is the older path that sent
page text to an AI provider for classification, ran declarative adapters, or
asked a connected MCP agent to read a site on the user's behalf.

This is deliberately not an environment variable or user setting. Re-enabling
that path requires a reviewed code change. Tests of the dormant code may patch
ENABLED explicitly; the application always ships it paused.
"""
from .util import Conflict

ENABLED = False
MESSAGE = ('AI link analysis and connected-agent site reads are paused. Use External links in Canvas Hub to open '
           'and save a site instead. Saved links and source data are still available.')


def status():
    return {'enabled': ENABLED, 'message': '' if ENABLED else MESSAGE}


def require_enabled():
    if not ENABLED:
        raise Conflict(MESSAGE, code='external_inspection_paused')
