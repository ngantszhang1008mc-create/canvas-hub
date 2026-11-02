"""Local fake-provider checks for the shared Canvas request budget."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
import time

import pytest

from canvas_hub.collector.client import CanvasClient, Cancelled, RateGate, RateLimited, shared_rate_gate


class Response:
    status = 200
    headers = {"content-type": "application/json"}
    url = "https://canvas.test/api/v1/test"

    def __init__(self, status=200):
        self.status = status
        self.disposed = False

    def text(self):
        return '{"ok":true}'

    def body(self):
        return b'fixture'

    def dispose(self):
        self.disposed = True


def test_default_gate_is_shared_by_origin():
    a = CanvasClient(None, "https://canvas.test/")
    b = CanvasClient(None, "https://canvas.test")
    assert a.rate_gate is b.rate_gate
    assert a.rate_gate.min_interval == .35
    assert a.rate_gate.max_concurrent == 1
    assert a.rate_gate is not shared_rate_gate("https://other.test")


@pytest.mark.parametrize("experimental,expected", [(False, 1), (True, 2)])
def test_api_concurrency_is_bounded(experimental, expected):
    gate = RateGate(0, max_concurrent=2 if experimental else 1)
    lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()
    active = maximum = 0

    def get(*args, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == expected:
                entered.set()
        assert release.wait(2)
        with lock:
            active -= 1
        return Response()

    def run():
        return CanvasClient(SimpleNamespace(request=SimpleNamespace(get=get)), "https://local.test", rate_gate=gate).api_get("/test")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(run) for _ in range(4)]
        assert entered.wait(2)
        release.set()
        assert [f.result() for f in futures] == [{"ok": True}] * 4
    assert maximum == expected
    assert gate._active == 0


def test_shared_start_spacing(monkeypatch):
    now = [1.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds + .000001))
    gate = RateGate(.35)
    starts = []
    def get(*args, **kwargs):
        starts.append(now[0])
        return Response()
    context = SimpleNamespace(request=SimpleNamespace(get=get))
    for _ in range(4):
        CanvasClient(context, "https://local.test", rate_gate=gate).api_get("/test")
    assert all(b-a >= .35 for a,b in zip(starts, starts[1:]))


def test_retry_backoff_circuit_and_cancel(monkeypatch):
    now = [1.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds + .000001))
    gate = RateGate(0, failure_limit=2, cooldown=30)
    responses = []
    def get(*args, **kwargs):
        response = Response(429)
        responses.append(response)
        return response
    client = CanvasClient(SimpleNamespace(request=SimpleNamespace(get=get)), "https://local.test", rate_gate=gate)
    with pytest.raises(RateLimited, match="circuit"):
        client.api_get("/test")
    assert len(responses) == 2
    assert all(r.disposed for r in responses)
    assert client.retries == 2
    assert now[0] >= 5
    with pytest.raises(Cancelled):
        gate.wait(lambda: True)
    now[0] += 31
    gate.wait(lambda: False)


def test_download_disposes_response_counts_bytes_and_uses_timeout():
    response = Response()
    options = {}
    def get(*args, **kwargs):
        options.update(kwargs)
        return response
    client = CanvasClient(SimpleNamespace(request=SimpleNamespace(get=get)), "https://canvas.test", min_interval=0, timeout_ms=123)
    assert client.download("/file", 100)[0] == b'fixture'
    assert response.disposed
    assert options['timeout'] == 123
    assert client.metrics() == {"requests": 1, "retries": 0, "bytes": 7}


def test_cancel_releases_slot_and_disposes_response():
    stopped = [False]
    response = Response()
    def get(*args, **kwargs):
        stopped[0] = True
        return response
    gate = RateGate(0)
    client = CanvasClient(SimpleNamespace(request=SimpleNamespace(get=get)), "https://canvas.test", rate_gate=gate,
                          should_stop=lambda: stopped[0])
    with pytest.raises(Cancelled):
        client.download("/file", 100)
    assert response.disposed
    with pytest.raises(Cancelled):
        client.api_get("/test")
    assert gate._active == 0


def test_download_redirects_each_origin_budget_and_disposes(monkeypatch):
    from canvas_hub.collector import client as module
    now = [1.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds + .000001))
    canvas_gate = RateGate(.35)
    storage_gate = RateGate(.35)
    monkeypatch.setattr(module, "shared_rate_gate", lambda origin: storage_gate)
    calls, responses = [], []
    def get(url, **kwargs):
        calls.append((url, now[0], kwargs))
        response = Response(302 if len(calls) == 1 else 200)
        response.url = url
        if response.status == 302:
            response.headers = {"location": "https://storage.test/file?secret=never-log"}
        responses.append(response)
        return response
    storage_gate.wait(lambda: False)
    client = CanvasClient(SimpleNamespace(request=SimpleNamespace(get=get)), "https://canvas.test", rate_gate=canvas_gate)
    assert client.download("/file", 100)[0] == b'fixture'
    assert calls[1][1] - calls[0][1] >= .35
    assert all(call[2]['max_redirects'] == 0 for call in calls)
    assert all(r.disposed for r in responses)
    assert client.requests == 2


def test_download_refuses_redirect_downgrade_without_leaking_url():
    from canvas_hub.collector.client import CollectorError
    response = Response(302)
    response.headers = {"location": "http://storage.test/file?secret=never-log"}
    client = CanvasClient(SimpleNamespace(request=SimpleNamespace(get=lambda *a, **k: response)),
                          "https://canvas.test", min_interval=0)
    with pytest.raises(CollectorError, match="insecure download redirect") as error:
        client.download("/file", 100)
    assert 'secret' not in str(error.value)
    assert response.disposed
