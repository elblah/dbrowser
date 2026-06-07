#!/usr/bin/env python3
"""
Browser IPC server - chromium backend (CDP over WebSocket).

Same Unix-socket JSON IPC as server.py (WebKitGTK), but drives chromium
via the Chrome DevTools Protocol. Stdlib only.

Environment Variables:
  DBROWSER_CHROMIUM_BIN     - chromium binary (default: chromium)
  DBROWSER_CDP_HOST         - CDP host (default: 127.0.0.1)
  DBROWSER_CDP_PORT         - CDP port (default: 9222)
  DBROWSER_CDP_URL          - initial URL (default: about:blank)
  DBROWSER_AUTO_LAUNCH      - auto-launch chromium if CDP not up (default: true)
  DBROWSER_WIDTH            - viewport width (default: 1280)
  DBROWSER_HEIGHT           - viewport height (default: 800)
  DBROWSER_TIMEOUT          - JS/cmd timeout seconds (default: 10.0)
  DBROWSER_CONSOLE_BUFFER   - max console log lines (default: 1000)
  DBROWSER_NETWORK_BUFFER   - max network requests tracked (default: 100)
  SOCKET_PATH               - Unix socket (default: /run/user/{uid}/tmp/dbrowser-chromium.sock)

Usage:
  python3 chromium-server.py [url]
  echo '{"command":["help"]}' | nc -U $SOCKET_PATH
"""
import os
import sys
import json
import socket
import base64
import struct
import hashlib
import secrets
import select
import atexit
import signal
import base64 as _b64
import threading
import subprocess
import time
import errno
import urllib.request

# --- config ---
UID = os.getuid()
# Default socket matches the WebKit dbrowser server so this can be a
# transparent drop-in replacement. Override with SOCKET_PATH env var.
DEFAULT_SOCKET = f"/run/user/{UID}/tmp/dbrowser.sock"
SOCKET_PATH = os.getenv("SOCKET_PATH", DEFAULT_SOCKET)

CHROMIUM_BIN = os.getenv("DBROWSER_CHROMIUM_BIN", "chromium")
CDP_HOST = os.getenv("DBROWSER_CDP_HOST", "127.0.0.1")
CDP_PORT = int(os.getenv("DBROWSER_CDP_PORT", "9222"))
CDP_URL = os.getenv("DBROWSER_CDP_URL", "about:blank")
AUTO_LAUNCH = os.getenv("DBROWSER_AUTO_LAUNCH", "true").lower() in ("1", "true", "yes")

DBROWSER_WIDTH = int(os.getenv("DBROWSER_WIDTH", "1280"))
DBROWSER_HEIGHT = int(os.getenv("DBROWSER_HEIGHT", "800"))
DBROWSER_TIMEOUT = float(os.getenv("DBROWSER_TIMEOUT", "10.0"))
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


# --- CDP client ---
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
        self._target_id = None

    def is_up(self):
        try:
            with urllib.request.urlopen(f"http://{self.host}:{self.port}/json/version", timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    def get_page_target(self):
        # find first 'page' type target
        with urllib.request.urlopen(f"http://{self.host}:{self.port}/json", timeout=5) as r:
            data = json.loads(r.read())
        for t in data:
            if t.get("type") == "page":
                return t
        return None

    def connect(self, target=None):
        if target is None:
            target = self.get_page_target()
        if not target:
            raise RuntimeError("no page target")
        self._target_id = target["id"]
        url = target["webSocketDebuggerUrl"].replace("wss://", "ws://").replace("ws://", "")
        # url is ws://host:port/devtools/page/ID
        # parse
        path = "/" + url.split("/", 1)[1]
        hostport = url.split("/", 1)[0]
        h, _, p = hostport.partition(":")
        self.ws = WS.connect(h, int(p), path)
        self._closed = False
        self.evt_thread = threading.Thread(target=self._reader, daemon=True)
        self.evt_thread.start()
        self.send("Page.enable")
        self.send("Runtime.enable")
        self.send("Network.enable")
        self.send("Log.enable")

    def _reader(self):
        while not self._closed:
            try:
                opc, data = self.ws.recv()
            except (ConnectionError, OSError) as e:
                self._closed = True
                with self.lock:
                    pending = self.pending
                    self.pending = {}
                for ev, holder in pending.values():
                    holder["resp"] = {"error": {"message": f"disconnected: {e}"}}
                    ev.set()
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
            # find by requestId
            req_id = params.get("requestId", "")
            with self.network_lock:
                # match by uri containing requestId? no — we keyed on counter, not CDP requestId.
                # We didn't store CDP requestId, so just match last loading entry with same uri.
                target_uri = params.get("response", {}).get("url", "")
                for rid, r in list(self.network_requests.items()):
                    if r["status"] == "loading" and (r["uri"] == target_uri or target_uri.endswith(r["uri"][:120])):
                        r["status_code"] = params["response"].get("status", 0)
                        r["mime_type"] = params["response"].get("mimeType", "")
                        r["response_headers"] = params["response"].get("headers", {})
                        r["status"] = "complete"
                        break

    def send(self, method, params=None, timeout=None):
        with self.lock:
            mid = self.next_id
            self.next_id += 1
            ev = threading.Event()
            holder = {"event": ev, "resp": None}
            self.pending[mid] = holder
            payload = {"id": mid, "method": method, "params": params or {}}
        try:
            self.ws.send(json.dumps(payload))
        except (ConnectionError, OSError) as e:
            with self.lock:
                self.pending.pop(mid, None)
            return {"error": {"message": f"send failed: {e}"}}
        if not ev.wait(timeout=timeout or DBROWSER_TIMEOUT):
            with self.lock:
                self.pending.pop(mid, None)
            return {"error": {"message": f"timeout after {timeout or DBROWSER_TIMEOUT}s"}}
        with self.lock:
            self.pending.pop(mid, None)
        if holder["resp"] is None:
            return {"error": {"message": "no response"}}
        return holder["resp"]

    def close(self):
        self._closed = True
        try:
            self.ws.close()
        except Exception:
            pass


# --- chromium lifecycle ---
_chromium_proc = None
_auto_launched = False  # True only if we started chromium ourselves

def _cleanup_chromium():
    """Called by atexit / signal handlers. Close CDP, SIGTERM chromium, then SIGKILL."""
    global _chromium_proc
    proc = _chromium_proc
    if not proc or proc.poll() is not None:
        return
    # Close the CDP websocket first so the browser knows we're done.
    if SERVER and SERVER.cdp and not getattr(SERVER.cdp, "_closed", True):
        try:
            SERVER.cdp.close()
        except Exception:
            pass
    # Graceful: SIGTERM. chromium exits cleanly and writes a Normal
    # exit_type to Local State, so the next launch shows no bubble.
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

atexit.register(_cleanup_chromium)

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

def launch_chromium():
    global _chromium_proc, _auto_launched
    user_dir = f"/tmp/chromium-dbrowser-{UID}"
    os.makedirs(user_dir, exist_ok=True)

    # Pre-seed Default/Preferences. Always (re)write so the file is
    # always in a "clean exit" state when chromium starts.
    default_dir = os.path.join(user_dir, "Default")
    os.makedirs(default_dir, exist_ok=True)
    prefs_path = os.path.join(default_dir, "Preferences")
    prefs = {
        "profile": {"exit_type": "Normal", "exited_cleanly": True},
        "session": {"restore_on_startup": 4},
        "browser": {"has_seen_welcome_page": True},
    }
    try:
        with open(prefs_path, "w") as f:
            json.dump(prefs, f)
    except OSError:
        pass

    # Pre-seed Local State. Always rewrite so chromium never sees a
    # dirty exit from a prior crash/kill.
    local_state_path = os.path.join(user_dir, "Local State")
    default_info = {
        "profile_path": default_dir,
        "is_ephemeral": False,
        "exited_cleanly": True,
    }
    local_state = {
        "profile": {
            "info_cache": {"Default": default_info},
            "last_cleanup_exited_cleanly": True,
            "exited_cleanly": True,
            "last_active_profiles": ["Default"],
        },
        "browser": {
            "has_seen_welcome_page": True,
            "did_show_profile_picker_views": True,
        },
        "session": {"restore_on_startup": 4},
    }
    try:
        with open(local_state_path, "w") as f:
            json.dump(local_state, f)
    except OSError:
        pass

    args = [
        CHROMIUM_BIN,
        f"--remote-debugging-port={CDP_PORT}",
        "--disable-gpu",
        "--no-memcheck",
        "--hide-crash-restore-bubble",     # newer than --disable-session-crashed-bubble
        "--disable-features=InfiniteSessionRestore,SessionRestoreOnStartup",
        "--no-first-run",
        "--no-default-browser-check",
        "--noerrdialogs",
        "--lang=pt-BR",                    # avoid login walls on .com.br sites
        f"--user-data-dir={user_dir}",
        CDP_URL,
    ]
    log_path = "/tmp/chromium-dbrowser.log"
    logf = open(log_path, "ab")
    proc = subprocess.Popen(args, stdout=logf, stderr=logf,
                            stdin=subprocess.DEVNULL,
                            start_new_session=True)
    _chromium_proc = proc
    # wait for CDP up
    for _ in range(40):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(f"http://{CDP_HOST}:{CDP_PORT}/json/version", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            continue
    return False


# --- command handlers ---
def show_help():
    return """
Browser IPC Server (chromium backend)

JSON request:  {"command": ["cmd", "arg1", ...]}
Response:      {"status": "ok", "data": <result>}
               {"status": "error", "message": "..."}

Navigation:
  load-url <url>                - navigate to url
  back                          - history back
  forward                       - history forward
  reload                        - reload current page
  status                        - {url, title, ready, width, height, loading}

Inspection:
  eval-js <code>                - run JS, return value (or description)
  screenshot                    - PNG, base64-encoded
  get-console-output [N]        - last N console lines (default: all, N<0: tail)
  wait-for-selector <css> [t]   - poll until css matches (default 10s, max 60s)
  wait-for-text <substr> [t]    - poll until page body contains text (default 10s, max 60s)

Network:
  list-network-requests [max]   - tracked requests [{id,url,type,method,status_code,...}]
  get-network-request <id>      - single request details (headers, response_headers)

Viewport:
  resize <w> <h>                - set window size (e.g. resize 1280 800)
  device [profile]              - viewport preset; profile is one of:
                                 phone-portrait, phone-landscape,
                                 tablet-portrait, tablet-landscape
                                 aliases: phone/mobile/iphone, tablet/ipad, desktop (no-op)

Identity:
  set-user-agent <ua>           - override User-Agent header
  cookies                       - list all cookies for current page (Network.getCookies)

Advanced:
  cdp <Domain.method> [json]   - raw CDP passthrough; e.g. cdp Page.printToPDF {"landscape":false}
                                 for setting cookies use cdp Network.setCookie {...} or
                                 Network.setCookies {cookies:[...]}

Other:
  help                          - this help
"""


class Server:
    def __init__(self):
        self.cdp = None
        self.width = DBROWSER_WIDTH
        self.height = DBROWSER_HEIGHT
        self._ensure_cdp()
        self._apply_viewport()

    def _ensure_cdp(self):
        if not self.cdp or not self.cdp.is_up():
            self.cdp = CDP(CDP_HOST, CDP_PORT)
            if not self.cdp.is_up():
                if not AUTO_LAUNCH:
                    raise RuntimeError(f"CDP not reachable at {CDP_HOST}:{CDP_PORT}")
                if not launch_chromium():
                    raise RuntimeError("failed to launch chromium")
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
            })
        except Exception:
            pass

    def handle(self, cmd):
        if "command" not in cmd or not cmd["command"]:
            return {"status": "error", "message": "invalid command"}
        args = cmd["command"]
        name = args[0]

        try:
            if name == "help":
                return {"status": "ok", "data": show_help()}

            if name == "status":
                r = self.cdp.send("Runtime.evaluate", {
                    "expression": "JSON.stringify({url: location.href, title: document.title, ready: document.readyState})",
                    "returnByValue": True,
                })
                if "error" in r:
                    return {"status": "error", "message": r["error"].get("message", "cdp error")}
                data = json.loads(r["result"]["result"]["value"])
                data["width"] = self.width
                data["height"] = self.height
                data["loading"] = data.get("ready") != "complete"
                return {"status": "ok", "data": data}

            if name == "load-url":
                if len(args) < 2:
                    return {"status": "error", "message": "load-url requires url"}
                r = self.cdp.send("Page.navigate", {"url": args[1]})
                if "error" in r:
                    return {"status": "error", "message": r["error"].get("message")}
                return {"status": "ok", "data": f"loading {args[1]}"}

            if name == "eval-js":
                if len(args) < 2:
                    return {"status": "error", "message": "eval-js requires code"}
                r = self.cdp.send("Runtime.evaluate", {
                    "expression": args[1],
                    "returnByValue": True,
                    "awaitPromise": True,
                })
                if "error" in r:
                    return {"status": "error", "message": r["error"].get("message", "cdp error")}
                res = r.get("result", {}).get("result", {})
                if "value" in res:
                    return {"status": "ok", "data": res["value"]}
                return {"status": "ok", "data": res.get("description", None)}

            if name == "screenshot":
                r = self.cdp.send("Page.captureScreenshot", {"format": "png"})
                if "error" in r:
                    return {"status": "error", "message": r["error"].get("message")}
                return {"status": "ok", "data": r["result"]["data"]}

            if name == "back":
                self.cdp.send("Page.getNavigationHistory")
                # CDP lacks direct back; emulate with history.go(-1) and current index
                self.cdp.send("Runtime.evaluate", {
                    "expression": "history.back()", "returnByValue": True,
                })
                return {"status": "ok", "data": "back"}

            if name == "forward":
                self.cdp.send("Runtime.evaluate", {
                    "expression": "history.forward()", "returnByValue": True,
                })
                return {"status": "ok", "data": "forward"}

            if name == "reload":
                self.cdp.send("Page.reload", {"ignoreCache": False})
                return {"status": "ok", "data": "reloading"}

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
                    r = self.cdp.send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
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
                    r = self.cdp.send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
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
                r = self.cdp.send("Network.setUserAgentOverride", {"userAgent": ua})
                if "error" in r:
                    return {"status": "error", "message": r["error"].get("message")}
                return {"status": "ok", "data": f"ua set: {ua[:60]}"}

            if name == "cookies":
                r = self.cdp.send("Network.getCookies", {})
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
                r = self.cdp.send(method, params)
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
        text = data.decode("utf-8", errors="replace").strip()
        if text == "help":
            resp = {"status": "ok", "data": show_help()}
        else:
            try:
                cmd = json.loads(text)
                resp = SERVER.handle(cmd)
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
    # kills the process before atexit runs and leaves chromium orphan.
    def _shutdown(_signo, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(f"chromium-server listening on {SOCKET_PATH}")
    print(f"CDP: {CDP_HOST}:{CDP_PORT}  auto-launch: {AUTO_LAUNCH}")
    print(f"Test: echo '{{\"command\":[\"help\"]}}' | nc -U {SOCKET_PATH}")

    try:
        while True:
            r, _, _ = select.select([sock], [], [], 1.0)
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
        # Only close chromium if we started it. If the user has a
        # manually-launched chromium on the CDP port, leave it alone.
        if _auto_launched:
            _cleanup_chromium()


if __name__ == "__main__":
    main()
