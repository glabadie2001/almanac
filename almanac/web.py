"""Almanac's own HTTP door: loopback only, one secret token, nothing shared with any other service.

* The server binds 127.0.0.1 and refuses any other address.
* Every request but `/ident` and `/healthz` needs the token: `Authorization: Bearer <token>`, or the signed session
  cookie a browser gets from `GET /login?token=<token>` (printed at startup; `--open` opens it).
* `Host` must name this port on loopback (or one of the configured `hosts`), which stops DNS rebinding.
* A POST must carry `Content-Length` and, when the browser sends `Origin`, come from this same origin.
  The cookie is HttpOnly + SameSite=Strict.
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PUBLIC = frozenset({"/ident", "/healthz"})
COOKIE = "almanac_session"
SESSION_TTL = 7 * 86400
JSON_LIMIT = 4_000_000
CSP = "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data: blob:; frame-ancestors 'none'"
COMPRESSIBLE = ("application/json", "text/", "application/javascript", "image/svg")
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")


class Refused(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def ensure_token(path: Path) -> str:
    """The door's secret, made once (32 random bytes, hex) and kept in `path`."""
    path = Path(path)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(32)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token


def _equal(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def _sign(token: str, value: str) -> str:
    return hmac.new(token.encode(), value.encode(), hashlib.sha256).hexdigest()


def new_session(token: str) -> str:
    value = f"{int(time.time())}.{secrets.token_hex(12)}"
    return f"{value}.{_sign(token, value)}"


def valid_session(raw: str | None, token: str) -> bool:
    if not raw:
        return False
    try:
        jar = SimpleCookie()
        jar.load(raw)
        value, signature = jar[COOKIE].value.rsplit(".", 1)
        age = time.time() - int(value.split(".", 1)[0])
        return 0 <= age <= SESSION_TTL and _equal(signature, _sign(token, value))
    except (KeyError, ValueError):
        return False


class JsonHandler(BaseHTTPRequestHandler):
    """JSON + static plumbing. Subclasses set `pages` ({path: (file, type)}), `static_dir`, `log`."""
    pages: dict = {}
    static_dir: Path | None = None
    log = logging.getLogger("almanac.web")
    _cache = None

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", self._cache or "no-store")
        self._cache = None
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def _bytes(self, data: bytes, content_type: str, code: int = 200, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        for name, value in extra:
            self.send_header(name, value)
        if len(data) > 1024 and content_type.startswith(COMPRESSIBLE) and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            data = gzip.compress(data, 5, mtime=0)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send(self, code: int, doc):
        self._bytes(json.dumps(doc, allow_nan=False, default=str).encode(), "application/json", code)

    def _read_body(self) -> bytes:
        if not hasattr(self, "_almanac_body"):
            size = getattr(self, "_almanac_length", 0) or 0
            body = self.rfile.read(size) if size else b""
            if len(body) != size:
                raise ValueError("incomplete request body")
            self._almanac_body = body
        return self._almanac_body

    def _body(self) -> dict:
        body = json.loads(self._read_body() or b"{}")
        if not isinstance(body, dict):
            raise ValueError("Expected JSON object")
        return body

    def _page(self, path: str) -> bool:
        if path not in self.pages or self.static_dir is None:
            return False
        name, kind = self.pages[path]
        self._bytes((Path(self.static_dir) / name).read_bytes(), kind + "; charset=utf-8")
        return True

    def _fail(self, exc: BaseException):
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        self.log.error("%s %s failed", self.command, self.path.partition("?")[0], exc_info=exc)
        try:
            self._send(500, {"ok": False, "error": "Operation failed"})
        except OSError:
            pass


def _host_ok(host: str | None, port: int, extra_hosts) -> bool:
    if not host:
        return False
    host = host.strip().lower()
    allowed = {f"{name}:{port}" for name in LOOPBACK_NAMES} | {h.lower() for h in extra_hosts}
    if port == 80:
        allowed |= set(LOOPBACK_NAMES)
    return host in allowed


def protect(handler_class, *, token: str, hosts=(), timeout: float = 60):
    """Wrap `handler_class` in the door: framing, Host, auth, Origin, `/login`, `/logout`."""

    class Guarded(handler_class):
        def setup(self):
            super().setup()
            self.connection.settimeout(timeout)

        def parse_request(self):
            if not super().parse_request():
                return False
            for name in ("_almanac_body", "_almanac_length"):
                if hasattr(self, name):
                    delattr(self, name)
            try:
                self._check()
            except Refused as exc:
                self._refuse(exc.status, str(exc))
                return False
            return True

        def _check(self):
            for name in ("Host", "Content-Length", "Authorization", "Origin", "Cookie"):
                if len(self.headers.get_all(name, [])) > 1:
                    raise Refused(400, f"duplicate {name} header")
            if self.headers.get("Transfer-Encoding"):
                raise Refused(400, "Transfer-Encoding is not supported; send Content-Length")
            raw = self.headers.get("Content-Length")
            if raw is None:
                if self.command in ("POST", "PUT", "PATCH"):
                    raise Refused(411, "Content-Length is required")
                size = 0
            elif not raw.isascii() or not raw.isdecimal():
                raise Refused(400, "invalid Content-Length")
            else:
                size = int(raw)
            if size > JSON_LIMIT:
                raise Refused(413, f"request body exceeds {JSON_LIMIT} bytes")
            self._almanac_length = size
            port = self.server.server_port
            if not _host_ok(self.headers.get("Host"), port, hosts):
                raise Refused(403, "unexpected Host")
            path = urlsplit(self.path).path
            if path in PUBLIC:
                return
            if path == "/login":
                return                      # checks its own token
            bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            if not (_equal(bearer, token) or valid_session(self.headers.get("Cookie"), token)):
                raise Refused(401, "sign in: open the /login?token=... link Almanac printed at startup")
            origin = self.headers.get("Origin")
            if self.command not in ("GET", "HEAD") and origin is not None:
                if not _host_ok(urlsplit(origin).netloc, port, hosts):
                    raise Refused(403, "cross-origin request refused")

        def _refuse(self, status: int, error: str):
            body = json.dumps({"ok": False, "error": error}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def _login(self) -> bool:
            url = urlsplit(self.path)
            if url.path == "/logout" and self.command == "POST":
                self._bytes(b"{}", "application/json", 200,
                            [("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")])
                return True
            if url.path != "/login" or self.command not in ("GET", "HEAD"):
                return False
            given = parse_qs(url.query).get("token", [""])[-1]
            if not _equal(given, token):
                self._refuse(401, "wrong or missing token")
                return True
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"{COOKIE}={new_session(token)}; Path=/; Max-Age={SESSION_TTL}; HttpOnly; SameSite=Strict")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True

        def handle_one_request(self):
            super().handle_one_request()
            size = getattr(self, "_almanac_length", 0) or 0
            if size and not hasattr(self, "_almanac_body") and not self.close_connection:
                try:
                    self._read_body()          # keep-alive: never read an unread body as the next request
                except Exception:
                    self.close_connection = True

    for verb in ("do_GET", "do_HEAD", "do_POST", "do_PUT", "do_DELETE"):
        inner = getattr(handler_class, verb, None)
        if inner is None:
            continue

        def wrapped(self, _inner=inner):
            if self._login():
                return None
            return _inner(self)
        setattr(Guarded, verb, wrapped)

    Guarded.__name__ = Guarded.__qualname__ = f"Guarded{handler_class.__name__}"
    return Guarded


def serve(handler_class, port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """A threading server bound to loopback only."""
    if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
        raise ValueError(f"Almanac binds loopback only, not {host!r}")
    server = ThreadingHTTPServer((host, port), handler_class)
    server.daemon_threads = True
    server.block_on_close = False
    return server
