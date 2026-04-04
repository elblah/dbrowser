# ruff: noqa: F821
# plugins/server.py — IPC server plugin for browser.py
# Runs in browser's global scope — has access to web, win, ctx, etc.
import atexit
import socket
import select
import json
import base64
import time
import io

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
_ucm = web.get_user_content_manager()
try:
    _ucm.register_script_message_handler('server_console')
except Exception:
    pass  # already registered

def _on_console_message(_ucm, result):
    message = result.get_js_value().to_string()
    console_buffer.append(message)
    if len(console_buffer) > CONSOLE_BUFFER_SIZE:
        console_buffer.pop(0)

_ucm.connect('script-message-received::server_console', _on_console_message)

_ucm.add_script(WebKit2.UserScript('''
(function() {
    const _p = (l, a) => {
        const m = a.map(x => typeof x === 'object' ? (() => { try { return JSON.stringify(x); } catch(e) { return String(x); } })() : String(x)).join(' ');
        window.webkit.messageHandlers.server_console.postMessage(`[${l}] ${m}`);
    };
    const _o = { log: console.log, warn: console.warn, error: console.error };
    console.log = (...a) => { _o.log(...a); _p('log', a); };
    console.warn = (...a) => { _o.warn(...a); _p('warn', a); };
    console.error = (...a) => { _o.error(...a); _p('error', a); };
})();
''', 0, 0, None, None))

# ── Network Tracking ────────────────────────────────────────────────────
def _on_resource_load_started(webview, resource, request):
    req_id = f"req_{_request_counter[0]}"
    _request_counter[0] += 1
    uri = resource.get_uri()
    req_headers = {}
    if hasattr(request, 'get_http_headers'):
        headers = request.get_http_headers()
        if headers:
            headers.foreach(lambda k, v: req_headers.update({k: v}))
    network_requests[req_id] = {
        "id": req_id, "uri": uri,
        "method": request.get_http_method() if hasattr(request, 'get_http_method') else "GET",
        "headers": req_headers, "response_headers": {}, "status": "loading"
    }
    if len(network_requests) > NETWORK_BUFFER_SIZE:
        oldest = list(network_requests.keys())[0]
        del network_requests[oldest]

    def _on_finished(resource):
        try:
            response = resource.get_response()
            if response and req_id in network_requests:
                network_requests[req_id]["status_code"] = response.get_status_code()
                network_requests[req_id]["mime_type"] = response.get_mime_type() or ""
                network_requests[req_id]["status"] = "complete"
                if hasattr(response, 'get_http_headers'):
                    resp_headers = {}
                    headers = response.get_http_headers()
                    if headers:
                        headers.foreach(lambda k, v: resp_headers.update({k: v}))
                    network_requests[req_id]["response_headers"] = resp_headers
        except Exception:
            pass
    resource.connect("finished", _on_finished)

web.connect("resource-load-started", _on_resource_load_started)

# ── Device Picker ───────────────────────────────────────────────────────
def select_device(profile=None):
    if profile and profile in DEVICE_PROFILES:
        w, h = DEVICE_PROFILES[profile]
        win.resize(w, h)
        return {"status": "ok", "data": f"resized to {profile} ({w}x{h})"}
    options = '\n'.join(DEVICE_PROFILES.keys())
    try:
        result = subprocess.run(
            ['rofi', '-dmenu', '-p', 'Device', '-i'],
            input=options, capture_output=True, text=True, timeout=30
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"status": "error", "message": "rofi not found"}
    selected = result.stdout.strip()
    if selected and selected in DEVICE_PROFILES:
        w, h = DEVICE_PROFILES[selected]
        win.resize(w, h)
        return {"status": "ok", "data": f"resized to {selected} ({w}x{h})"}
    return {"status": "error", "message": "No device selected"}

def _on_plugin_key(widget, event):
    if (event.state & Gdk.ModifierType.CONTROL_MASK and
        event.state & Gdk.ModifierType.SHIFT_MASK and
        event.keyval == Gdk.KEY_D):
        select_device()
        return True
    return False

win.connect('key-press-event', _on_plugin_key)

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
  status                                    - Current URL, title, loading state
  get-console-output [lines]                - Console output
  list-network-requests [max]               - Network requests
  get-network-request <id>                  - Request details
  resize <width> <height>                   - Resize window
  maximize / unmaximize                     - Window state
  fullscreen / unfullscreen                 - Fullscreen toggle
  rotate                                    - Swap width/height
  device [profile]                          - Device profile
'''}

    if name == 'status':
        alloc = win.get_allocation()
        return {"status": "ok", "data": {
            "url": web.get_uri() or "",
            "title": web.get_title() or "",
            "loading": web.is_loading(),
            "progress": web.get_estimated_load_progress(),
            "width": alloc.width,
            "height": alloc.height
        }}

    if name == 'back':
        web.go_back()
        return {"status": "ok", "data": "went back"}

    if name == 'forward':
        web.go_forward()
        return {"status": "ok", "data": "went forward"}

    if name == 'load-url':
        if len(args) < 2:
            return {"status": "error", "message": "load-url requires URL argument"}
        web.load_uri(args[1])
        return {"status": "ok", "data": f"loading {args[1]}"}

    if name == 'eval-js':
        if len(args) < 2:
            return {"status": "error", "message": "eval-js requires code argument"}
        js_code = args[1]
        result = [None]
        error = [None]
        done = [False]
        def on_result(webview, res, data):
            try:
                js_result = webview.run_javascript_finish(res)
                if js_result:
                    js_value = js_result.get_js_value()
                    if js_value.is_string():
                        result[0] = js_value.to_string()
                    elif js_value.is_number():
                        result[0] = js_value.to_double()
                    elif js_value.is_boolean():
                        result[0] = js_value.to_boolean()
                    elif js_value.is_null():
                        result[0] = None
                    else:
                        result[0] = js_value.to_string()
            except Exception as e:
                error[0] = str(e)
            finally:
                done[0] = True
        web.run_javascript(js_code, None, on_result, None)
        start = time.time()
        while not done[0] and (time.time() - start) < DBROWSER_TIMEOUT:
            Gtk.main_iteration_do(False)
        if error[0]:
            return {"status": "error", "message": error[0]}
        return {"status": "ok", "data": result[0]}

    if name == 'screenshot':
        result = [None]
        done = [False]
        def on_snapshot(webview, res, data):
            try:
                surface = webview.get_snapshot_finish(res)
                buf = io.BytesIO()
                surface.write_to_png(buf)
                result[0] = base64.b64encode(buf.getvalue()).decode('utf-8')
            except Exception:
                pass
            finally:
                done[0] = True
        web.get_snapshot(WebKit2.SnapshotRegion.FULL_DOCUMENT,
                        WebKit2.SnapshotOptions.NONE, None, on_snapshot, None)
        start = time.time()
        while not done[0] and (time.time() - start) < DBROWSER_TIMEOUT:
            Gtk.main_iteration_do(False)
        if result[0]:
            return {"status": "ok", "data": result[0]}
        return {"status": "error", "message": "Failed to capture screenshot"}

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
            width, height = int(args[1]), int(args[2])
            if width <= 0 or height <= 0:
                return {"status": "error", "message": "width and height must be positive"}
            win.resize(width, height)
            return {"status": "ok", "data": f"resized to {width}x{height}"}
        except ValueError:
            return {"status": "error", "message": "width and height must be integers"}

    if name == 'maximize':
        win.maximize()
        return {"status": "ok", "data": "window maximized"}

    if name == 'unmaximize':
        win.unmaximize()
        return {"status": "ok", "data": "window restored"}

    if name == 'fullscreen':
        win.fullscreen()
        return {"status": "ok", "data": "entered fullscreen"}

    if name == 'unfullscreen':
        win.unfullscreen()
        return {"status": "ok", "data": "exited fullscreen"}

    if name == 'rotate':
        alloc = win.get_allocation()
        win.resize(alloc.height, alloc.width)
        return {"status": "ok", "data": f"rotated to {alloc.width}x{alloc.height}"}

    if name == 'device':
        profile = args[1] if len(args) > 1 else None
        return select_device(profile)

    return {"status": "error", "message": f"Unknown command: {name}"}

# ── Socket Server ───────────────────────────────────────────────────────
def _handle_client(sock, cond):
    conn, _ = sock.accept()
    conn.setblocking(False)
    try:
        data = b''
        while True:
            ready, _, _ = select.select([conn], [], [], 0.5)
            if ready:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b'\n' in data:
                    break
            elif data:
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
                    response = {"status": "error", "message": "Invalid JSON. Use 'help' for usage."}
            conn.setblocking(True)
            conn.sendall(json.dumps(response).encode('utf-8'))
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()
    return True

_server_sock = None

def _cleanup():
    if _server_sock:
        _server_sock.close()
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

atexit.register(_cleanup)

# Setup socket
_sock_active = False
if os.path.exists(SOCKET_PATH):
    test_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        test_sock.connect(SOCKET_PATH)
        test_sock.close()
        print(f"Warning: Socket {SOCKET_PATH} is already in use — IPC disabled", file=sys.stderr)
    except (ConnectionRefusedError, FileNotFoundError):
        os.unlink(SOCKET_PATH)
        _sock_active = True
    finally:
        test_sock.close()
else:
    _sock_active = True

if _sock_active:
    os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
    _server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    _server_sock.bind(SOCKET_PATH)
    _server_sock.listen(5)
    _server_sock.setblocking(False)
    GLib.io_add_watch(_server_sock, GLib.IO_IN, _handle_client)
    print(f"IPC server listening on {SOCKET_PATH}")
    print(f"Buffers: console={CONSOLE_BUFFER_SIZE}, network={NETWORK_BUFFER_SIZE}")
    print(f"Test: echo '{{\"command\": [\"help\"]}}' | nc -U {SOCKET_PATH}")
