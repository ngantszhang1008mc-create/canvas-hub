"""Opt-in allowance gateway client. Credentials and tool execution stay local.

Each model round has one durable idempotency key and at most one POST. A lost
reply is never regenerated automatically. The journal is private runtime data,
outside course backups; no credential is written into an attempt or transcript.
"""
from __future__ import annotations

import asyncio
import codecs
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from .providers import ProviderError, Stopped, ToolCall, Turn
from .usage import normalize_usage

METADATA_SECONDS = 15
GENERATION_SECONDS = 135
MAX_METADATA = 131072
MAX_STREAM = 1048576
MAX_ARGUMENTS = 16384  # Protocol v1's historical-call limit, also enforced before execution.
NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
CALL_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
TOKEN = re.compile(r"^ses_[A-Za-z0-9_-]{43}$")
GATEWAY_CONSENT_VERSION = 2


def gateway_consent_valid(cfg):
    # Earlier production disclosures named only OpenAI. Preserve the session,
    # but require acceptance of the relay/DeepSeek disclosure before reuse.
    return cfg.get('gateway_consent') is True and (cfg.get('gateway_mode') != 'production'
            or cfg.get('gateway_consent_version') == GATEWAY_CONSENT_VERSION)


class GatewayError(ProviderError):
    abort_run = True


def base_url(value, mode):
    if mode not in ('mock', 'production'):
        raise GatewayError('Select mock or production gateway mode explicitly.')
    if not isinstance(value, str) or len(value) > 300 or any(ord(c) <= 32 for c in value):
        raise GatewayError('Enter the gateway origin URL without credentials, a path or query.')
    try:
        p = urlsplit(value)
        port = p.port
    except ValueError as e:
        raise GatewayError('Invalid gateway URL.') from e
    if (not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ('', '/')
            or p.scheme not in ('https', 'http') or (port is not None and not 1 <= port <= 65535)):
        raise GatewayError('Use the gateway origin URL without credentials, a path, query or fragment.')
    if p.scheme == 'http' and (mode != 'mock' or p.hostname not in ('127.0.0.1', '::1', 'localhost')):
        raise GatewayError('Remote and production gateways require HTTPS. HTTP is only for a loopback mock gateway.')
    host = '127.0.0.1' if p.hostname == 'localhost' else p.hostname
    host = f'[{host}]' if ':' in host else host
    return urlunsplit((p.scheme, host + (f':{port}' if port else ''), '', '', ''))


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True, allow_nan=False)


def _parse(data):
    try:
        value = json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError) as e:
        raise GatewayError('The gateway returned invalid JSON; no tool was executed.') from e
    if not isinstance(value, dict):
        raise GatewayError('The gateway returned an unsupported response.')
    return value


def _request_id(value):
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _error(data, status=None):
    code = data.get('error') if isinstance(data, dict) else None
    messages = {
        'invalid_session': 'Gateway session expired or was rejected. Reconnect with a new invitation.',
        'invalid_invite': 'The invitation is invalid, expired or already used. Obtain a new invitation.',
        'user_daily_limit': 'Daily allowance is unavailable. Check allowance and its UTC reset time.',
        'user_monthly_limit': 'Monthly allowance is unavailable. Check allowance and its UTC reset time.',
        'model_not_allowed': 'This model alias is no longer available at the gateway.',
        'tool_not_allowed': 'The gateway tool policy changed. Review the setup before retrying.',
        'input_too_large': 'This context exceeds the gateway input limit. Use a narrower question or compact the chat.',
        'body_too_large': 'This context exceeds the gateway body limit. Use a narrower question or compact the chat.',
        'invalid_output_limit': 'The response budget is outside the gateway model limits.',
    }
    safe_code = code if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,64}', code) else None
    message = messages.get(safe_code, 'Gateway stopped this attempt' + (f' ({safe_code})' if safe_code else '') + '. Check allowance and settings.')
    rid = _request_id(data.get('request_id')) if isinstance(data, dict) else None
    if rid:
        message += f' Request {rid} may have used allowance.'
    return GatewayError(message + ' No automatic retry or paid-provider fallback was made.')


def _metadata(cfg, method, path, body=None, *, session=True):
    url = base_url(cfg.get('base_url'), cfg.get('gateway_mode')) + path
    headers = {'Accept': 'application/json', 'Accept-Encoding': 'identity'}
    if session:
        token = cfg.get('gateway_token') or ''
        if not TOKEN.fullmatch(token):
            raise GatewayError('Connect this gateway setup with an invitation first.')
        headers['Authorization'] = 'Bearer ' + token
    async def request():
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            async with client.stream(method, url, json=body, headers=headers) as response:
                if response.is_redirect:
                    raise GatewayError('Gateway redirects are not followed. Review the exact gateway URL.')
                if response.headers.get('content-encoding', 'identity') != 'identity':
                    raise GatewayError('Compressed gateway metadata is unsupported.')
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_METADATA:
                        raise GatewayError('Gateway metadata exceeds the supported limit.')
                result = _parse(data)
                if not 200 <= response.status_code < 300:
                    raise _error(result, response.status_code)
                return result

    async def bounded():
        return await asyncio.wait_for(request(), timeout=METADATA_SECONDS)

    try:
        return asyncio.run(bounded())
    except (httpx.HTTPError, TimeoutError) as e:
        raise GatewayError('Gateway connection failed or timed out. No automatic retry was made; an invitation submission may already have been consumed.') from e



def enroll(cfg, invitation):
    if not gateway_consent_valid(cfg):
        raise GatewayError('Review and accept the gateway disclosure before connecting.')
    if not isinstance(invitation, str) or not re.fullmatch(r'inv_[A-Za-z0-9_-]{43}', invitation):
        raise GatewayError('Enter a valid operator-issued invitation.')
    data = _metadata(cfg, 'POST', '/v1/enroll', {'invite': invitation}, session=False)
    if (not isinstance(data.get('token'), str) or not TOKEN.fullmatch(data['token']) or type(data.get('expires_at')) is not int
            or not _request_id(data.get('user_id')) or not _request_id(data.get('session_id'))):
        raise GatewayError('Enrollment response was invalid. The invitation may be consumed; obtain operator recovery before retrying.')
    return data


def allowance(cfg):
    data = _metadata(cfg, 'GET', '/v1/allowance')
    caps = data.get('capabilities') or {}
    if data.get('mode') != cfg.get('gateway_mode'):
        raise GatewayError('Gateway mode differs from this setup. No model request was sent.')
    limits = {'max_tools': 128, 'max_tool_calls_per_response': 32, 'max_messages': 128, 'max_body_bytes': 1048576}
    if (not isinstance(caps, dict) or caps.get('protocol_version') != 1 or caps.get('mode_header') != 'X-Gateway-Mode'
            or any(type(caps.get(k)) is not int or not 1 <= caps[k] <= maximum for k, maximum in limits.items())):
        raise GatewayError('Gateway protocol or limits are unsupported. No model request was sent.')
    tools = data.get('tools')
    models = data.get('models')
    if (not isinstance(tools, list) or len(tools) > caps['max_tools']
            or any(not isinstance(n, str) or not NAME.fullmatch(n) for n in tools) or len(set(tools)) != len(tools)
            or not isinstance(models, list) or not 1 <= len(models) <= 128):
        raise GatewayError('Gateway capabilities are invalid.')
    seen = set()
    for model in models:
        if (not isinstance(model, dict) or not isinstance(model.get('id'), str) or not MODEL.fullmatch(model['id'])
                or model['id'] in seen or type(model.get('min_output_tokens')) is not int
                or type(model.get('max_output_tokens')) is not int
                or not 1 <= model['min_output_tokens'] <= model['max_output_tokens'] <= 128000):
            raise GatewayError('Gateway model capabilities are invalid.')
        seen.add(model['id'])
    windows = {}
    for kind in ('daily', 'monthly'):
        window = data.get(kind) or {}
        if (not isinstance(window, dict) or any(not isinstance(window.get(k), str) or not re.fullmatch(r'\d{1,24}', window[k])
               for k in ('limit_microusd', 'spent_microusd', 'held_microusd', 'remaining_microusd')) or type(window.get('resets_at')) is not int):
            raise GatewayError('Gateway allowance metadata is invalid.')
        windows[kind] = {k: window[k] for k in ('limit_microusd', 'spent_microusd', 'held_microusd', 'remaining_microusd', 'resets_at')}
    return {**windows, 'mode': data['mode'], 'tools': tools, 'models': models, 'capabilities': caps,
            'gateway_paused': data.get('gateway_paused') is True, 'reset_timezone': 'UTC'}


def authorize(cfg):
    check = cfg.get('_gateway_authorize')
    if check is not None:
        check()


def prepare_config(cfg):
    authorize(cfg)
    if not gateway_consent_valid(cfg):
        raise GatewayError('Accept the gateway disclosure in Assistant settings first.')
    if cfg.get('api_key') or cfg.get('extra_params') or cfg.get('thinking') or cfg.get('effort'):
        raise GatewayError('Gateway setups do not accept BYOK keys, reasoning settings or extra provider options.')
    info = allowance(cfg)
    model = next((m for m in info['models'] if m['id'] == cfg.get('model')), None)
    if not model:
        raise GatewayError('Choose an advertised gateway model alias in Assistant settings.')
    chosen = cfg.get('gateway_tools') or []
    if not isinstance(chosen, list) or any(n not in info['tools'] for n in chosen):
        raise GatewayError('Gateway tool policy changed. Check allowance and review the available tools before retrying.')
    if info['gateway_paused']:
        raise GatewayError('The gateway is paused. No model request was sent.')
    return {**cfg, '_gateway_allowance': info, '_gateway_model': model}


def selected_tools(cfg, tools):
    chosen = set(cfg.get('gateway_tools') or [])
    return [tool for tool in tools if tool['name'] in chosen]


def messages(system, transcript):
    result = [{'role': 'system', 'content': system}]
    pending, used = set(), set()
    for message in transcript:
        role = message.get('role')
        if pending and role != 'tool':
            raise GatewayError('Incomplete tool history cannot be sent to the gateway. Review the previous outcome first.')
        if role == 'user':
            result.append({'role': role, 'content': message.get('sent_text') or message.get('text') or ''})
        elif role == 'assistant':
            entry = {'role': role, 'content': message.get('text') or ''}
            calls = []
            for call in message.get('tool_calls') or []:
                args = call.get('input')
                if (not isinstance(args, dict) or not isinstance(call.get('id'), str) or not CALL_ID.fullmatch(call['id'])
                        or call['id'] in used or not isinstance(call.get('name'), str) or not NAME.fullmatch(call['name'])):
                    raise GatewayError('Unsupported historical tool call. Original history is retained; use a focused chat.')
                encoded = _json(args)
                if len(encoded.encode('utf-16-le')) // 2 > MAX_ARGUMENTS:
                    raise GatewayError('Historical tool arguments exceed the gateway limit. Original history is retained.')
                used.add(call['id']); pending.add(call['id'])
                calls.append({'id': call['id'], 'name': call['name'], 'arguments': encoded})
            if len(calls) > 32:
                raise GatewayError('Historical tool group exceeds the gateway limit.')
            if calls:
                entry['tool_calls'] = calls
            result.append(entry)
        elif role == 'tool':
            for item in message.get('results') or []:
                if item.get('id') not in pending or not isinstance(item.get('content'), str):
                    raise GatewayError('Historical tool results are incomplete or duplicated.')
                pending.remove(item['id'])
                result.append({'role': 'tool', 'tool_call_id': item['id'], 'content': item['content']})
        else:
            raise GatewayError('Unsupported conversation role for the gateway.')
    if pending:
        raise GatewayError('Historical tool results are missing. No model request was sent.')
    return result


class AllowanceGatewayProvider:
    name = 'allowance_gateway'

    def __init__(self, cfg):
        self.last_usage = None
        self.cfg = cfg if cfg.get('_gateway_allowance') else prepare_config(cfg)
        self.info, self.model = self.cfg['_gateway_allowance'], self.cfg['_gateway_model']
        self.closed = False

    def close(self):
        self.closed = True

    def _journal(self, record):
        directory = self.cfg.get('_gateway_journal')
        if not directory:
            raise GatewayError('Gateway attempt storage is unavailable. No automatic retry is safe.')
        directory = Path(directory)
        if directory.is_symlink():
            raise GatewayError('Gateway attempt storage cannot be a symbolic link.')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        path = directory / (record['key'] + '.json')
        temporary = directory / (record['key'] + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                out.write(_json(record)); out.flush(); os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def complete(self, system, transcript, tools, on_text=None):
        self.last_usage = None  # Never reuse counts from a previous model round.
        authorize(self.cfg)
        names = [tool['name'] for tool in tools]
        caps = self.info['capabilities']
        allowed = set(self.cfg.get('gateway_tools') or [])
        if len(names) > caps['max_tools'] or len(set(names)) != len(names) or any(n not in allowed for n in names):
            raise GatewayError('This request uses tools outside the reviewed gateway setup.')
        output = self.cfg.get('_response_tokens') or self.model['max_output_tokens']
        output = min(output, self.model['max_output_tokens'])
        if type(output) is not int or output < self.model['min_output_tokens']:
            raise GatewayError('The response budget is outside the gateway limits.')
        body = {'model': self.model['id'], 'messages': messages(system, transcript), 'tools': names, 'max_output_tokens': output}
        encoded = _json(body).encode('utf-8')
        if len(body['messages']) > caps['max_messages'] or len(encoded) > caps['max_body_bytes']:
            raise GatewayError('This context exceeds the advertised gateway message/body limit. Compact the chat or use a narrower request; nothing was truncated or sent.')
        if self.closed:
            raise Stopped()
        if on_text:
            on_text('')
        record = {'key': str(uuid.uuid4()), 'profile_id': self.cfg.get('id'), 'endpoint': self.cfg['base_url'],
                  'mode': self.cfg['gateway_mode'], 'model': self.model['id'], 'created_at': time.time(),
                  'state': 'dispatching', 'request_id': None, 'body': body}
        try:
            self._journal(record)  # Required before any generation POST.
            async def bounded():
                return await asyncio.wait_for(self._complete(encoded, names, record, on_text), timeout=GENERATION_SECONDS)
            return asyncio.run(bounded())
        except Stopped:
            record['state'] = 'cancelled_unknown'
            try:
                self._journal(record)
            except OSError:
                pass
            raise
        except (GatewayError, httpx.HTTPError, OSError, ValueError, TimeoutError) as e:
            if record['state'] not in ('completed', 'failed_before_dispatch', 'replay'):
                record['state'] = 'uncertain'
            try:
                self._journal(record)
            except OSError:
                pass
            if isinstance(e, GatewayError):
                raise
            raise GatewayError('Gateway request failed or its outcome could not be saved. Allowance may have been used. No automatic retry or tool execution occurred.') from e

    def _terminal_usage(self, terminal):
        if terminal.get('finish_reason') not in ('stop', 'tool_calls', 'length') or terminal.get('model') != self.model['id']:
            raise GatewayError('Gateway terminal model or finish reason is unsupported.')
        if terminal.get('accounting') not in (None, 'authoritative_usage', 'full_reservation'):
            raise GatewayError('Gateway accounting mode is unsupported; no tool was executed.')
        usage = terminal.get('usage')
        if terminal.get('accounting') == 'full_reservation':
            # A relay may provide estimates rather than authoritative metering.
            # Its successful response consumes the entire reserved allowance;
            # missing token counts remain unknown in local telemetry.
            reserved = terminal.get('reserved_microusd')
            if ('usage' not in terminal or usage is not None or not isinstance(reserved, str)
                    or not re.fullmatch(r'\d{1,24}', reserved) or terminal.get('charged_microusd') != reserved):
                raise GatewayError('Gateway conservative charge is invalid; no tool was executed.')
            return None
        valid = lambda n: type(n) is int and 0 <= n <= 9007199254740991
        if not isinstance(usage, dict) or any(not valid(usage.get(k)) for k in ('input_tokens', 'output_tokens')):
            raise GatewayError('Gateway terminal usage is missing or invalid; no tool was executed.')
        details = ('cached_input_tokens', 'cache_write_input_tokens', 'cache_write_30m_input_tokens',
                   'cache_write_12h_input_tokens', 'reasoning_tokens')
        for key in details:
            value = usage.get(key)
            bound = usage['output_tokens'] if key == 'reasoning_tokens' else usage['input_tokens']
            if value is not None and (not valid(value) or value > bound):
                raise GatewayError('Gateway usage details are invalid; no tool was executed.')
        cached, written = usage.get('cached_input_tokens'), usage.get('cache_write_input_tokens')
        ttl = [usage.get(k) for k in ('cache_write_30m_input_tokens', 'cache_write_12h_input_tokens')]
        if ((cached is not None and written is not None and cached + written > usage['input_tokens'])
                or (written is not None and sum(v for v in ttl if v is not None) > written)
                or (written is not None and all(v is not None for v in ttl) and sum(ttl) != written)):
            raise GatewayError('Gateway usage subsets are inconsistent; no tool was executed.')
        return {k: usage[k] for k in ('input_tokens', 'output_tokens', *details) if k in usage}

    async def _complete(self, body, names, record, on_text):
        text = ''
        calls = {}
        terminal = None
        terminal_usage = None
        seen_meta = False

        async def read_stream():
            nonlocal text, terminal, terminal_usage, seen_meta
            headers = {'Authorization': 'Bearer ' + self.cfg['gateway_token'], 'Content-Type': 'application/json',
                       'Accept': 'application/x-ndjson', 'Accept-Encoding': 'identity', 'Idempotency-Key': record['key'],
                       'X-Gateway-Mode': self.cfg['gateway_mode']}
            if self.cfg['gateway_mode'] == 'production' and gateway_consent_valid(self.cfg):
                headers['X-Gateway-Consent-Version'] = str(GATEWAY_CONSENT_VERSION)
            timeout = httpx.Timeout(60, connect=10, write=10, pool=10)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
                authorize(self.cfg)
                async with client.stream('POST', base_url(self.cfg['base_url'], self.cfg['gateway_mode']) + '/v1/responses',
                                         content=body, headers=headers) as response:
                    record['request_id'] = _request_id(response.headers.get('X-Request-ID'))
                    self._journal(record)
                    if response.is_redirect:
                        raise GatewayError('Gateway redirect was blocked. No retry or credential forwarding occurred.')
                    if response.headers.get('content-encoding', 'identity') != 'identity':
                        raise GatewayError('Compressed gateway streams are unsupported.')
                    if response.headers.get('content-type', '').split(';')[0].strip() != 'application/x-ndjson':
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > MAX_METADATA:
                                raise GatewayError('Gateway error response exceeds the supported limit.')
                        data = _parse(raw)
                        record['request_id'] = _request_id(data.get('request_id')) or record['request_id']
                        if data.get('replay') is True:
                            record['state'] = 'replay'
                            raise GatewayError('The gateway already has this request. Output cannot be replayed; allowance may have been used. Review its status before explicitly starting another attempt.')
                        raise _error(data, response.status_code)
                    if response.status_code != 200:
                        raise GatewayError('The gateway did not accept this stream.')
                    decoder = codecs.getincrementaldecoder('utf-8')('strict')
                    buffer = ''
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_STREAM:
                            raise GatewayError('Gateway stream exceeds the supported limit; no tool was executed.')
                        buffer += decoder.decode(chunk)
                        while '\n' in buffer:
                            line, buffer = buffer.split('\n', 1)
                            if not line.strip():
                                continue
                            event = _parse(line)
                            if terminal is not None:
                                raise GatewayError('Unexpected data after the gateway terminal record.')
                            kind = event.get('type')
                            if kind == 'meta':
                                rid = _request_id(event.get('request_id'))
                                if seen_meta or not rid or (record['request_id'] and record['request_id'] != rid):
                                    raise GatewayError('Gateway request identity changed.')
                                seen_meta = True; record['request_id'] = rid
                                self._journal(record)
                            elif not seen_meta:
                                raise GatewayError('Gateway stream is missing its request identity.')
                            elif kind == 'text.delta':
                                if not isinstance(event.get('delta'), str):
                                    raise GatewayError('Invalid text fragment from gateway.')
                                text += event['delta']
                            elif kind == 'tool_call.delta':
                                index = event.get('index')
                                if type(index) is not int or not 0 <= index < self.info['capabilities']['max_tool_calls_per_response']:
                                    raise GatewayError('Invalid gateway tool index.')
                                call = calls.setdefault(index, {'id': None, 'name': None, 'arguments': ''})
                                for key in ('id', 'name'):
                                    if key in event:
                                        if call[key] is not None and call[key] != event[key]:
                                            raise GatewayError('Gateway changed a tool call identity.')
                                        call[key] = event[key]
                                fragment = event.get('arguments_delta', '')
                                if not isinstance(fragment, str):
                                    raise GatewayError('Invalid gateway tool arguments.')
                                call['arguments'] += fragment
                                if len(call['arguments'].encode('utf-16-le')) // 2 > MAX_ARGUMENTS:
                                    raise GatewayError('Gateway tool arguments exceed the continuation limit; no tool was executed.')
                            elif kind == 'error':
                                raise _error(event)
                            elif kind == 'done':
                                if event.get('status') != 'completed' or event.get('request_id') != record['request_id']:
                                    raise GatewayError('Gateway did not complete this request successfully.')
                                terminal_usage = self._terminal_usage(event)
                                self.last_usage = normalize_usage(terminal_usage, 'gateway')
                                terminal = event
                            else:
                                raise GatewayError('Unsupported gateway stream record.')
                            if on_text:
                                on_text(text)
                    buffer += decoder.decode(b'', final=True)
                    if buffer.strip() or terminal is None:
                        raise GatewayError('Gateway reply ended without a complete terminal record. Allowance may have been used; no tool was executed.')

        task = asyncio.create_task(read_stream())
        try:
            while not task.done():
                if self.closed:
                    raise Stopped()
                authorize(self.cfg)
                if on_text:
                    on_text(text)
                await asyncio.wait({task}, timeout=.2)
            await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        usage = terminal_usage
        finish = terminal.get('finish_reason')
        complete_calls, identifiers = [], set()
        if finish == 'tool_calls':
            if not calls or sorted(calls) != list(range(len(calls))):
                raise GatewayError('Gateway tool fragments are incomplete.')
            for index in sorted(calls):
                call = calls[index]
                if (not isinstance(call['id'], str) or not CALL_ID.fullmatch(call['id']) or call['id'] in identifiers
                        or call['name'] not in names):
                    raise GatewayError('Gateway returned an unapproved or duplicate tool call.')
                args = _parse(call['arguments'])
                identifiers.add(call['id'])
                complete_calls.append(ToolCall(call['id'], call['name'], args))
        elif calls and finish != 'length':
            raise GatewayError('Gateway did not authorize completion of its tool calls.')
        record.update(state='completed', usage=usage, finish_reason=finish,
                      reserved_microusd=terminal.get('reserved_microusd'), charged_microusd=terminal.get('charged_microusd'),
                      accounting=terminal.get('accounting'))
        record.pop('body', None)
        self._journal(record)
        usage = ({**usage, 'cache_read_input_tokens': usage.get('cached_input_tokens'),
                  'cache_creation_input_tokens': usage.get('cache_write_input_tokens')} if usage is not None else {})
        return Turn(text=text, tool_calls=complete_calls, stop={'stop': 'end', 'tool_calls': 'tool_use', 'length': 'truncated'}[finish],
                    model=self.model['id'], usage=usage, raw={'provider': self.name, 'request_id': record['request_id'],
                                                           'accounting': terminal.get('accounting')})
