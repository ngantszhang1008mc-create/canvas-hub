"""The proxy in front of the external-site browser: redirects and subresources get the same address check as the link."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from canvas_hub.services.guard_proxy import GuardProxy


@pytest.fixture
def private_service():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_GET(self):
            hits.append(self.path)
            body = b"<p>private service</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", hits
    server.shutdown()


def browse(proxy, origin, steps):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(proxy={"server": proxy.server, "bypass": "<-loopback>"})
        try:
            ctx = browser.new_context()
            # Two "public" pages, answered without the network: one redirects into the private
            # service, the other embeds an image from it.
            ctx.route("http://redirect.example/**", lambda r: r.fulfill(status=302, headers={"location": origin + "/admin"}))
            ctx.route("http://embed.example/**", lambda r: r.fulfill(
                status=200, content_type="text/html", body=f"<img src='{origin}/pixel.png'><p>public page</p>"))
            return [steps(ctx.new_page(), url) for url in ("http://redirect.example/x", "http://embed.example/x")]
        finally:
            browser.close()


def visit(page, url):
    try:
        page.goto(url, wait_until="networkidle", timeout=15000)
        return page.inner_text("body")
    except Exception as e:  # noqa: BLE001
        return str(e).splitlines()[0]


@pytest.mark.browser
def test_redirects_and_subresources_to_a_private_address_are_refused(private_service):
    origin, hits = private_service
    proxy = GuardProxy(lambda ip: True).start()
    try:
        redirected, embedded = browse(proxy, origin, visit)
    finally:
        proxy.stop()
    assert "ERR_" in redirected and "public page" in embedded
    assert hits == [] and "127.0.0.1" in proxy.refused


@pytest.mark.browser
def test_allowed_addresses_are_forwarded(private_service):
    origin, hits = private_service
    proxy = GuardProxy(lambda ip: False).start()
    try:
        redirected, embedded = browse(proxy, origin, visit)
    finally:
        proxy.stop()
    assert "private service" in redirected and hits == ["/admin", "/pixel.png"] and proxy.refused == []
