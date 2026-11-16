"""A small local proxy that the external-site browser sends every request through.

The browser's own routing only sees the first request of a redirect chain, so it cannot keep a
public page from redirecting, or loading a resource, into this computer or a private network.
This proxy sees every connection the browser makes: the page itself, each redirect hop and every
subresource. It resolves the host name itself, refuses the connection when any address is not
public, and connects to the address it checked, so a second DNS answer cannot change the target.

HTTPS is tunnelled (CONNECT) and never decrypted. Plain HTTP requests are forwarded one per
connection.
"""
from __future__ import annotations

import logging
import select
import socket
import threading
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

MAX_HEAD = 64 * 1024
IDLE_SECONDS = 120
HOP_HEADERS = {"proxy-connection", "connection", "keep-alive", "proxy-authorization", "te", "trailer", "upgrade"}


class GuardProxy:
    def __init__(self, refuse):
        """`refuse(ip)` returns True for an address the browser must not reach."""
        self.refuse = refuse
        self.refused: list[str] = []          # host names refused, newest last (bounded)
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    @property
    def server(self) -> str:
        return f"http://127.0.0.1:{self._sock.getsockname()[1]}"

    def start(self) -> "GuardProxy":
        if self._sock is not None:
            return self
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(64)
        self._sock = sock
        self._thread = threading.Thread(target=self._serve, name="link-guard-proxy", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def refused_since(self, mark: int) -> list[str]:
        with self._lock:
            return self.refused[mark:] if mark <= len(self.refused) else list(self.refused)

    def mark(self) -> int:
        with self._lock:
            return len(self.refused)

    # ---- internals -------------------------------------------------------------------
    def _serve(self) -> None:
        sock = self._sock
        while self._sock is sock:
            try:
                client, _ = sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _note_refused(self, host: str) -> None:
        with self._lock:
            self.refused.append(host)
            if len(self.refused) > 1000:
                del self.refused[:500]

    def _connect(self, host: str, port: int) -> socket.socket | None:
        host = host.strip("[]").lower()
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            return None
        if not infos or any(self.refuse(info[4][0]) for info in infos):
            self._note_refused(host)
            return None
        for family, kind, proto, _, address in infos:
            try:
                upstream = socket.socket(family, kind, proto)
                upstream.settimeout(15)
                upstream.connect(address)              # the address that was checked, not a new lookup
                upstream.settimeout(None)
                return upstream
            except OSError:
                continue
        return None

    def _handle(self, client: socket.socket) -> None:
        upstream = None
        try:
            client.settimeout(30)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(8192)
                if not chunk:
                    return
                head += chunk
                if len(head) > MAX_HEAD:
                    return
            head, rest = head.split(b"\r\n\r\n", 1)
            lines = head.decode("latin-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                upstream = self._connect(host, int(port))
                if upstream is None:
                    return                             # the browser reports a failed connection
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
            else:
                p = urlsplit(target)
                if p.scheme != "http" or not p.hostname:
                    return
                upstream = self._connect(p.hostname, p.port or 80)
                if upstream is None:
                    return
                path = (p.path or "/") + (f"?{p.query}" if p.query else "")
                headers = [h for h in lines[1:] if h.split(":", 1)[0].strip().lower() not in HOP_HEADERS]
                upstream.sendall("\r\n".join([f"{method} {path} {version}", *headers, "Connection: close", "", ""])
                                 .encode("latin-1") + rest)
            client.settimeout(None)
            self._pipe(client, upstream)
        except Exception:  # noqa: BLE001
            log.debug("guard proxy connection ended", exc_info=True)
        finally:
            for s in (client, upstream):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    @staticmethod
    def _pipe(a: socket.socket, b: socket.socket) -> None:
        peers = {a: b, b: a}
        while True:
            ready, _, _ = select.select(list(peers), [], [], IDLE_SECONDS)
            if not ready:
                return
            for s in ready:
                data = s.recv(65536)
                if not data:
                    return
                peers[s].sendall(data)
