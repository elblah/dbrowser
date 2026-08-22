#!/usr/bin/env python3
"""
Browser with IPC server — controlled via keyboard AND Unix socket.

Combines the interactive browser (keyboard shortcuts, rofi pickers, save/export)
with the IPC server (JSON commands over Unix domain socket for AI agent control).

Environment Variables:
  DBROWSER_HEADLESS       - Enable native headless mode (true/false, default: false)
  DBROWSER_FULLSCREEN     - Enable fullscreen mode (true/false, default: false)
  DBROWSER_WIDTH / DBROWSER_SIZE - Viewport width or WxH size
  DBROWSER_HEIGHT         - Viewport height in pixels
  DBROWSER_TIMEOUT        - JS execution timeout in seconds (default: 10.0)
  DBROWSER_IDLE_TIMEOUT   - Auto-exit after N seconds of inactivity (default: 0 = disabled)
  DBROWSER_CONSOLE_BUFFER - Max console log lines to retain (default: 1000)
  DBROWSER_NETWORK_BUFFER - Max network requests to track (default: 100)
  DBROWSER_COOKIE_POLICY  - Cookie policy: no_third_party, none, all
  DBROWSER_DOWNLOAD_DIR   - Download directory
  DBROWSER_CACHE_DIR      - Custom cache directory
  DBROWSER_NO_CACHE=1     - Disable disk cache
  DBROWSER_NO_JS=1        - Disable JavaScript
  DBROWSER_NO_IMAGES=1    - Don't load images
  DBROWSER_LOW_MEM=1      - Minimize memory usage
  DBROWSER_MEMORY_LIMIT   - Memory limit in MB
  DBROWSER_FAST=1         - Faster loading
  DBROWSER_WEBGL=1        - Enable WebGL
  DBROWSER_MEDIA=1        - Enable media streaming
  DBROWSER_DRM=1          - Enable DRM/encrypted media
  DBROWSER_DEBUG=1        - Show key events
  DBROWSER_JS_CONSOLE=1   - Log JS console to stdout
  SOCKET_PATH             - Unix socket path (default: /run/user/{uid}/tmp/dbrowser.sock)

Usage:
  python3 browser-server.py [url]

  # Interactive: use keyboard shortcuts
  # IPC: echo '{"command": ["help"]}' | nc -U /run/user/1000/tmp/dbrowser.sock
"""
import sys
import os
import time

def show_help():
    print('''
Usage: browser-server.py <URL>

Keybindings:
  F1              - Show this help
  F11             - Toggle fullscreen
  Ctrl+Q          - Quit
  F5 / Ctrl+R     - Reload page
  F12             - Developer tools
  Ctrl+P          - Print dialog
  Ctrl+Shift+P    - Save page as PDF
  Ctrl+S          - Save page as HTML
  Ctrl+Shift+S    - Save page screenshot as PNG
  Ctrl+L          - Change URL (rofi)
  Ctrl+B          - Open link from bookmarks (rofi)
  Ctrl+G          - Load URL from tmux buffer
  Ctrl+Shift+G    - Load URL from clipboard
  Alt+Left / Alt+H / Alt+,  - Go back
  Alt+Right / Alt+L / Alt+. - Go forward
  Alt+J           - Scroll down
  Alt+K           - Scroll up
  Alt+U           - Page down
  Alt+I           - Page up
  Ctrl+Shift+C    - Copy page text to tmux + clipboard
  Ctrl+Shift+U    - Copy current URL to tmux + clipboard
  Ctrl++          - Zoom in
  Ctrl+-          - Zoom out
  Ctrl+0          - Zoom reset
  Ctrl+F          - Find in page (rofi)
  Ctrl+N          - Find next
  Ctrl+Shift+N    - Find previous
  Ctrl+Shift+Del  - Clear all browsing data
  Ctrl+W          - Toggle new window redirect
  Ctrl+Shift+M    - Rotate (swap width/height)
  Ctrl+Shift+D    - Select device profile (rofi)

IPC Commands (via Unix socket):
  help                                      - Show IPC help
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

Examples:
  echo '{"command": ["help"]}' | nc -U /run/user/1000/tmp/dbrowser.sock
  echo '{"command": ["load-url", "https://example.com"]}' | nc -U ...
  echo '{"command": ["eval-js", "document.title"]}' | nc -U ...
''')

if len(sys.argv) >= 2 and sys.argv[1] in ('-h', '--help'):
    show_help()
    sys.exit(0)

import warnings  # noqa: E402
warnings.filterwarnings('ignore', category=DeprecationWarning, module='gi.repository')
import gi  # noqa: E402
for ver in ('4.1', '4.0'):
    try:
        gi.require_version('WebKit2', ver)
        break
    except ValueError:
        pass
else:
    raise SystemExit('No WebKit2 found')
gi.require_version('Gdk', '3.0')
from gi.repository import WebKit2, Gtk, Gdk, GLib  # noqa: E402

# ── Configuration ──────────────────────────────────────────────────────────
url = sys.argv[1] if len(sys.argv) > 1 else 'about:blank'
debug = os.getenv('DBROWSER_DEBUG')
cache_dir = os.getenv('DBROWSER_CACHE_DIR')
no_cache = os.getenv('DBROWSER_NO_CACHE')
no_js = os.getenv('DBROWSER_NO_JS')
low_mem = os.getenv('DBROWSER_LOW_MEM')
fast = os.getenv('DBROWSER_FAST')
no_images = os.getenv('DBROWSER_NO_IMAGES')
enable_webgl = os.getenv('DBROWSER_WEBGL')
enable_media = os.getenv('DBROWSER_MEDIA')
enable_drm = os.getenv('DBROWSER_DRM')
memory_limit = os.getenv('DBROWSER_MEMORY_LIMIT')
show_js_console = os.getenv('DBROWSER_JS_CONSOLE')
fullscreen = os.getenv('DBROWSER_FULLSCREEN', 'false').lower() in ('true', '1', 'yes')

# Server-specific config
DBROWSER_HEADLESS = os.getenv('DBROWSER_HEADLESS', 'false').lower() in ('true', '1', 'yes')
DBROWSER_TIMEOUT = float(os.getenv('DBROWSER_TIMEOUT', 10.0))

# Idle auto-exit: exit after N seconds of no activity (IPC/UI/click). 0/empty/invalid = disabled.
try:
    DBROWSER_IDLE_TIMEOUT = float(os.getenv('DBROWSER_IDLE_TIMEOUT', 0) or 0)
except (ValueError, TypeError):
    DBROWSER_IDLE_TIMEOUT = 0
if DBROWSER_IDLE_TIMEOUT <= 0:
    DBROWSER_IDLE_TIMEOUT = 0
last_activity = [time.monotonic()]
CONSOLE_BUFFER_SIZE = int(os.getenv('DBROWSER_CONSOLE_BUFFER', 1000))
NETWORK_BUFFER_SIZE = int(os.getenv('DBROWSER_NETWORK_BUFFER', 100))
DBROWSER_COOKIE_POLICY = os.getenv('DBROWSER_COOKIE_POLICY', 'no_third_party')
DEFAULT_SOCKET_PATH = f"/run/user/{os.getuid()}/tmp/dbrowser.sock"
SOCKET_PATH = os.getenv('SOCKET_PATH', DEFAULT_SOCKET_PATH)

# Device profiles
DEVICE_PROFILES = {
    'phone-portrait':   (375, 812),
    'phone-landscape':  (812, 375),
    'tablet-portrait':  (768, 1024),
    'tablet-landscape': (1024, 768),
}

# ── Buffers ────────────────────────────────────────────────────────────────
console_buffer = []
network_requests = {}

# ── WebKit Context ─────────────────────────────────────────────────────────
if memory_limit:
    data_manager = WebKit2.WebsiteDataManager()
    try:
        mem_mb = int(memory_limit)
        mps = WebKit2.MemoryPressureSettings()
        mps.set_memory_limit(mem_mb)
        mps.set_kill_threshold(0.95)
        mps.set_strict_threshold(0.85)
        mps.set_conservative_threshold(0.7)
        data_manager.set_memory_pressure_settings(mps)
    except ValueError:
        pass
    ctx = WebKit2.WebContext.new_with_website_data_manager(data_manager)
else:
    ctx = WebKit2.WebContext.get_default()
    if cache_dir:
        ctx.set_disk_cache_directory(os.path.expanduser(cache_dir))

if no_cache or low_mem:
    ctx.set_cache_model(WebKit2.CacheModel.DOCUMENT_VIEWER)

cookie_manager = ctx.get_cookie_manager()
cookie_policies = {
    'no_third_party': WebKit2.CookieAcceptPolicy.NO_THIRD_PARTY,
    'none': WebKit2.CookieAcceptPolicy.NEVER,
    'all': WebKit2.CookieAcceptPolicy.ALWAYS
}
cookie_manager.set_accept_policy(cookie_policies.get(DBROWSER_COOKIE_POLICY, WebKit2.CookieAcceptPolicy.NO_THIRD_PARTY))

if low_mem:
    ctx.set_process_model(WebKit2.ProcessModel.SHARED_SECONDARY_PROCESS)

# ── Window & WebView ───────────────────────────────────────────────────────
win = Gtk.Window()
# Support both DBROWSER_SIZE (WxH) and DBROWSER_WIDTH/DBROWSER_HEIGHT
size_str = os.getenv('DBROWSER_SIZE')
if size_str:
    w, h = map(int, size_str.split('x'))
else:
    w = int(os.getenv('DBROWSER_WIDTH', 800))
    h = int(os.getenv('DBROWSER_HEIGHT', 600))
win.set_default_size(w, h)

web = WebKit2.WebView()
if DBROWSER_HEADLESS:
    web.set_size_request(w, h)

settings = web.get_settings()
settings.set_enable_developer_extras(True)
settings.set_enable_mediasource(bool(enable_media))
settings.set_enable_media_stream(bool(enable_media))
settings.set_enable_encrypted_media(bool(enable_drm))
settings.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.ON_DEMAND)
settings.set_enable_webgl(False)
settings.set_enable_smooth_scrolling(False)
settings.set_allow_file_access_from_file_urls(False)
settings.set_allow_universal_access_from_file_urls(False)
settings.set_javascript_can_access_clipboard(False)
settings.set_javascript_can_open_windows_automatically(False)
settings.set_user_agent('Mozilla/5.0')

if no_js:
    settings.set_enable_javascript(False)
if low_mem:
    settings.set_enable_page_cache(False)
    settings.set_enable_offline_web_application_cache(False)
    settings.set_enable_html5_database(False)
    settings.set_enable_html5_local_storage(False)
    settings.set_minimum_font_size(10)
if fast:
    settings.set_enable_dns_prefetching(True)
    settings.set_enable_page_cache(True)
if enable_webgl:
    settings.set_enable_webgl(True)
    settings.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.ALWAYS)
if no_images:
    settings.set_auto_load_images(False)

# Native headless mode (WebKitGTK 2.36+)
native_headless_available = False
if DBROWSER_HEADLESS:
    try:
        settings.set_enable_headless_mode(True)
        native_headless_available = True
    except AttributeError:
        print("Warning: Native headless mode not available (requires WebKitGTK 2.36+)", file=sys.stderr)

win.add(web)

# ── Console Capture ────────────────────────────────────────────────────────
user_content = web.get_user_content_manager()
user_content.register_script_message_handler('console')

def on_console_message(user_content, result):
    message = result.get_js_value().to_string()
    console_buffer.append(message)
    if len(console_buffer) > CONSOLE_BUFFER_SIZE:
        console_buffer.pop(0)
    if show_js_console:
        print(f'[JS] {message}')

user_content.connect('script-message-received::console', on_console_message)

console_script = '''
(function() {
    const originalLog = console.log;
    const originalWarn = console.warn;
    const originalError = console.error;
    const sendMessage = (level, args) => {
        const msg = args.map(arg => {
            if (typeof arg === 'object') {
                try { return JSON.stringify(arg); }
                catch (e) { return String(arg); }
            }
            return String(arg);
        }).join(' ');
        window.webkit.messageHandlers.console.postMessage(`[${level}] ${msg}`);
    };
    console.log = (...args) => { originalLog(...args); sendMessage('log', args); };
    console.warn = (...args) => { originalWarn(...args); sendMessage('warn', args); };
    console.error = (...args) => { originalError(...args); sendMessage('error', args); };
})();
'''
user_content.add_script(WebKit2.UserScript(console_script, 0, 0, None, None))

# ── Network Request Tracking ───────────────────────────────────────────────
request_counter = [0]

def on_resource_load_started(webview, resource, request):
    req_id = f"req_{request_counter[0]}"
    request_counter[0] += 1
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

    def on_finished(resource):
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
    resource.connect("finished", on_finished)

web.connect("resource-load-started", on_resource_load_started)

# ── State ──────────────────────────────────────────────────────────────────
is_fullscreen = fullscreen
redirect_new_windows = False
clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
find = web.get_find_controller()
find_text = ['']

# ── Helpers ────────────────────────────────────────────────────────────────
import subprocess  # noqa: E402
import urllib.parse  # noqa: E402
import random  # noqa: E402
import string  # noqa: E402
import json  # noqa: E402
import base64  # noqa: E402
import io  # noqa: E402
import select  # noqa: E402

def clear_browsing_data():
    data_manager = ctx.get_website_data_manager()
    data_manager.clear(
        WebKit2.WebsiteDataTypes.ALL, 0, None,
        lambda obj, result: print('All browsing data cleared')
    )
    print('Cache, cookies, and all site data cleared')

def is_valid_url(text):
    if not text:
        return False
    return text.startswith(('http://', 'https://', 'ftp://', 'file://'))

def get_save_path(title, ext):
    safe_title = ''.join(c if c.isalnum() or c in '-_' else '_' for c in title)
    rand_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=7))
    path = os.path.expanduser(os.getenv('DBROWSER_DOWNLOAD_DIR', '~/Downloads'))
    os.makedirs(path, exist_ok=True)
    return f'{path}/{safe_title}__{rand_suffix}.{ext}'

def rotate_window():
    alloc = win.get_allocation()
    win.resize(alloc.height, alloc.width)

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

# ── IPC Command Handler ────────────────────────────────────────────────────
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
  load-url <url>                            - Load URL in browser
  eval-js <code>                            - Execute JavaScript, return result
  screenshot                                - Return PNG as base64 string
  back                                      - Go back in history
  forward                                   - Go forward in history
  status                                    - Get current URL, title, loading state
  get-console-output [lines]                - Get console output
  list-network-requests [max]               - List network requests
  get-network-request <id>                  - Get details of a network request
  resize <width> <height>                   - Resize the window
  maximize / unmaximize                     - Window state
  fullscreen / unfullscreen                 - Fullscreen toggle
  rotate                                    - Swap width/height
  device [profile]                          - Device profile (phone-portrait, phone-landscape, tablet-portrait, tablet-landscape)
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
                buffer = io.BytesIO()
                surface.write_to_png(buffer)
                result[0] = base64.b64encode(buffer.getvalue()).decode('utf-8')
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
        rotate_window()
        alloc = win.get_allocation()
        return {"status": "ok", "data": f"rotated to {alloc.width}x{alloc.height}"}

    if name == 'device':
        profile = args[1] if len(args) > 1 else None
        return select_device(profile)

    return {"status": "error", "message": f"Unknown command: {name}"}

# ── Socket Server ──────────────────────────────────────────────────────────
def handle_client(sock, cond):
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
            last_activity[0] = time.monotonic()
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

def setup_socket_server():
    if os.path.exists(SOCKET_PATH):
        test_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            test_sock.connect(SOCKET_PATH)
            test_sock.close()
            print(f"Warning: Socket {SOCKET_PATH} is already in use — IPC disabled", file=sys.stderr)
            return None
        except (ConnectionRefusedError, FileNotFoundError):
            os.unlink(SOCKET_PATH)
        finally:
            test_sock.close()

    os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(SOCKET_PATH)
    sock.listen(5)
    sock.setblocking(False)
    GLib.io_add_watch(sock, GLib.IO_IN, handle_client)
    print(f"IPC server listening on {SOCKET_PATH}")
    print(f"Buffers: console={CONSOLE_BUFFER_SIZE}, network={NETWORK_BUFFER_SIZE}")
    print(f"Test: echo '{{\"command\": [\"help\"]}}' | nc -U {SOCKET_PATH}")
    return sock

import socket  # noqa: E402

# ── Keyboard Handler ───────────────────────────────────────────────────────
def on_key(w, e):
    last_activity[0] = time.monotonic()
    if debug:
        print(f'key pressed: keyval={e.keyval}, state={e.state}')

    if e.keyval == Gdk.KEY_F1:
        show_help()
    elif e.keyval == Gdk.KEY_F11:
        global is_fullscreen
        if is_fullscreen:
            win.unfullscreen()
            is_fullscreen = False
            print('Exited fullscreen')
        else:
            win.fullscreen()
            is_fullscreen = True
            print('Entered fullscreen')
    elif e.keyval == Gdk.KEY_q and e.state & Gdk.ModifierType.CONTROL_MASK:
        print('Quitting...')
        Gtk.main_quit()
    elif e.keyval == Gdk.KEY_M and e.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
        rotate_window()
        alloc = win.get_allocation()
        print(f'Rotated to {alloc.height}x{alloc.width}')
    elif e.keyval == Gdk.KEY_D and e.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
        select_device()
    elif e.keyval == Gdk.KEY_F5 or (e.keyval == Gdk.KEY_r and e.state & Gdk.ModifierType.CONTROL_MASK):
        print('Reloading...')
        web.reload()
    elif e.keyval == Gdk.KEY_F12:
        print('Opening inspector...')
        web.get_inspector().show()
    elif e.keyval == Gdk.KEY_p and e.state & Gdk.ModifierType.CONTROL_MASK and not (e.state & Gdk.ModifierType.SHIFT_MASK):
        print('Printing...')
        web.run_javascript('window.print()', None, None)
    elif e.keyval == Gdk.KEY_P and e.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
        def on_title(wv, result, data):
            title = wv.run_javascript_finish(result).get_js_value().to_string() or 'page'
            dest = get_save_path(title, 'pdf')
            print_op = WebKit2.PrintOperation.new(web)
            settings = Gtk.PrintSettings()
            settings.set_printer('Print to File')
            settings.set('output-uri', f'file://{dest}')
            settings.set('output-file-format', 'pdf')
            print_op.set_print_settings(settings)
            print_op.print_()
            print(f'Saving PDF to {dest} ...')
        print('Saving PDF...')
        web.run_javascript('document.title', None, on_title, None)
    elif e.keyval == Gdk.KEY_s and e.state & Gdk.ModifierType.CONTROL_MASK and not (e.state & Gdk.ModifierType.SHIFT_MASK):
        def on_title(wv, result, data):
            title = wv.run_javascript_finish(result).get_js_value().to_string() or 'page'
            dest = get_save_path(title, 'html')
            def on_save_finished(wv, result, data):
                stream = wv.save_finish(result)
                data = stream.read_bytes(10 * 1024 * 1024, None)
                html = data.get_data().decode('utf-8')
                with open(dest, 'w') as f:
                    f.write(html)
                print(f'Saved HTML to {dest}')
            web.save(WebKit2.SaveMode.MHTML, None, on_save_finished, None)
        print('Saving HTML...')
        web.run_javascript('document.title', None, on_title, None)
    elif e.keyval == Gdk.KEY_S and e.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
        def on_title(wv, result, data):
            title = wv.run_javascript_finish(result).get_js_value().to_string() or 'page'
            dest = get_save_path(title, 'png')
            def on_snapshot_finished(wv, result, data):
                surface = wv.get_snapshot_finish(result)
                surface.write_to_png(dest)
                print(f'Saved screenshot to {dest}')
            web.get_snapshot(WebKit2.SnapshotRegion.FULL_DOCUMENT, WebKit2.SnapshotOptions.NONE, None, on_snapshot_finished, None)
        print('Saving screenshot...')
        web.run_javascript('document.title', None, on_title, None)
    elif e.keyval == Gdk.KEY_C and e.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
        print('Copying page text to tmux and clipboard...')
        def copy_text(wv, result, data):
            text = wv.run_javascript_finish(result).get_js_value().to_string()
            subprocess.run(['tmux', 'set-buffer', text], check=False)
            clipboard.set_text(text, -1)
            print(f'Copied {len(text)} chars to tmux and clipboard')
        web.run_javascript('document.body.innerText', None, copy_text, None)
    elif e.keyval == Gdk.KEY_g and e.state & Gdk.ModifierType.CONTROL_MASK and not (e.state & Gdk.ModifierType.SHIFT_MASK):
        url_text = subprocess.run(['tmux', 'show-buffer'], capture_output=True, text=True).stdout.strip()
        if is_valid_url(url_text):
            print(f'Loading from tmux buffer: {url_text}')
            web.load_uri(url_text)
        else:
            print(f'Tmux buffer is not a valid URL: {url_text[:50] if url_text else "(empty)"}...')
    elif e.keyval == Gdk.KEY_G and e.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
        url_text = clipboard.wait_for_text()
        if is_valid_url(url_text):
            print(f'Loading from clipboard: {url_text}')
            web.load_uri(url_text)
        else:
            print(f'Clipboard is not a valid URL: {url_text[:50] if url_text else "(empty)"}...')
    elif e.keyval == Gdk.KEY_l and e.state & Gdk.ModifierType.CONTROL_MASK and not (e.state & Gdk.ModifierType.SHIFT_MASK):
        print('Opening rofi for URL...')
        new_url = subprocess.run(['rofi', '-dmenu', '-p', 'URL', '-i'], input=web.get_uri(),
                                 capture_output=True, text=True).stdout.strip()
        if new_url:
            print(f'Navigating to: {new_url}')
            web.load_uri(new_url)
    elif e.keyval == Gdk.KEY_b and e.state & Gdk.ModifierType.CONTROL_MASK:
        links_path = os.getenv('BOOKMARKS_FILE') or os.path.expanduser('~/data/links.txt')
        try:
            with open(links_path) as f:
                links = f.read()
        except FileNotFoundError:
            print(f'Links file not found: {links_path}')
            return
        selected = subprocess.run(['rofi', '-dmenu', '-i', '-l', '20', '-p', 'Open link:'],
                                  input=links, capture_output=True, text=True).stdout.strip()
        if selected:
            print(f'Opening: {selected}')
            web.load_uri(selected)
    elif e.keyval in (Gdk.KEY_Left, Gdk.KEY_h, Gdk.KEY_comma) and e.state & Gdk.ModifierType.MOD1_MASK:
        print('Going back...')
        web.go_back()
    elif e.keyval in (Gdk.KEY_Right, Gdk.KEY_l, Gdk.KEY_period) and e.state & Gdk.ModifierType.MOD1_MASK:
        print('Going forward...')
        web.go_forward()
    elif e.keyval == Gdk.KEY_j and e.state & Gdk.ModifierType.MOD1_MASK:
        web.run_javascript('window.scrollBy(0, 100)', None, None)
    elif e.keyval == Gdk.KEY_k and e.state & Gdk.ModifierType.MOD1_MASK:
        web.run_javascript('window.scrollBy(0, -100)', None, None)
    elif e.keyval == Gdk.KEY_u and e.state & Gdk.ModifierType.MOD1_MASK:
        web.run_javascript('window.scrollBy(0, window.innerHeight)', None, None)
    elif e.keyval == Gdk.KEY_i and e.state & Gdk.ModifierType.MOD1_MASK:
        web.run_javascript('window.scrollBy(0, -window.innerHeight)', None, None)
    elif e.keyval == Gdk.KEY_U and e.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
        url_text = web.get_uri()
        subprocess.run(['tmux', 'set-buffer', url_text], check=False)
        clipboard.set_text(url_text, -1)
        print(f'Copied URL to tmux and clipboard: {url_text}')
    elif e.keyval in (Gdk.KEY_plus, Gdk.KEY_equal) and e.state & Gdk.ModifierType.CONTROL_MASK:
        web.set_zoom_level(web.get_zoom_level() + 0.1)
        print(f'Zoom: {web.get_zoom_level():.1f}')
    elif e.keyval == Gdk.KEY_minus and e.state & Gdk.ModifierType.CONTROL_MASK:
        web.set_zoom_level(web.get_zoom_level() - 0.1)
        print(f'Zoom: {web.get_zoom_level():.1f}')
    elif e.keyval == Gdk.KEY_0 and e.state & Gdk.ModifierType.CONTROL_MASK:
        web.set_zoom_level(1.0)
        print('Zoom: 1.0')
    elif e.keyval == Gdk.KEY_f and e.state & Gdk.ModifierType.CONTROL_MASK:
        search = subprocess.run(['rofi', '-dmenu', '-p', 'Find', '-i'], input=find_text[0],
                                capture_output=True, text=True).stdout.strip()
        if search:
            find_text[0] = search
            find.search(search, WebKit2.FindOptions.CASE_INSENSITIVE, 9999)
            print(f'Searching: {search}')
    elif e.keyval == Gdk.KEY_n and e.state & Gdk.ModifierType.CONTROL_MASK and not (e.state & Gdk.ModifierType.SHIFT_MASK):
        find.search_next()
        print('Find next')
    elif e.keyval == Gdk.KEY_N and e.state & Gdk.ModifierType.CONTROL_MASK:
        find.search_previous()
        print('Find previous')
    elif e.keyval == Gdk.KEY_Delete and e.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
        print('Clearing all browsing data...')
        clear_browsing_data()
    elif e.keyval == Gdk.KEY_w and e.state & Gdk.ModifierType.CONTROL_MASK:
        global redirect_new_windows
        redirect_new_windows = not redirect_new_windows
        status = 'ON' if redirect_new_windows else 'OFF'
        print(f'New window redirect: {status}')
    else:
        return False
    return True

def on_load_changed(webview, load_event):
    """Reset idle timer on top-level page load/navigation."""
    last_activity[0] = time.monotonic()

def on_button_press(widget, event):
    """Reset idle timer on any mouse click in the browser view."""
    last_activity[0] = time.monotonic()
    return False

def _check_idle():
    """Auto-exit if no IPC/UI activity for DBROWSER_IDLE_TIMEOUT seconds."""
    if DBROWSER_IDLE_TIMEOUT > 0 and (time.monotonic() - last_activity[0]) >= DBROWSER_IDLE_TIMEOUT:
        print(f"idle timeout {DBROWSER_IDLE_TIMEOUT:.0f}s reached - auto-exiting")
        Gtk.main_quit()
        return False
    return True

# ── Downloads & Policy ─────────────────────────────────────────────────────
def on_download(ctx, download):
    def on_decide_destination(d, suggested):
        path = os.path.expanduser(os.getenv('DBROWSER_DOWNLOAD_DIR', '~/Downloads'))
        os.makedirs(path, exist_ok=True)
        dest = path + '/' + (suggested or 'download')
        d.set_destination('file://' + urllib.parse.quote(dest))
        print(f'Saving file to {dest} ...')
        d.connect('finished', lambda dl: print(f'File {dest} saved'))
        return True
    download.connect('decide-destination', on_decide_destination)

def on_decide_policy(webview, decision, decision_type):
    if decision_type == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
        action = decision.get_navigation_action()
        uri = None
        if action:
            request = action.get_request()
            if request:
                uri = request.get_uri()
        if redirect_new_windows and uri:
            print(f'New window request redirected to: {uri}')
            webview.load_uri(uri)
        elif uri:
            print(f'New window blocked: {uri} (press Ctrl+W to enable)')
        else:
            print('New window request blocked (press Ctrl+W to enable)')
        decision.ignore()
        return True
    return False

WebKit2.WebContext.get_default().connect('download-started', on_download)
web.connect('decide-policy', on_decide_policy)

# ── Title & Progress ───────────────────────────────────────────────────────
def update_title():
    title = web.get_title() or 'Loading...'
    progress = web.get_estimated_load_progress()
    if progress < 1.0:
        win.set_title(f'{title} - dbrowser ({int(progress * 100)}%)')
    else:
        win.set_title(f'{title} - dbrowser')

web.connect('notify::title', lambda wv, pspec: update_title())
web.connect('notify::estimated-load-progress', lambda wv, pspec: update_title())
web.connect('load-changed', on_load_changed)
web.connect('button-press-event', on_button_press)

# ── Main ───────────────────────────────────────────────────────────────────
win.connect('destroy', Gtk.main_quit)
win.connect('key-press-event', on_key)
win.set_title('dbrowser')

if is_fullscreen:
    win.fullscreen()

if native_headless_available:
    win.hide()

win.show_all()
web.load_uri(url)

# Start socket server
server_sock = setup_socket_server()

# Idle auto-exit
if DBROWSER_IDLE_TIMEOUT > 0:
    GLib.timeout_add_seconds(10, _check_idle)

try:
    Gtk.main()
finally:
    if server_sock:
        server_sock.close()
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
