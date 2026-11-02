"""Local process fixtures exercise timeout/cancellation without a Canvas session."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

from canvas_hub.collector.client import Cancelled


def test_bounded_real_conversion_and_cached_restart(hub, monkeypatch):
    sha = hub.resources.store_bytes(b"Local fixture document.\n", "text/plain")
    result = hub.resources.extract_bounded(sha, "fixture.txt", should_stop=lambda: False)
    assert result["extraction_status"] == "ok"
    assert "Local fixture document." in hub.resources.extracted_text_for(sha)
    monkeypatch.setattr(hub.resources, "_extraction_command", lambda *args: pytest.fail("cache spawned worker"))
    assert hub.resources.extract_bounded(sha, "fixture.txt", should_stop=lambda: False) == result


def test_timeout_leaves_retryable_state_and_cleans_temp(hub, monkeypatch):
    sha = hub.resources.store_bytes(b"fixture")
    roots = []
    def slow(request, result):
        roots.append(request.parent)
        return [sys.executable, "-c", "import time; time.sleep(60)"]
    monkeypatch.setattr(hub.resources, "_extraction_command", slow)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        hub.resources.extract_bounded(sha, "fixture.txt", should_stop=lambda: False, timeout_seconds=.15)
    assert time.monotonic() - started < 3
    assert hub.db.one("SELECT extraction_status FROM resources WHERE id=?", (sha,))[0] == "not_attempted"
    assert not roots[0].exists()
    # A successful retry commits exactly one durable resource.
    monkeypatch.setattr(hub.resources, "_extraction_command", lambda request, result: [
        sys.executable, "-c", "import pathlib,json,sys; pathlib.Path(sys.argv[1]).write_text(json.dumps(['ok',None,'retry','fixture',None]))", str(result)])
    assert hub.resources.extract_bounded(sha, "fixture.txt", should_stop=lambda: False)["extraction_status"] == "ok"


def test_cancel_while_running_reaps_worker(hub, monkeypatch, tmp_path):
    sha = hub.resources.store_bytes(b"fixture")
    marker = tmp_path / "pid"
    monkeypatch.setattr(hub.resources, "_extraction_command", lambda *args: [
        sys.executable, "-c", "import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)", str(marker)])
    with pytest.raises(Cancelled):
        hub.resources.extract_bounded(sha, "fixture.txt", should_stop=marker.exists)
    if os.name != "nt":
        with pytest.raises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)
    assert hub.db.one("SELECT extraction_status FROM resources WHERE id=?", (sha,))[0] == "not_attempted"


def test_worker_crash_is_retryable_and_diagnostics_are_not_exposed(hub, monkeypatch):
    sha = hub.resources.store_bytes(b"fixture")
    monkeypatch.setattr(hub.resources, "_extraction_command", lambda *args: [
        sys.executable, "-c", "import sys; print('private body', file=sys.stderr); sys.exit(2)"])
    with pytest.raises(RuntimeError, match="worker failed") as error:
        hub.resources.extract_bounded(sha, "fixture.txt", should_stop=lambda: False)
    assert "private body" not in str(error.value)
    assert hub.db.one("SELECT extraction_status FROM resources WHERE id=?", (sha,))[0] == "not_attempted"


def test_cancel_before_start_does_not_spawn(hub, monkeypatch):
    sha = hub.resources.store_bytes(b"fixture")
    monkeypatch.setattr(hub.resources, "_extraction_command", lambda *args: pytest.fail("spawned"))
    with pytest.raises(Cancelled):
        hub.resources.extract_bounded(sha, "fixture.txt", should_stop=lambda: True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
def test_timeout_terminates_ocr_descendant(hub, monkeypatch, tmp_path):
    sha = hub.resources.store_bytes(b"fixture")
    marker = tmp_path / "child"
    code = ("import subprocess,sys,time,pathlib; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)")
    monkeypatch.setattr(hub.resources, "_extraction_command", lambda *args: [sys.executable, "-c", code, str(marker)])
    with pytest.raises(TimeoutError):
        hub.resources.extract_bounded(sha, "fixture.txt", should_stop=lambda: False, timeout_seconds=.4)
    pid = int(marker.read_text())
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        status = Path(f"/proc/{pid}/status")
        if status.exists() and "State:\tZ" in status.read_text():
            break  # dead; its new parent must reap it
        time.sleep(.01)
    else:
        pytest.fail("OCR descendant remained alive")


def test_failed_conversion_can_be_explicitly_retried(hub, monkeypatch):
    sha = hub.resources.store_bytes(b"fixture")
    results = iter([['failed', 'converter failed', '', 'fixture', None],
                    ['ok', None, 'recovered text', 'fixture', None]])
    def command(request, output):
        return [sys.executable, '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])',
                str(output), json.dumps(next(results))]
    monkeypatch.setattr(hub.resources, '_extraction_command', command)
    assert hub.resources.extract_bounded(sha, 'fixture.txt', should_stop=lambda: False)['extraction_status'] == 'failed'
    assert hub.resources.extract_bounded(sha, 'fixture.txt', should_stop=lambda: False)['extraction_status'] == 'ok'
    assert hub.resources.extracted_text_for(sha) == 'recovered text'
