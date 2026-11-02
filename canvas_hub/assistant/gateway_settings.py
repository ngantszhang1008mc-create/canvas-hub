"""Private invitation/session settings for the optional allowance provider."""
from __future__ import annotations

import time

from ..agent_tools import TOOLS_BY_NAME
from ..util import InvalidInput, NotFound
from .gateway import GatewayError, allowance, base_url, enroll

GATEWAY_FIELDS = {'gateway_mode', 'gateway_consent', 'gateway_token', 'gateway_expires_at',
                  'gateway_session_id', 'gateway_user_id', 'gateway_tools', 'gateway_available_tools', 'gateway_models'}


def apply_gateway(cfg, data, previous):
    is_gateway = cfg.get('provider') == 'allowance_gateway'
    was_gateway = previous.get('provider') == 'allowance_gateway'
    if is_gateway != was_gateway:
        for key in GATEWAY_FIELDS:
            cfg.pop(key, None)
        cfg.update(api_key='', extra_params={})
    if not is_gateway:
        return
    mode = data.get('gateway_mode', cfg.get('gateway_mode', 'mock'))
    try:
        endpoint = base_url(cfg.get('base_url'), mode)
    except GatewayError as e:
        raise InvalidInput(str(e)) from e
    if data.get('api_key') or data.get('extra_params'):
        raise InvalidInput('Gateway authentication uses invitations, never your provider API key or extra request settings.')
    changed = previous.get('base_url') != endpoint or previous.get('gateway_mode') != mode or not was_gateway
    if changed or data.get('clear_gateway_session') is True:
        for key in GATEWAY_FIELDS - {'gateway_mode'}:
            cfg.pop(key, None)
    cfg.update(base_url=endpoint, gateway_mode=mode, api_key='', extra_params={})
    if 'gateway_consent' in data:
        if type(data['gateway_consent']) is not bool:
            raise InvalidInput('gateway_consent must be true or false')
        cfg['gateway_consent'] = data['gateway_consent']
    if 'gateway_tools' in data:
        chosen = data['gateway_tools']
        if (not isinstance(chosen, list) or len(chosen) > 128
                or any(not isinstance(n, str) or n not in TOOLS_BY_NAME for n in chosen) or len(set(chosen)) != len(chosen)):
            raise InvalidInput('Gateway tools must name at most 128 distinct local tools.')
        cfg['gateway_tools'] = chosen


class GatewaySettings:
    @staticmethod
    def _gateway_public(cfg):
        return {'gateway_mode': cfg.get('gateway_mode', 'mock'), 'gateway_consent': cfg.get('gateway_consent') is True,
                'gateway_session_saved': bool(cfg.get('gateway_token')),
                'gateway_expires_at': cfg.get('gateway_expires_at'),
                'gateway_tools': cfg.get('gateway_tools') or [],
                'gateway_available_tools': cfg.get('gateway_available_tools') or [],
                'gateway_models': cfg.get('gateway_models') or []}

    @staticmethod
    def _gateway_profile(store, pid):
        prof = next((p for p in store['profiles'] if p['id'] == pid), None)
        if not prof:
            raise NotFound('Gateway setup no longer exists.')
        if prof.get('provider') != 'allowance_gateway':
            raise InvalidInput('Choose an allowance gateway setup first.')
        return prof

    @staticmethod
    def _gateway_catalog(prof, info, *, initial=False):
        available = [name for name in info['tools'] if name in TOOLS_BY_NAME]
        prof.update(gateway_available_tools=available,
                    gateway_models=[{k: m[k] for k in ('id', 'min_output_tokens', 'max_output_tokens')} for m in info['models']])
        if initial:
            prof['gateway_tools'] = available

    def gateway_enroll(self, profile_id, invitation, *, consent=False):
        if consent is not True:
            raise InvalidInput('Review and accept the gateway disclosure before connecting.', code='disclosure_required')
        with self._cfg_lock:
            store = self._load_store()
            prof = self._gateway_profile(store, profile_id)
            if prof.get('gateway_token') and (prof.get('gateway_expires_at') or 0) > time.time() * 1000:
                raise InvalidInput('This setup already has a session. Check allowance, or remove the session before replacing it.')
            try:
                data = enroll(prof, invitation)
                prof.update(gateway_token=data['token'], gateway_expires_at=data['expires_at'],
                            gateway_session_id=data['session_id'], gateway_user_id=data['user_id'])
                # Persist the one-time response before any second network call.
                self._save_store(store)
                info = allowance(prof)
                self._gateway_catalog(prof, info, initial=True)
                self._save_store(store)
            except GatewayError as e:
                raise InvalidInput(str(e), code='gateway_connection_failed') from e
            except OSError as e:
                raise InvalidInput('Gateway session could not be saved. The invitation may already be consumed; contact the operator for recovery.', code='gateway_storage_failed') from e
            return {**self.public_config(), 'ok': True, 'saved_profile': prof['id'], 'allowance': info}

    def gateway_check(self, profile_id=None):
        with self._cfg_lock:
            store = self._load_store()
            prof = self._gateway_profile(store, profile_id or store['active'])
            try:
                info = allowance(prof)
                self._gateway_catalog(prof, info)
                self._save_store(store)
                return {'ok': True, 'model': prof['model'], 'allowance': info,
                        'gateway_tools': prof.get('gateway_tools') or [], 'gateway_available_tools': prof['gateway_available_tools']}
            except GatewayError as e:
                return {'ok': False, 'message': str(e)}
