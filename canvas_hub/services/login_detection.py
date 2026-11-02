"""User initiated anonymous detection, without saved sessions or model calls."""
import threading

from ..util import Conflict
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
        key = (aid, sid, account['origin'], account['canvas_user_id'], source['url'])
        with self._lock:
            return dict(self._states.get(key, {'state': 'not_checked'}))

    def start(self, aid, sid):
        account = self.hub.identity.get_account(aid)
        source = self.hub.external_sources._source(aid, sid)
        key = (aid, sid, account['origin'], account['canvas_user_id'], source['url'])
        with self._lock:
            if self._states.get(key, {}).get('state') == 'running':
                raise Conflict('This site check is already running')
            if not self._slots.acquire(blocking=False):
                raise Conflict('Site checks are busy; try again shortly')
            state = {'state': 'running'}
            self._states[key] = state
        def run():
            try:
                result = self.detector(source['url'])
                # Do not return screenshots, raw URLs, page text or exceptions to UI.
                allowed = ('initial_host', 'final_host', 'http_status', 'outcome', 'evidence', 'course_access')
                state = {'state': 'finished', **{k: result[k] for k in allowed if k in result}}
            except Exception:
                state = {'state': 'finished', 'outcome': 'unsupported_unavailable', 'evidence': ['probe_error'], 'course_access': 'unknown'}
            finally:
                with self._lock:
                    self._states[key] = state
                self._slots.release()
        try:
            threading.Thread(target=run, name='site-login-detection', daemon=True).start()
        except Exception:
            with self._lock:
                self._states.pop(key, None)
            self._slots.release()
            raise
        return {'state': 'running'}
