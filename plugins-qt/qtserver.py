# ruff: noqa: F821
# plugins/qtserver.py — IPC server plugin for qtbrowser.py
# Runs after QApplication and window setup, has access to: app, win, web, page, profile, etc.
import os
import socket
import select
import json
import base64
import time
from io import BytesIO

# ── Config ──────────────────────────────────────────────────────────────
DBROWSER_TIMEOUT = float(os.getenv('DBROWSER_TIMEOUT', 10.0))
CONSOLE_BUFFER_SIZE = int(os.getenv('DBROWSER_CONSOLE_BUFFER', 1000))
NETWORK_BUFFER_SIZE = int(os.getenv('DBROWSER_NETWORK_BUFFER', 100))
DEFAULT_SOCKET_PATH = f"/run/user/{os.getuid()}/tmp/dbrowser.sock"
SOCKET_PATH = os.getenv('SOCKET_PATH', DEFAULT_SOCKET_PATH)

DEVICE_PROFILES = {
    'phone-portrait':   (375, 812),
    'phone-landscape':  (812, 375),
    'tablet-portrait':  (768, 1024),
    'tablet-landscape': (1024, 768),
}

# ── Buffers ─────────────────────────────────────────────────────────────
console_buffer = []
network_requests = {}
_request_counter = [0]

# ── Console Capture ─────────────────────────────────────────────────────
def _on_console_message(msg):
    console_buffer.append(str(msg))
    if len(console_buffer) > CONSOLE_BUFFER_SIZE:
        console_buffer.pop(0)

# Inject JS to capture console
run_js('''
(function() {
    const _p = (l, a) => {
        const m = a.map(x => typeof x === 'object' ? (() => { try { return JSON.stringify(x); } catch(e) { return String(x); } })() : String(x)).join(' ');
        console.log(`[${l}] ${m}`);
    };
    const _o = { log: console.log, warn: console.warn, error: console.error };
    console.log = (...a) => { _o.log(...a); _p('log', a); };
    console.warn = (...a) => { _o.warn(...a); _p('warn', a); };
    console.error = (...a) => { _o.error(...a); _p('error', a); };
})();
''')

# Console captured via injected JS that prefixes with [log]/[warn]/[error]

# ── Network Tracking ────────────────────────────────────────────────────
def _on_resource_request(req):
    req_id = f"req_{_request_counter[0]}"
    _request_counter[0] += 1
    url = req.requestUrl()
    network_requests[req_id] = {
        "id": req_id,
        "uri": url.toString() if url else "",
        "method": req.requestMethod() if hasattr(req, 'requestMethod') else "GET",
        "headers": {},
        "status": "loading"
    }
    if len(network_requests) > NETWORK_BUFFER_SIZE:
        oldest = list(network_requests.keys())[0]
        del network_requests[oldest]

def _on_resource_response(req, response):
    req_id = f"req_{_request_counter[0] - 1}"
    if req_id in network_requests:
        network_requests[req_id]["status_code"] = response.statusCode() if hasattr(response, 'statusCode') else 0
        network_requests[req_id]["mime_type"] = response.mimeType() if hasattr(response, 'mimeType') else ""
        network_requests[req_id]["status"] = "complete"

# Try to enable network logging via page profile
try:
    if hasattr(profile, 'setUrlRequestInterceptor'):
        def _intercept(req):
            _on_resource_request(req)
            # Return None to continue normal handling
        profile.setUrlRequestInterceptor(_intercept)
except Exception:
    pass  # May not be available

# ── Device Picker ───────────────────────────────────────────────────────
def select_device(profile_name=None):
    if profile_name and profile_name in DEVICE_PROFILES:
        w, h = DEVICE_PROFILES[profile_name]
        win.resize(w, h)
        return {"status": "ok", "data": f"resized to {profile_name} ({w}x{h})"}
    options = list(DEVICE_PROFILES.keys())
    try:
        from PyQt6.QtWidgets import QInputDialog
        selected, ok = QInputDialog.getItem(win, "Device", "Select device:", options, 0, False)
        if ok and selected in DEVICE_PROFILES:
            w, h = DEVICE_PROFILES[selected]
            win.resize(w, h)
            return {"status": "ok", "data": f"resized to {selected} ({w}x{h})"}
    except Exception:
        pass
    return {"status": "error", "message": "No device selected"}

# ── IPC Commands ────────────────────────────────────────────────────────
def handle_command(cmd):
    if not cmd or 'command' not in cmd:
        return {"status": "error", "message": "Invalid command format"}
    args = cmd['command']
    if not args:
        return {"status": "error", "message": "Empty command"}
    name = args[0]

    if name == 'help':
        return {"status": "ok", "data": '''
IPC Commands:
  help                                      - Show this help
  load-url <url>                            - Load URL
  eval-js <code>                            - Execute JavaScript
  screenshot                                - Return PNG as base64
  back / forward                            - Navigation
  reload                                    - Reload page
  status                                    - Current URL, title, loading state
  get-console-output [lines]               - Console output
  list-network-requests [max]               - Network requests
  get-network-request <id>                  - Request details
  resize <width> <height>                   - Resize window
  maximize / unmaximize                     - Window state
  fullscreen / unfullscreen                 - Fullscreen toggle
  rotate                                    - Swap width/height
  device [profile]                          - Device profile
  set-zoom <factor>                         - Set zoom (e.g., 1.5)
  get-zoom                                  - Get current zoom
'''}

    if name == 'status':
        alloc = win.size()
        return {"status": "ok", "data": {
            "url": web.url().toString() if web.url() else "",
            "title": web.title() or "",
            "loading": page.isLoading(),
            "progress": getattr(web, '_progress', 0),
            "width": alloc.width(),
            "height": alloc.height()
        }}

    if name == 'back':
        web.back()
        return {"status": "ok", "data": "went back"}

    if name == 'forward':
        web.forward()
        return {"status": "ok", "data": "went forward"}

    if name == 'reload':
        web.reload()
        return {"status": "ok", "data": "reloaded"}

    if name == 'load-url':
        if len(args) < 2:
            return {"status": "error", "message": "load-url requires URL argument"}
        web.load(QUrl(args[1]))
        return {"status": "ok", "data": f"loading {args[1]}"}

    if name == 'eval-js':
        if len(args) < 2:
            return {"status": "error", "message": "eval-js requires code argument"}
        js_code = args[1]
        result = [None]
        done = [False]

        def callback(response):
            # PyQt6 can return various types
            try:
                if response is None:
                    result[0] = None
                elif isinstance(response, bool):
                    result[0] = response
                elif isinstance(response, (int, float)):
                    result[0] = response
                elif isinstance(response, str):
                    result[0] = response
                elif hasattr(response, 'toString'):
                    result[0] = response.toString()
                elif hasattr(response, 'value'):
                    result[0] = response.value()
                else:
                    result[0] = str(response)
            except Exception as e:
                result[0] = str(response)
            done[0] = True

        page.runJavaScript(js_code, callback)
        start = time.time()
        while not done[0] and (time.time() - start) < DBROWSER_TIMEOUT:
            app.processEvents()
        return {"status": "ok", "data": result[0]}

    if name == 'screenshot':
        pixmap = web.grab()
        from PyQt6.QtCore import QBuffer, QByteArray
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QBuffer.OpenModeFlag.WriteOnly)
        pixmap.save(buf, "PNG")
        b64 = base64.b64encode(bytes(ba)).decode('utf-8')
        return {"status": "ok", "data": b64}

    if name == 'get-console-output':
        lines = int(args[1]) if len(args) > 1 else None
        if lines is None:
            output = console_buffer.copy()
        elif lines < 0:
            output = console_buffer[lines:]
        else:
            output = console_buffer[:lines]
        return {"status": "ok", "data": output}

    if name == 'list-network-requests':
        max_reqs = int(args[1]) if len(args) > 1 else None
        reqs = list(network_requests.values())
        if max_reqs and max_reqs > 0:
            reqs = reqs[:max_reqs]
        return {"status": "ok", "data": reqs}

    if name == 'get-network-request':
        if len(args) < 2:
            return {"status": "error", "message": "get-network-request requires id argument"}
        req_id = args[1]
        if req_id in network_requests:
            return {"status": "ok", "data": network_requests[req_id]}
        return {"status": "error", "message": f"Request {req_id} not found"}

    if name == 'resize':
        if len(args) < 3:
            return {"status": "error", "message": "resize requires width and height"}
        try:
            w, h = int(args[1]), int(args[2])
            if w <= 0 or h <= 0:
                return {"status": "error", "message": "width and height must be positive"}
            win.resize(w, h)
            return {"status": "ok", "data": f"resized to {w}x{h}"}
        except ValueError:
            return {"status": "error", "message": "width and height must be integers"}

    if name == 'maximize':
        win.showMaximized()
        return {"status": "ok", "data": "window maximized"}

    if name == 'unmaximize':
        win.showNormal()
        return {"status": "ok", "data": "window restored"}

    if name == 'fullscreen':
        win.showFullScreen()
        return {"status": "ok", "data": "entered fullscreen"}

    if name == 'unfullscreen':
        win.showNormal()
        return {"status": "ok", "data": "exited fullscreen"}

    if name == 'rotate':
        size = win.size()
        win.resize(size.height(), size.width())
        return {"status": "ok", "data": f"rotated to {size.height()}x{size.width()}"}

    if name == 'device':
        profile_name = args[1] if len(args) > 1 else None
        return select_device(profile_name)

    if name == 'set-zoom':
        if len(args) < 2:
            return {"status": "error", "message": "set-zoom requires factor argument"}
        try:
            factor = float(args[1])
            web.setZoomFactor(factor)
            return {"status": "ok", "data": f"zoom set to {factor}"}
        except ValueError:
            return {"status": "error", "message": "invalid zoom factor"}

    if name == 'get-zoom':
        return {"status": "ok", "data": web.zoomFactor()}

    return {"status": "error", "message": f"Unknown command: {name}"}

# ── Socket Server ───────────────────────────────────────────────────────
def handle_client(sock, cond):
    conn, _ = sock.accept()
    conn.setblocking(False)
    data = b''
    # Read data with multiple recv calls to ensure we get all
    for _ in range(10):  # Try up to 10 times
        try:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
            # Check if we have a complete JSON (ends with newline or has complete data)
            try:
                text = data.decode('utf-8').strip()
                json.loads(text)
                break  # Complete JSON, exit loop
            except:
                pass  # Not complete yet, keep reading
        except BlockingIOError:
            break
        except Exception:
            break

    if data:
        text = data.decode('utf-8').strip()
        if text == 'help':
            response = handle_command({'command': ['help']})
        else:
            try:
                cmd = json.loads(text)
                response = handle_command(cmd)
            except json.JSONDecodeError:
                response = {"status": "error", "message": "Invalid JSON"}
        resp_data = json.dumps(response).encode('utf-8') + b'\n'
        try:
            conn.sendall(resp_data)
        except:
            pass
    conn.close()
    return True

# Cleanup
def cleanup():
    try:
        os.remove(SOCKET_PATH)
    except:
        pass

# Setup socket
_sock_active = False
if os.path.exists(SOCKET_PATH):
    test_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        test_sock.connect(SOCKET_PATH)
        print(f"Socket already active: {SOCKET_PATH}")
    except:
        try:
            os.remove(SOCKET_PATH)
        except:
            pass
        _sock_active = True
    test_sock.close()
else:
    _sock_active = True

if _sock_active:
    os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
    _server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    _server_sock.bind(SOCKET_PATH)
    _server_sock.listen(5)
    _server_sock.setblocking(False)
    from PyQt6.QtCore import QSocketNotifier
    _notifier = QSocketNotifier(_server_sock.fileno(), QSocketNotifier.Type.Read, app)
    _notifier.activated.connect(lambda fd: handle_client(_server_sock, None))

    print(f"IPC server listening on {SOCKET_PATH}")
    print(f"Test: echo '{{\"command\": [\"help\"]}}' | nc -U {SOCKET_PATH}")