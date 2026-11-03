"""Temporary release freeze for external inspection, not Canvas collection.

This is deliberately not an environment variable or user setting. Re-enabling
the unfinished feature requires a reviewed code change. Dormant-feature tests
may patch ENABLED explicitly; the application always ships paused.
"""
from .util import Conflict

ENABLED = False
MESSAGE = 'External inspection is temporarily unavailable. Saved links and source data are still available.'


def status():
    return {'enabled': ENABLED, 'message': '' if ENABLED else MESSAGE}


def require_enabled():
    if not ENABLED:
        raise Conflict(MESSAGE, code='external_inspection_paused')
