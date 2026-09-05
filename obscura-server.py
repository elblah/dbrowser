#!/usr/bin/env python3
"""
Browser IPC server - obscura backend (CDP over WebSocket).

Same Unix-socket JSON IPC as server.py (WebKitGTK) and chromium-server.py,
but drives obscura (github.com/h4ckf0r0day/obscura), a Rust headless
browser speaking the Chrome DevTools Protocol.

obscura exposes only the browser-level WS endpoint
(ws://HOST:PORT/devtools/browser). This server handles target discovery
(Target.getTargets / Target.createTarget) and session attach
(Target.attachToTarget flatten) itself, so the IPC command set stays
identical to the chromium backend.

Environment Variables:
  DBROWSER_OBSCURA_BIN      - obscura binary (default: obscura)
  DBROWSER_OBSCURA_ALLOW_PRIVATE_NETWORK - pass --allow-private-network so
                              localhost/LAN URLs are not blocked by the
                              SSRF guard (default: true)
  DBROWSER_CDP_HOST         - CDP host (default: 127.0.0.1)
  DBROWSER_CDP_PORT         - CDP port (default: 9222)
  DBROWSER_CDP_URL          - initial URL for created pages (default: about:blank)
  DBROWSER_AUTO_LAUNCH      - auto-launch obscura if CDP not up (default: true)
  DBROWSER_DEBUG            - enable debug logging (default: false)
  DBROWSER_WIDTH            - viewport width (default: 1280)
  DBROWSER_HEIGHT           - viewport height (default: 800)
  DBROWSER_TIMEOUT          - JS/cmd timeout seconds (default: 10.0)
  DBROWSER_SHOT_TIMEOUT     - screenshot timeout seconds (default: 30.0)
  DBROWSER_RECOVERY_GRACE   - seconds of sustained unresponsiveness before engine restart (default: 60.0)
  DBROWSER_NAV_TIMEOUT      - in-flight-nav fast-path window seconds (default: 30.0)
  DBROWSER_IDLE_TIMEOUT     - Auto-exit after N seconds of inactivity (default: 0 = disabled)
  DBROWSER_CONSOLE_BUFFER   - max console log lines (default: 1000)
  DBROWSER_NETWORK_BUFFER   - max network requests tracked (default: 100)
  DBROWSER_CDP_WAIT         - seconds to wait for CDP after launch (default: 60)
  SOCKET_PATH               - Unix socket (default: /run/user/{uid}/tmp/dbrowser.sock)

Usage:
  python3 obscura-server.py [url]
  echo '{"command":["help"]}' | nc -U $SOCKET_PATH
  python3 obscura-server.py --checkversion   # exit 0 ok / 1 outdated / 2 cannot check

Start obscura yourself (auto-launch also works):
  obscura serve --port 9222
"""
import os
import re
import sys
import json
import socket
import struct
import secrets
import select
import atexit
import signal
import threading
import subprocess
import time
import urllib.request
import base64 as _b64

# --- config ---
UID = os.getuid()
DEBUG = os.getenv("DBROWSER_DEBUG", "false").lower() in ("1", "true", "yes")
HOME = os.getenv("HOME", f"/home/{os.getenv('USER', 'user')}")
STORAGE_DIR = f"{HOME}/storage/tmp"
os.makedirs(STORAGE_DIR, exist_ok=True)
# Default socket matches the WebKit dbrowser server so this can be a
# transparent drop-in replacement. Override with SOCKET_PATH env var.
# NOTE: clashes with server.py / chromium-server.py if run at the same time.
DEFAULT_SOCKET = f"/run/user/{UID}/tmp/dbrowser.sock"
SOCKET_PATH = os.getenv("SOCKET_PATH", DEFAULT_SOCKET)

OBSCURA_BIN = os.getenv("DBROWSER_OBSCURA_BIN", "obscura")
# GitHub latest-release endpoint for the --checkversion mode.
# The server itself never phones home at startup.
RELEASES_API = "https://api.github.com/repos/h4ckf0r0day/obscura/releases/latest"
# obscura blocks requests to private/localhost addresses by default (SSRF
# guard). Pass --allow-private-network so localhost URLs behave like chromium.
ALLOW_PRIVATE_NETWORK = os.getenv("DBROWSER_OBSCURA_ALLOW_PRIVATE_NETWORK", "true").lower() in ("1", "true", "yes")
CDP_HOST = os.getenv("DBROWSER_CDP_HOST", "127.0.0.1")
CDP_PORT = int(os.getenv("DBROWSER_CDP_PORT", "9222"))
CDP_URL = os.getenv("DBROWSER_CDP_URL", "about:blank")
AUTO_LAUNCH = os.getenv("DBROWSER_AUTO_LAUNCH", "true").lower() in ("1", "true", "yes")

DBROWSER_WIDTH = int(os.getenv("DBROWSER_WIDTH", "1280"))
DBROWSER_HEIGHT = int(os.getenv("DBROWSER_HEIGHT", "800"))
DBROWSER_TIMEOUT = float(os.getenv("DBROWSER_TIMEOUT", "10.0"))
DBROWSER_NAV_TIMEOUT = float(os.getenv("DBROWSER_NAV_TIMEOUT", "30.0"))
# Screenshot render on Pi can take far longer than the default command timeout.
DBROWSER_SHOT_TIMEOUT = float(os.getenv("DBROWSER_SHOT_TIMEOUT", "30.0"))
# obscura is single-threaded: heavy pages starve all CDP replies for a while.
# Wait this long after the first timeout before declaring the engine dead.
DBROWSER_RECOVERY_GRACE = float(os.getenv("DBROWSER_RECOVERY_GRACE", "60.0"))

# Idle auto-exit: exit after N seconds of no IPC activity. 0/empty/invalid = disabled.
try:
    DBROWSER_IDLE_TIMEOUT = float(os.getenv("DBROWSER_IDLE_TIMEOUT", "0") or "0")
except (ValueError, TypeError):
    DBROWSER_IDLE_TIMEOUT = 0
if DBROWSER_IDLE_TIMEOUT <= 0:
    DBROWSER_IDLE_TIMEOUT = 0
last_activity = [time.monotonic()]
CONSOLE_BUFFER_SIZE = int(os.getenv("DBROWSER_CONSOLE_BUFFER", "1000"))
NETWORK_BUFFER_SIZE = int(os.getenv("DBROWSER_NETWORK_BUFFER", "500"))

DEVICE_PROFILES = {
    "phone-portrait":   (375, 812),
    "phone-landscape":  (812, 375),
    "tablet-portrait":  (768, 1024),
    "tablet-landscape": (1024, 768),
}

DEVICE_ALIASES = {
    "phone":  "phone-portrait",
    "mobile": "phone-portrait",
    "iphone": "phone-portrait",
    "tablet": "tablet-portrait",
    "ipad":   "tablet-portrait",
    "desktop": None,  # special: don't change viewport
}

# --- stdout activity log (always on) ---
def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --- --checkversion: compare installed obscura vs latest GitHub release ---
# Never runs as part of server startup. Exit codes:
#   0 = up to date, 1 = outdated, 2 = cannot check (offline, missing
#   binary, API error). Cron wrappers should notify only on 1.
def _parse_version(text):
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(x) for x in m.groups()) if m else None


def _installed_obscura_version():
    try:
        out = subprocess.run(
            [OBSCURA_BIN, "--version"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _parse_version(out)


def _latest_obscura_version():
    req = urllib.request.Request(RELEASES_API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "obscura-server-checkversion",
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        return _parse_version(json.load(r).get("tag_name", ""))


def check_version_command():
    installed = _installed_obscura_version()
    if installed is None:
        print(f"cannot check: cannot run '{OBSCURA_BIN} --version'")
        return 2
    istr = ".".join(map(str, installed))
    try:
        latest = _latest_obscura_version()
    except Exception as e:
        print(f"cannot check: {e}")
        return 2
    if latest is None:
        print("cannot check: unparsable latest release tag")
        return 2
    lstr = ".".join(map(str, latest))
    if installed >= latest:
        print(f"obscura {istr} up to date (latest {lstr})")
        return 0
    print(f"obscura {istr} OUTDATED - latest {lstr}: "
          "https://github.com/h4ckf0r0day/obscura/releases")
    return 1

# --- minimal websocket client (RFC 6455, client side, text+binary, no deflate) ---
class WS:
    OPC_CONT = 0x0
    OPC_TEXT = 0x1
    OPC_BIN  = 0x2
    OPC_CLOSE = 0x8
    OPC_PING = 0x9
    OPC_PONG = 0xA

    def __init__(self, sock):
        self.s = sock
        self.s.settimeout(None)

    @classmethod
    def connect(cls, host, port, path):
        s = socket.create_connection((host, port), timeout=10)
        key = _b64.b64encode(secrets.token_bytes(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        s.sendall(req.encode())
        # read until \r\n\r\n
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                raise ConnectionError("ws handshake closed")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("ws handshake failed: " + head.decode(errors="replace"))
        w = cls(s)
        w._buf = rest
        return w

    def _fill(self, n):
        buf = self._buf
        while len(buf) < n:
            chunk = self.s.recv(max(4096, n - len(buf)))
            if not chunk:
                raise ConnectionError("ws closed")
            buf += chunk
        out, self._buf = buf[:n], buf[n:]
        return out

    def recv(self):
        # opcode, payload
        hdr = self._fill(2)
        b1, b2 = hdr[0], hdr[1]
        opc = b1 & 0x0F
        masked = b2 & 0x80
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._fill(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._fill(8))[0]
        mask = self._fill(4) if masked else b""
        data = self._fill(ln)
        if mask:
            data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        if opc == self.OPC_PING:
            self._send_frame(self.OPC_PONG, data)
            return self.recv()
        if opc == self.OPC_CLOSE:
            raise ConnectionError("ws closed by peer")
        return opc, data

    def _send_frame(self, opc, data):
        b1 = 0x80 | opc
        n = len(data)
        mask_bit = 0x80
        if n < 126:
            hdr = bytes([b1, mask_bit | n])
        elif n <= 0xFFFF:
            hdr = bytes([b1, mask_bit | 126]) + struct.pack(">H", n)
        else:
            hdr = bytes([b1, mask_bit | 127]) + struct.pack(">Q", n)
        mask = secrets.token_bytes(4)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        self.s.sendall(hdr + mask + masked)

    def send(self, data):
        if isinstance(data, str):
            self._send_frame(self.OPC_TEXT, data.encode())
        else:
            self._send_frame(self.OPC_BIN, data)

    def close(self):
        try:
            self._send_frame(self.OPC_CLOSE, b"")
        except OSError:
            pass
        try:
            self.s.close()
        except OSError:
            pass

# --- CDP client (obscura: browser-level WS only, sessions via Target.attachToTarget) ---
class CDP:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.ws = None
        self.lock = threading.Lock()
        self.next_id = 1
        self.pending = {}  # id -> Event, result
        self.evt_thread = None
        self.console_buffer = []
        self.console_lock = threading.Lock()
        self.network_requests = {}
        self.network_lock = threading.Lock()
        self._net_counter = 0
        self._closed = False
        self._target_id = None     # page targetId this server drives
        self._session_id = None    # flatten session for that page
        self._crashed = False
        self._crash_reason = ""
        # Engine-health tracking: suspect=True once a command timed out
        # waiting for the engine; nav_started=timestamp while a navigation
        # may be holding the engine (Page.navigate reply not yet received).
        self.suspect = False
        self.suspect_since = None  # first suspect moment; recovery waits a grace period
        self.nav_started = None
        self.last_load_s = None  # duration of the most recent completed load
        self.last_load_s = None  # duration of the most recent completed load

    # -- liveness ---------------------------------------------------------
    def is_up(self):
        # live WS means the server is up; skip probing entirely
        if self.ws is not None and not self._closed:
            return True
        # HTTP /json/version first (may not exist on obscura); fall back to
        # a WS probe of the browser endpoint, which is the documented one.
        try:
            with urllib.request.urlopen(f"http://{self.host}:{self.port}/json/version", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        try:
            w = WS.connect(self.host, self.port, "/devtools/browser")
            w.close()
            return True
        except Exception:
            return False

    # -- target discovery -------------------------------------------------
    # NOTE: obscura gives each WS connection a fresh isolated CdpContext with
    # an EMPTY pages list (per dispatch.rs). Target ids learned anywhere else
    # (e.g. HTTP /json, served from a different context) can NEVER be attached
    # on this connection -> "Target not found". Discovery must therefore happen
    # on the same WS connection we drive.

    def _ws_discover_page(self):
        """Browser-level discovery: getTargets, create page if none."""
        r = self.send("Target.getTargets", {}, timeout=10)
        infos = r.get("result", {}).get("targetInfos", [])
        page = next((t for t in infos if t.get("type") == "page"), None)
        if page is None:
            r = self.send("Target.createTarget", {"url": CDP_URL}, timeout=15)
            if "error" in r:
                raise RuntimeError(f"Target.createTarget failed: {r['error'].get('message')}")
            tid = r.get("result", {}).get("targetId")
            if not tid:
                raise RuntimeError(f"Target.createTarget: no targetId in {r}")
            page = {"targetId": tid, "type": "page", "url": CDP_URL}
            _log(f"created page {tid} ({CDP_URL})")
        else:
            _log(f"using existing page {page.get('targetId')}")
        return page

    def _ws_attach(self, target_id):
        r = self.send("Target.attachToTarget",
                      {"targetId": target_id, "flatten": True}, timeout=10)
        if "error" in r or not r.get("result", {}).get("sessionId"):
            raise RuntimeError(f"Target.attachToTarget failed: {r.get('error', {}).get('message', r)}")
        sid = r["result"]["sessionId"]
        _log(f"attached page {target_id} (session {sid})")
        return sid

    def get_page_target(self):
        """Return {'targetId':..., 'sessionId':...} for the page we drive.

        Discover and attach on the SAME WS connection: obscura keeps pages
        per-connection, so ids from any other source are invalid here.
        """
        t = self._ws_discover_page()
        tid = t["targetId"]
        sid = self._ws_attach(tid)
        return {"targetId": tid, "sessionId": sid}

    def connect(self, target=None):
        # Always the browser-level endpoint; page commands carry sessionId.
        self.ws = WS.connect(self.host, self.port, "/devtools/browser")
        _log(f"CDP connected to ws://{self.host}:{self.port}/devtools/browser")
        self._closed = False
        self._crashed = False
        self._crash_reason = ""
        self.evt_thread = threading.Thread(target=self._reader, daemon=True)
        self.evt_thread.start()
        if target is None:
            target = self.get_page_target()
        self._target_id = target["targetId"]
        self._session_id = target.get("sessionId")
        # obscura implements no Log/Inspector domains: tolerate errors.
        self.send("Page.enable", session=True)
        self.send("Runtime.enable", session=True)
        self.send("Network.enable", session=True)
        self.send("Log.enable", session=True)
        self.send("Inspector.enable", session=True)

    # -- reader -----------------------------------------------------------
    def _reader(self):
        while not self._closed:
            try:
                opc, data = self.ws.recv()
            except (ConnectionError, OSError) as e:
                self._closed = True
                _log(f"CDP connection lost: {e}")
                with self.lock:
                    pending = self.pending
                    self.pending = {}
                for holder in pending.values():
                    holder["resp"] = {"error": {"message": f"disconnected: {e}"}}
                    holder["event"].set()
                return
            if opc != WS.OPC_TEXT:
                continue
            try:
                msg = json.loads(data.decode("utf-8", errors="replace"))
            except (ValueError, UnicodeDecodeError):
                continue
            if "id" in msg:
                with self.lock:
                    holder = self.pending.get(msg["id"])
                if holder is not None:
                    holder["resp"] = msg
                    ev = holder["event"]
                    ev.set()
            else:
                try:
                    self._on_event(msg)
                except Exception:
                    pass

    def _on_event(self, msg):
        method = msg.get("method", "")
        params = msg.get("params", {})
        # flatten sessions wrap page events: {"method":..., "params":{...}, "sessionId":...}
        if method == "Runtime.consoleAPICalled":
            try:
                level = params.get("type", "log")
                args = params.get("args", [])
                parts = []
                for a in args:
                    if a.get("type") == "string":
                        parts.append(a.get("value", ""))
                    else:
                        desc = a.get("description")
                        if desc is not None:
                            parts.append(str(desc))
                        elif "value" in a:
                            parts.append(repr(a["value"]))
                        else:
                            parts.append(json.dumps(a))
                line = f"[{level}] " + " ".join(parts)
            except Exception as e:
                line = f"[log] <parse error: {e}>"
            with self.console_lock:
                self.console_buffer.append(line)
                if len(self.console_buffer) > CONSOLE_BUFFER_SIZE:
                    self.console_buffer.pop(0)
        elif method == "Runtime.exceptionThrown":
            try:
                ex = params.get("exceptionDetails", {})
                txt = ex.get("text", "")
                desc = ex.get("exception", {}).get("description", "")
                line = f"[exception] {txt} {desc}".strip()
            except Exception:
                line = "[exception] <unparsed>"
            with self.console_lock:
                self.console_buffer.append(line)
                if len(self.console_buffer) > CONSOLE_BUFFER_SIZE:
                    self.console_buffer.pop(0)
        elif method == "Inspector.targetCrashed":
            reason = params.get("status", "crashed")
            self._crashed = True
            self._crash_reason = reason
            _log(f"page crashed: {reason}")
        elif method == "Page.frameNavigated":
            fr = params.get("frame", {})
            if not fr.get("parentId"):  # skip iframes
                _log(f"loading: {fr.get('url', '')}")
        elif method == "Page.navigatedWithinDocument":
            fr = params.get("frame", {})
            if not fr.get("parentId"):
                if self.nav_started:
                    self.last_load_s = round(time.monotonic() - self.nav_started, 1)
                self.nav_started = None
                _log(f"loading (same-doc): {fr.get('url', '')}")
        elif method == "Page.loadEventFired":
            if self.nav_started:
                self.last_load_s = round(time.monotonic() - self.nav_started, 1)
            self.nav_started = None
            _log("load complete")
        elif method == "Network.requestWillBeSent":
            with self.network_lock:
                self._net_counter += 1
                rid = f"req_{self._net_counter}"
                req = params.get("request", {})
                self.network_requests[rid] = {
                    "id": rid,
                    "uri": req.get("url", ""),
                    "url": req.get("url", ""),
                    "type": params.get("type", "Other"),
                    "method": req.get("method", "GET"),
                    "headers": req.get("headers", {}),
                    "response_headers": {},
                    "status": "loading",
                }
                if len(self.network_requests) > NETWORK_BUFFER_SIZE:
                    # drop oldest
                    oldest = next(iter(self.network_requests))
                    del self.network_requests[oldest]
        elif method == "Network.responseReceived":
            req_id = params.get("requestId", "")
            http_err = None
            with self.network_lock:
                target_uri = params.get("response", {}).get("url", "")
                for rid, r in list(self.network_requests.items()):
                    if r["status"] == "loading" and (r["uri"] == target_uri or target_uri.endswith(r["uri"][:120])):
                        r["status_code"] = params["response"].get("status", 0)
                        r["mime_type"] = params["response"].get("mimeType", "")
                        r["response_headers"] = params["response"].get("headers", {})
                        r["status"] = "complete"
                        status = r["status_code"]
                        if status and status >= 400:
                            http_err = f"HTTP {status} {target_uri}"
                        break
            if http_err:
                _log(http_err)

    # -- command send -----------------------------------------------------
    def send(self, method, params=None, timeout=None, session=False):
        with self.lock:
            # Route page-domain commands to the attached page session.
            sid = self._session_id if session else None
            if session and not sid:
                return {"error": {"message": "no page session attached"}}
            mid = self.next_id
            self.next_id += 1
            ev = threading.Event()
            holder = {"event": ev, "resp": None}
            self.pending[mid] = holder
            payload = {"id": mid, "method": method, "params": params or {}}
            if sid:
                payload["sessionId"] = sid
        try:
            if self.ws is None:
                raise OSError("not connected")
            self.ws.send(json.dumps(payload))
        except (ConnectionError, OSError) as e:
            with self.lock:
                self.pending.pop(mid, None)
            return {"error": {"message": f"send failed: {e}"}}
        if not ev.wait(timeout=timeout or DBROWSER_TIMEOUT):
            if not self.suspect:
                self.suspect = True
                self.suspect_since = time.monotonic()
            with self.lock:
                self.pending.pop(mid, None)
            return {"error": {"message": f"timeout after {timeout or DBROWSER_TIMEOUT}s"}}
        self.suspect = False
        self.suspect_since = None
        with self.lock:
            self.pending.pop(mid, None)
        if holder["resp"] is None:
            return {"error": {"message": "no response"}}
        return holder["resp"]

    def send_nowait(self, method, params=None, session=False):
        """Fire-and-forget send: no pending registration, no reply wait.

        Obscura's Page.navigate handler blocks the reply until its internal
        navigation deadline (OBSCURA_NAV_TIMEOUT_MS, default 30s), while the
        page keeps loading anyway. The CDP-correct client pattern is to send
        the command and track progress via Page events instead. Any late
        reply for an unregistered id is dropped silently by _reader.
        """
        sid = self._session_id if session else None
        if session and not sid:
            return {"error": {"message": "no page session attached"}}
        with self.lock:
            mid = self.next_id
            self.next_id += 1
            payload = {"id": mid, "method": method, "params": params or {}}
            if sid:
                payload["sessionId"] = sid
        try:
            if self.ws is None:
                raise OSError("not connected")
            self.ws.send(json.dumps(payload))
        except (ConnectionError, OSError) as e:
            return {"error": {"message": f"send failed: {e}"}}
        return None

    def close(self):
        self._closed = True
        try:
            self.ws.close()
        except Exception:
            pass

# --- obscura lifecycle ---
_obscura_proc = None
_auto_launched = False  # True only if we started obscura ourselves

def _stop_obscura_proc():
    """SIGTERM the obscura process, then SIGKILL if it ignores it."""
    proc = _obscura_proc
    if not proc or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass


def _cleanup_obscura():
    """Called by atexit / signal handlers. Close CDP, SIGTERM obscura, then SIGKILL."""
    proc = _obscura_proc
    if not proc or proc.poll() is not None:
        return
    # Close the CDP websocket first so the browser knows we're done.
    if SERVER and SERVER.cdp and not getattr(SERVER.cdp, "_closed", True):
        try:
            SERVER.cdp.close()
        except Exception:
            pass
    _stop_obscura_proc()

atexit.register(_cleanup_obscura)

_owns_socket = False  # True only if we successfully bound SOCKET_PATH

def _cleanup_socket():
    global _owns_socket
    if not _owns_socket:
        return
    try:
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
    except OSError:
        pass
    _owns_socket = False

atexit.register(_cleanup_socket)

def launch_obscura():
    global _obscura_proc, _auto_launched
    args = [OBSCURA_BIN, "serve", "--port", str(CDP_PORT)]
    if ALLOW_PRIVATE_NETWORK:
        # without this, obscura's SSRF guard blocks localhost/LAN fetches
        args.append("--allow-private-network")
    if DEBUG:
        log_path = f"{STORAGE_DIR}/obscura-dbrowser.log"
        logf = open(log_path, "ab")
    else:
        logf = open(os.devnull, "wb")
    _log(f"launching obscura: {' '.join(args)}")
    proc = subprocess.Popen(args, stdout=logf, stderr=logf,
                            stdin=subprocess.DEVNULL,
                            start_new_session=True)
    _obscura_proc = proc
    # wait for CDP up
    for _ in range(int(os.getenv("DBROWSER_CDP_WAIT", "60")) * 2):
        time.sleep(0.5)
        if CDP(CDP_HOST, CDP_PORT).is_up():
            return True
    return False


# --- command handlers ---
def show_help():
    return """
Browser IPC Server (obscura backend)

JSON request:  {"command": ["cmd", "arg1", ...]}
Response:      {"status": "ok", "data": <result>}
               {"status": "error", "message": "..."}

Navigation:
  load-url <url>                - navigate to url
  back                          - history back
  forward                       - history forward
  reload                        - reload current page
  status                        - {url, title, ready, width, height, loading}
                                  adds load_s while loading, last_load_s after
  blank                         - de-facto cancel: navigate to about:blank
  restart                       - force engine relaunch (kills obscura, reconnects)

Inspection:
  eval-js <code>                - run JS, return value (or description)
  screenshot                    - PNG, base64-encoded (needs an obscura render build)
  get-console-output [N]        - last N console lines (default: all, N<0: tail)
  wait-for-selector <css> [t]   - poll until css matches (default 10s, max 60s)
  wait-for-text <substr> [t]    - poll until page body contains text (default 10s, max 60s)

Network:
  list-network-requests [max]   - tracked requests [{id,url,type,method,status_code,...}]
  get-network-request <id>      - single request details (headers, response_headers)

System:
  mem                           - engine RSS/swap/peak + host memory/load

Viewport:
  resize <w> <h>                - set viewport size (e.g. resize 1280 800)
  device [profile]              - viewport preset; profile is one of:
                                 phone-portrait, phone-landscape,
                                 tablet-portrait, tablet-landscape
                                 aliases: phone/mobile/iphone, tablet/ipad, desktop (no-op)

Identity:
  set-user-agent <ua>           - override User-Agent header
  cookies                       - list all cookies for current page (Network.getCookies)

Advanced:
  cdp <Domain.method> [json]    - raw CDP passthrough; e.g. cdp Page.printToPDF {"landscape":false}
                                 for setting cookies use cdp Network.setCookie {...} or
                                 Network.setCookies {cookies:[...]}
                                 Target.*/Browser.* go to the browser endpoint (no session);
                                 everything else is routed to the attached page session.

Other:
  help                          - this help
"""


class Server:
    def __init__(self):
        self.cdp = None
        self.width = DBROWSER_WIDTH
        self.height = DBROWSER_HEIGHT
        self.last_url = CDP_URL
        self._recover_lock = threading.Lock()
        self._last_recovery = 0.0
        self._last_navguard = None
        self._ensure_cdp()
        self._apply_viewport()

    def _ensure_cdp(self):
        if not self.cdp or not self.cdp.is_up():
            self.cdp = CDP(CDP_HOST, CDP_PORT)
            if not self.cdp.is_up():
                if not AUTO_LAUNCH:
                    raise RuntimeError(f"CDP not reachable at {CDP_HOST}:{CDP_PORT}")
                if not launch_obscura():
                    raise RuntimeError("failed to launch obscura")
                global _auto_launched
                _auto_launched = True
        # (re)connect WS
        if self.cdp.ws is None or getattr(self.cdp, "_closed", True):
            self.cdp.connect()

    def _apply_viewport(self):
        try:
            self.cdp.send("Emulation.setDeviceMetricsOverride", {
                "width": self.width,
                "height": self.height,
                "deviceScaleFactor": 1,
                "mobile": False,
            }, session=True)
        except Exception:
            pass

    def _check_engine(self):
        """Restart obscura if it stopped answering CDP commands.

        The wedge failure mode: Page.navigate to a heavy page blocks obscura
        indefinitely - no replies, no events. Each command times out on its
        own (suspect flag) but the server would stay half-dead forever.
        Confirm unresponsiveness with browser-level pings, then restart.
        """
        cdp = self.cdp
        if not cdp or not cdp.suspect:
            return
        # A young in-flight navigation may just be a slow page; give it time.
        if cdp.nav_started and (time.monotonic() - cdp.nav_started) < DBROWSER_NAV_TIMEOUT * 2:
            return
        self._recover_engine()

    def _recover_engine(self):
        if self._recover_lock.locked():
            return
        with self._recover_lock:
            cdp = self.cdp
            if not cdp or not cdp.suspect:
                return
            # Never auto-restart while a navigation is in flight: a heavy
            # page can stall every reply, and killing the engine mid-load
            # would lose the load. Manual `restart` IPC bypasses this.
            if cdp.nav_started:
                if self._last_navguard != cdp.nav_started:
                    self._last_navguard = cdp.nav_started
                    _log("engine suspect but nav in flight; no auto restart")
                return
            # obscura runs a single-threaded tokio runtime: a heavy render or
            # settle can starve every reply, including the lock-free pings
            # below. That is busyness, not death. Unless the socket is gone
            # (engine truly dead), give it a grace period to catch up.
            if not cdp._closed:
                since = cdp.suspect_since or time.monotonic()
                busy = time.monotonic() - since
                if busy < DBROWSER_RECOVERY_GRACE:
                    _log(f"engine busy {int(busy)}s; grace {DBROWSER_RECOVERY_GRACE:.0f}s before restart")
                    return
            # confirm twice: a browser-level ping must fail both times
            for _ in range(2):
                r = cdp.send("Target.getTargets", {}, timeout=3.0)
                if "error" not in r:
                    cdp.suspect = False
                    cdp.suspect_since = None
                    _log("engine responsive again, no restart needed")
                    return
            now = time.monotonic()
            if now - self._last_recovery < 30.0:
                _log("engine still unresponsive; restart rate-limited")
                return
            self._last_recovery = now
            _log("ENGINE UNRESPONSIVE - restarting obscura")
            try:
                cdp.close()
            except Exception:
                pass
            _stop_obscura_proc()
            self.cdp = None
            try:
                self._ensure_cdp()
                self._apply_viewport()
                self.cdp.nav_started = None
                _log("obscura restarted; engine recovered")
            except Exception as e:
                _log(f"obscura restart failed: {e}")

    def handle(self, cmd):
        if "command" not in cmd or not cmd["command"]:
            return {"status": "error", "message": "invalid command"}
        args = cmd["command"]
        name = args[0]

        # AI-requested engine restart: bypasses health checks and any
        # nav-in-flight guard. Full relaunch of the engine process.
        if name == "restart":
            try:
                if self.cdp:
                    self.cdp.close()
            except Exception:
                pass
            _stop_obscura_proc()
            self.cdp = None
            try:
                self._ensure_cdp()
                self._apply_viewport()
                self.cdp.nav_started = None
                _log("obscura restarted by request")
                return {"status": "ok", "data": "restarted"}
            except Exception as e:
                return {"status": "error", "message": f"restart failed: {e}"}

        # Check CDP health and relaunch if needed (auto_launch enabled)
        try:
            self._ensure_cdp()
        except Exception as e:
            return {"status": "error", "message": f"obscura not available: {e}"}

        try:
            self._check_engine()
        except Exception as e:
            _log(f"engine health check failed: {e}")

        try:
            if name == "help":
                return {"status": "ok", "data": show_help()}

            if name == "status":
                if self.cdp._crashed:
                    return {"status": "ok", "data": {
                        "url": None, "title": "crashed",
                        "ready": "crashed",
                        "width": self.width, "height": self.height,
                        "loading": False,
                        "crashed": True,
                        "crash_reason": self.cdp._crash_reason,
                    }}
                # Navigation in flight: engine may be blocked by a heavy page.
                # Answer from cache, no CDP roundtrip (would eat the timeout).
                if self.cdp.nav_started and (time.monotonic() - self.cdp.nav_started) < DBROWSER_NAV_TIMEOUT * 2:
                    return {"status": "ok", "data": {
                        "url": self.last_url, "title": "",
                        "ready": "loading",
                        "width": self.width, "height": self.height,
                        "loading": True, "crashed": False,
                        "load_s": round(time.monotonic() - self.cdp.nav_started, 1),
                    }}
                r = self.cdp.send("Runtime.evaluate", {
                    "expression": "JSON.stringify({url: location.href, title: document.title, ready: document.readyState})",
                    "returnByValue": True,
                }, session=True)
                if "error" in r:
                    msg = r["error"].get("message", "cdp error")
                    # Post-load settle can hold the V8 lock past the evaluate
                    # timeout; that is engine busyness, not a dead engine.
                    if "timeout" in msg:
                        return {"status": "ok", "data": {
                            "url": self.last_url, "title": "",
                            "ready": "busy",
                            "width": self.width, "height": self.height,
                            "loading": True, "crashed": False,
                            "load_s": round(time.monotonic() - self.cdp.nav_started, 1) if self.cdp.nav_started else None,
                        }}
                    return {"status": "error", "message": msg}
                data = json.loads(r["result"]["result"]["value"])
                data["width"] = self.width
                data["height"] = self.height
                data["loading"] = data.get("ready") != "complete"
                data["crashed"] = False
                data["last_load_s"] = self.cdp.last_load_s
                return {"status": "ok", "data": data}

            if name == "mem":
                engine = None
                if _obscura_proc and _obscura_proc.pid:
                    st = {}
                    try:
                        with open(f"/proc/{_obscura_proc.pid}/status") as f:
                            for line in f:
                                k, _, v = line.partition(":")
                                if k in ("VmRSS", "VmSwap", "VmHWM"):
                                    st[k] = int(v.strip().split()[0]) * 1024
                    except (OSError, ValueError):
                        pass
                    engine = {
                        "pid": _obscura_proc.pid,
                        "rss_mb": round(st.get("VmRSS", 0) / 1048576, 1),
                        "swap_mb": round(st.get("VmSwap", 0) / 1048576, 1),
                        "peak_mb": round(st.get("VmHWM", 0) / 1048576, 1),
                    }
                mi = {}
                try:
                    with open("/proc/meminfo") as f:
                        for line in f:
                            k, _, v = line.partition(":")
                            if k in ("MemAvailable", "SwapFree", "SwapTotal"):
                                mi[k] = int(v.strip().split()[0]) * 1024
                except (OSError, ValueError):
                    pass
                return {"status": "ok", "data": {
                    "engine": engine,
                    "host": {
                        "mem_avail_mb": round(mi.get("MemAvailable", 0) / 1048576, 1),
                        "swap_free_mb": round(mi.get("SwapFree", 0) / 1048576, 1),
                        "swap_total_mb": round(mi.get("SwapTotal", 0) / 1048576, 1),
                    },
                    "load": [round(x, 2) for x in os.getloadavg()],
                }}

            if name == "load-url":
                if len(args) < 2:
                    return {"status": "error", "message": "load-url requires url"}
                if args[1].startswith("file:"):
                    return {"status": "error", "message": "file:// urls denied by server policy"}
                self.cdp._crashed = False
                self.cdp._crash_reason = ""
                self.last_url = args[1]
                self.cdp.nav_started = time.monotonic()
                r = self.cdp.send_nowait("Page.navigate", {"url": args[1]}, session=True)
                if r is not None:
                    self.cdp.nav_started = None
                    return {"status": "error", "message": r["error"].get("message", "send failed")}
                return {"status": "ok", "data": f"loading {args[1]}"}

            if name == "eval-js":
                if len(args) < 2:
                    return {"status": "error", "message": "eval-js requires code"}
                r = self.cdp.send("Runtime.evaluate", {
                    "expression": args[1],
                    "returnByValue": True,
                    "awaitPromise": True,
                }, session=True)
                if "error" in r:
                    return {"status": "error", "message": r["error"].get("message", "cdp error")}
                res = r.get("result", {}).get("result", {})
                if "value" in res:
                    return {"status": "ok", "data": res["value"]}
                return {"status": "ok", "data": res.get("description", None)}

            if name == "screenshot":
                r = self.cdp.send("Page.captureScreenshot", {"format": "png"}, session=True,
                                  timeout=DBROWSER_SHOT_TIMEOUT)
                if "error" in r:
                    return {"status": "error", "message": r["error"].get("message")}
                return {"status": "ok", "data": r["result"]["data"]}

            if name == "back":
                self.cdp.send("Runtime.evaluate", {
                    "expression": "history.back()", "returnByValue": True,
                }, session=True)
                return {"status": "ok", "data": "back"}

            if name == "forward":
                self.cdp.send("Runtime.evaluate", {
                    "expression": "history.forward()", "returnByValue": True,
                }, session=True)
                return {"status": "ok", "data": "forward"}

            if name == "reload":
                self.cdp._crashed = False
                self.cdp._crash_reason = ""
                self.cdp.nav_started = time.monotonic()
                r = self.cdp.send_nowait("Page.reload", {"ignoreCache": False}, session=True)
                if r is not None:
                    self.cdp.nav_started = None
                    return {"status": "error", "message": r["error"].get("message", "send failed")}
                return {"status": "ok", "data": "reloading"}

            if name == "blank":
                # de-facto cancel: obscura has no Page.stopLoading; navigating
                # to about:blank (native navigate_blank fast path) replaces the
                # destination as soon as the engine frees up.
                self.cdp._crashed = False
                self.cdp._crash_reason = ""
                self.cdp.nav_started = time.monotonic()
                r = self.cdp.send_nowait("Page.navigate", {"url": "about:blank"}, session=True)
                if r is not None:
                    self.cdp.nav_started = None
                    return {"status": "error", "message": r["error"].get("message", "send failed")}
                return {"status": "ok", "data": "blanking"}

            if name == "get-console-output":
                lines = int(args[1]) if len(args) > 1 else None
                with self.cdp.console_lock:
                    buf = list(self.cdp.console_buffer)
                if lines is None:
                    out = buf
                elif lines < 0:
                    out = buf[lines:]
                else:
                    out = buf[:lines]
                return {"status": "ok", "data": out}

            if name == "wait-for-selector":
                if len(args) < 2:
                    return {"status": "error", "message": "wait-for-selector requires css"}
                css = args[1]
                timeout = float(args[2]) if len(args) > 2 else 10.0
                timeout = min(timeout, 60.0)
                import time as _t
                deadline = _t.time() + timeout
                expr = f"document.querySelectorAll({json.dumps(css)}).length"
                last_count = 0
                while _t.time() < deadline:
                    r = self.cdp.send("Runtime.evaluate", {"expression": expr, "returnByValue": True}, session=True)
                    if "error" in r:
                        return {"status": "error", "message": r["error"].get("message", "cdp error")}
                    last_count = r.get("result", {}).get("result", {}).get("value", 0)
                    if last_count > 0:
                        waited = int((timeout - (deadline - _t.time())) * 1000)
                        return {"status": "ok", "data": {"found": True, "count": last_count, "waited_ms": waited}}
                    _t.sleep(0.2)
                return {"status": "ok", "data": {"found": False, "timed_out": True, "count": last_count, "waited_ms": int(timeout * 1000)}}

            if name == "wait-for-text":
                if len(args) < 2:
                    return {"status": "error", "message": "wait-for-text requires substring"}
                text = args[1]
                timeout = float(args[2]) if len(args) > 2 else 10.0
                timeout = min(timeout, 60.0)
                import time as _t
                deadline = _t.time() + timeout
                expr = f"document.body && document.body.innerText.includes({json.dumps(text)})"
                while _t.time() < deadline:
                    r = self.cdp.send("Runtime.evaluate", {"expression": expr, "returnByValue": True}, session=True)
                    if "error" in r:
                        return {"status": "error", "message": r["error"].get("message", "cdp error")}
                    if r.get("result", {}).get("result", {}).get("value", False):
                        return {"status": "ok", "data": {"found": True, "waited_ms": int((timeout - (deadline - _t.time())) * 1000)}}
                    _t.sleep(0.2)
                return {"status": "ok", "data": {"found": False, "timed_out": True, "waited_ms": int(timeout * 1000)}}

            if name == "list-network-requests":
                mx = int(args[1]) if len(args) > 1 else None
                with self.cdp.network_lock:
                    reqs = list(self.cdp.network_requests.values())
                if mx and mx > 0:
                    reqs = reqs[:mx]
                return {"status": "ok", "data": reqs}

            if name == "get-network-request":
                if len(args) < 2:
                    return {"status": "error", "message": "get-network-request requires id"}
                with self.cdp.network_lock:
                    r = self.cdp.network_requests.get(args[1])
                if r is None:
                    return {"status": "error", "message": f"unknown id {args[1]}"}
                return {"status": "ok", "data": r}

            if name == "resize":
                if len(args) < 3:
                    return {"status": "error", "message": "resize requires w h"}
                w, h = int(args[1]), int(args[2])
                self.width, self.height = w, h
                self._apply_viewport()
                return {"status": "ok", "data": f"resized to {w}x{h}"}

            if name == "device":
                profile = args[1] if len(args) > 1 else None
                if profile in DEVICE_ALIASES:
                    profile = DEVICE_ALIASES[profile]
                if profile is None:
                    return {"status": "ok", "data": f"no viewport change for {args[1]}"}
                if profile not in DEVICE_PROFILES:
                    return {"status": "error", "message": f"unknown profile {profile}; options: {list(DEVICE_PROFILES)}"}
                w, h = DEVICE_PROFILES[profile]
                self.width, self.height = w, h
                self._apply_viewport()
                return {"status": "ok", "data": f"resized to {profile} ({w}x{h})"}

            if name == "set-user-agent":
                if len(args) < 2:
                    return {"status": "error", "message": "set-user-agent requires ua string"}
                ua = args[1]
                r = self.cdp.send("Network.setUserAgentOverride", {"userAgent": ua}, session=True)
                if "error" in r:
                    return {"status": "error", "message": r["error"].get("message")}
                return {"status": "ok", "data": f"ua set: {ua[:60]}"}

            if name == "cookies":
                r = self.cdp.send("Network.getCookies", {}, session=True)
                if "error" in r:
                    return {"status": "error", "message": r["error"].get("message")}
                cookies = r.get("result", {}).get("cookies", [])
                return {"status": "ok", "data": cookies}

            if name == "cdp":
                # generic passthrough: cdp <Domain.method> [json-args]
                if len(args) < 2:
                    return {"status": "error", "message": "cdp requires <Domain.method> [json-args]"}
                method = args[1]
                params = {}
                if len(args) > 2 and args[2].strip():
                    try:
                        params = json.loads(args[2])
                    except json.JSONDecodeError as e:
                        return {"status": "error", "message": f"bad json args: {e}"}
                # Target.*/Browser.* are browser-level; all else is session-routed
                session = not method.startswith(("Target.", "Browser."))
                r = self.cdp.send(method, params, session=session)
                if "error" in r:
                    return {"status": "error", "message": r["error"].get("message", str(r["error"]))}
                return {"status": "ok", "data": r.get("result", {})}

            return {"status": "error", "message": f"unknown command: {name}"}
        except (ConnectionError, OSError) as e:
            # try one reconnect
            try:
                self.cdp.connect()
                return {"status": "error", "message": f"reconnected after: {e}; please retry"}
            except Exception as e2:
                return {"status": "error", "message": f"connection lost: {e2}"}

def handle_client(conn):
    try:
        conn.settimeout(2.0)
        data = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break
        except socket.timeout:
            pass
        if not data:
            return
        last_activity[0] = time.monotonic()
        text = data.decode("utf-8", errors="replace").strip()
        if text == "help":
            resp = {"status": "ok", "data": show_help()}
        else:
            try:
                cmd = json.loads(text)
                if isinstance(cmd, dict) and cmd.get("command"):
                    desc = " ".join(str(a) for a in cmd["command"])
                    _log(f"ipc: {desc[:120]}")
                resp = SERVER.handle(cmd)
                if resp.get("status") == "error":
                    _log(f"ipc error: {str(resp.get('message', ''))[:120]}")
            except json.JSONDecodeError:
                resp = {"status": "error", "message": "invalid JSON. use 'help'."}
            except Exception as e:
                resp = {"status": "error", "message": f"server error: {e}"}
        try:
            conn.sendall(json.dumps(resp).encode("utf-8"))
        except OSError:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


SERVER = None

def main():
    global SERVER
    # initial URL from argv[1] overrides env
    if len(sys.argv) > 1:
        global CDP_URL
        CDP_URL = sys.argv[1]

    SERVER = Server()

    # socket setup
    if os.path.exists(SOCKET_PATH):
        test = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            test.connect(SOCKET_PATH)
            test.close()
            print(f"socket {SOCKET_PATH} in use (another dbrowser already running)", file=sys.stderr)
            sys.exit(1)
        except (ConnectionRefusedError, FileNotFoundError):
            try:
                os.unlink(SOCKET_PATH)
            except FileNotFoundError:
                pass
        finally:
            test.close()

    os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(SOCKET_PATH)
    sock.listen(8)
    os.chmod(SOCKET_PATH, 0o660)
    sock.setblocking(False)
    # From here on, the socket file is ours. atexit will clean it up.
    global _owns_socket
    _owns_socket = True

    # Signal handlers: run cleanup then exit. Without these, SIGTERM
    # kills the process before atexit runs and leaves obscura orphaned.
    def _shutdown(_signo, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(f"obscura-server listening on {SOCKET_PATH}")
    print(f"CDP: {CDP_HOST}:{CDP_PORT}  auto-launch: {AUTO_LAUNCH}  binary: {OBSCURA_BIN}")
    print(f"Test: echo '{{\"command\":[\"help\"]}}' | nc -U {SOCKET_PATH}")
    _log(f"initial page URL: {CDP_URL}")

    try:
        while True:
            r, _, _ = select.select([sock], [], [], 1.0)
            if DBROWSER_IDLE_TIMEOUT > 0 and (time.monotonic() - last_activity[0]) >= DBROWSER_IDLE_TIMEOUT:
                print(f"idle timeout {DBROWSER_IDLE_TIMEOUT:.0f}s reached - auto-exiting")
                break
            if not r:
                continue
            try:
                conn, _ = sock.accept()
            except OSError:
                break
            t = threading.Thread(target=handle_client, args=(conn,), daemon=True)
            t.start()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            sock.close()
        except Exception:
            pass
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
        # Only kill obscura if we started it. If the user runs obscura
        # manually on the CDP port, leave it alone.
        if _auto_launched:
            _cleanup_obscura()


if __name__ == "__main__":
    if "--checkversion" in sys.argv or os.getenv("CHECKVERSION") == "1":
        sys.exit(check_version_command())
    main()



