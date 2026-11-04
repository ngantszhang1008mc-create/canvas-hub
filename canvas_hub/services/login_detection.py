"""Bounded anonymous detection after failed analysis or an explicit check."""
import threading

from ..util import Conflict
from .. import external_inspection
from .login_detector import inspect


class LoginDetection:
    def __init__(self, hub, detector=None):
        self.hub = hub
        self.detector = detector or inspect
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(2)
        self._states = {}

    def get(self, aid, sid):
        account = self.hub.identity.get_account(aid)
        source = self.hub.external_sources._source(aid, sid)
        if not external_inspection.ENABLED:
            return {'state':'paused', **external_inspection.status()}
        key = (aid, sid, account['origin'], account['canvas_user_id'], source['url'])
        with self._lock:
            return dict(self._states.get(key, {'state': 'not_checked'}))

    def start(self, aid, sid, *, failure_id=None):
        external_inspection.require_enabled()
        account = self.hub.identity.get_account(aid)
        source = self.hub.external_sources._source(aid, sid)
        key = (aid, sid, account['origin'], account['canvas_user_id'], source['url'])
        with self._lock:
            previous = self._states.get(key, {})
            if failure_id is not None and (previous.get('failure_id') == failure_id or previous.get('state') == 'running'):
                return dict(previous)
            if previous.get('state') == 'running':
                raise Conflict('This site check is already running')
            # A manual recheck may replace the result, but must not make a
            # subsequent source read repeat the automatic check for that failure.
            binding = ({'failure_id': failure_id} if failure_id is not None else
                       {'failure_id': previous['failure_id'], 'manual': True} if 'failure_id' in previous else {})
            # Retain bounded, process-local diagnostics; never persist page data.
            if key not in self._states and len(self._states) >= 128:
                oldest = next((k for k, v in self._states.items() if v.get('state') != 'running'), None)
                if oldest is not None:
                    self._states.pop(oldest)
            if not self._slots.acquire(blocking=False):
                if failure_id is not None:
                    state = {'state': 'finished', 'outcome': 'unsupported_unavailable',
                             'evidence': ['probe_busy'], 'course_access': 'unknown', 'failure_id': failure_id}
                    self._states[key] = state
                    return dict(state)
                raise Conflict('Site checks are busy; try again shortly')
            state = {'state': 'running', **binding}
            self._states[key] = state
        def run():
            try:
                external_inspection.require_enabled()
                result = self.detector(source['url'])
                # Do not return screenshots, raw URLs, page text or exceptions to UI.
                allowed = ('initial_host', 'final_host', 'http_status', 'outcome', 'evidence', 'course_access', 'auth_scheme', 'diagnostics')
                state = {'state': 'finished', **{k: result[k] for k in allowed if k in result}}
            except Exception:
                state = {'state': 'finished', 'outcome': 'unsupported_unavailable', 'evidence': ['probe_error'], 'course_access': 'unknown'}
            finally:
                state.update(binding)
                with self._lock:
                    self._states[key] = state
                self._slots.release()
        try:
            threading.Thread(target=run, name='site-login-detection', daemon=True).start()
        except Exception:
            with self._lock:
                if failure_id is not None:
                    self._states[key] = {'state': 'finished', 'outcome': 'unsupported_unavailable',
                                         'evidence': ['probe_error'], 'course_access': 'unknown', 'failure_id': failure_id}
                else:
                    self._states.pop(key, None)
            self._slots.release()
            if failure_id is not None:
                return self.get(aid, sid)
            raise
        return {'state': 'running', **binding}
