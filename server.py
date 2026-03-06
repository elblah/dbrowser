#!/usr/bin/env python3
"""Browser IPC server - controlled via Unix socket."""
import os
import sys
import socket
import json
import base64
import warnings
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
from gi.repository import WebKit2, Gtk, GLib  # noqa: E402

# Default socket path
DEFAULT_SOCKET_PATH = f"/run/user/{os.getuid()}/tmp/dbrowser.sock"
SOCKET_PATH = os.getenv('SOCKET_PATH', DEFAULT_SOCKET_PATH)

# Buffer sizes
CONSOLE_BUFFER_SIZE = int(os.getenv('DBROWSER_CONSOLE_BUFFER', 1000))
NETWORK_BUFFER_SIZE = int(os.getenv('DBROWSER_NETWORK_BUFFER', 100))

def show_help():
    """Return help text for AI clients."""
    return '''
Browser IPC Server Commands:

All commands sent as JSON: {"command": ["cmd", "arg1", "arg2", ...]}
Responses are JSON: {"status": "ok", "data": ...} or {"status": "error", "message": ...}

Commands:
  help                                      - Show this help
  load-url <url>                            - Load URL in browser
  eval-js <code>                            - Execute JavaScript, return result
  screenshot                                - Return PNG as base64 string
  back                                      - Go back in history
  forward                                   - Go forward in history
  status                                    - Get current URL, title, loading state
  get-console-output [lines]                - Get console output (default: all, negative: last N)
  list-network-requests [max]               - List network requests (default: all)
  get-network-request <id>                  - Get details of a network request

Examples:
  echo \'{"command": ["help"]}\' | nc -U /run/user/1000/tmp/dbrowser.sock
  echo \'{"command": ["load-url", "https://example.com"]}\' | nc -U /run/user/1000/tmp/dbrowser.sock
  echo \'{"command": ["eval-js", "document.title"]}\' | nc -U /run/user/1000/tmp/dbrowser.sock
'''

# Console buffer
console_buffer = []

# Network requests buffer
network_requests = {}

# Initialize GTK and WebKit
ctx = WebKit2.WebContext.get_default()
cookie_manager = ctx.get_cookie_manager()
cookie_manager.set_accept_policy(WebKit2.CookieAcceptPolicy.NO_THIRD_PARTY)

win = Gtk.Window()
win.set_default_size(800, 600)
web = WebKit2.WebView()
settings = web.get_settings()
settings.set_enable_developer_extras(True)
settings.set_user_agent('Mozilla/5.0')
settings.set_allow_file_access_from_file_urls(False)
settings.set_allow_universal_access_from_file_urls(False)
win.add(web)
win.show_all()

# Setup console capture
user_content = web.get_user_content_manager()
user_content.register_script_message_handler('console')

def on_console_message(user_content, result):
    message = result.get_js_value().to_string()
    console_buffer.append(message)
    # Trim buffer if needed
    if len(console_buffer) > CONSOLE_BUFFER_SIZE:
        console_buffer.pop(0)

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

# Network request tracking
request_counter = [0]

def on_resource_load_started(webview, resource, request):
    req_id = f"req_{request_counter[0]}"
    request_counter[0] += 1
    
    uri = resource.get_uri()
    
    # Capture request headers
    req_headers = {}
    if hasattr(request, 'get_http_headers'):
        headers = request.get_http_headers()
        if headers:
            headers.foreach(lambda k, v: req_headers.update({k: v}))
    
    network_requests[req_id] = {
        "id": req_id,
        "uri": uri,
        "method": request.get_http_method() if hasattr(request, 'get_http_method') else "GET",
        "headers": req_headers,
        "response_headers": {},
        "status": "loading"
    }
    
    # Trim if needed
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
                # Capture response headers
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

# Load blank page initially
web.load_uri('about:blank')

def handle_command(cmd):
    """Process a command and return response dict."""
    if not cmd or 'command' not in cmd:
        return {"status": "error", "message": "Invalid command format"}
    
    args = cmd['command']
    if not args:
        return {"status": "error", "message": "Empty command"}
    
    name = args[0]
    
    if name == 'help':
        return {"status": "ok", "data": show_help()}
    
    if name == 'status':
        return {"status": "ok", "data": {
            "url": web.get_uri() or "",
            "title": web.get_title() or "",
            "loading": web.is_loading(),
            "progress": web.get_estimated_load_progress()
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
        url = args[1]
        web.load_uri(url)
        return {"status": "ok", "data": f"loading {url}"}
    
    if name == 'eval-js':
        if len(args) < 2:
            return {"status": "error", "message": "eval-js requires code argument"}
        js_code = args[1]
        # Result will be captured via callback
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
                        # Try to get as string for objects
                        result[0] = js_value.to_string()
            except Exception as e:
                error[0] = str(e)
            finally:
                done[0] = True
        
        web.run_javascript(js_code, None, on_result, None)
        
        # Wait for result (with timeout)
        import time
        timeout = 10.0
        start = time.time()
        while not done[0] and (time.time() - start) < timeout:
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
                import io
                buffer = io.BytesIO()
                surface.write_to_png(buffer)
                result[0] = base64.b64encode(buffer.getvalue()).decode('utf-8')
            except Exception:
                pass
            finally:
                done[0] = True
        
        web.get_snapshot(WebKit2.SnapshotRegion.FULL_DOCUMENT, 
                        WebKit2.SnapshotOptions.NONE, None, on_snapshot, None)
        
        # Wait for result
        import time
        timeout = 10.0
        start = time.time()
        while not done[0] and (time.time() - start) < timeout:
            Gtk.main_iteration_do(False)
        
        if result[0]:
            return {"status": "ok", "data": result[0]}
        return {"status": "error", "message": "Failed to capture screenshot"}
    
    if name == 'get-console-output':
        lines = int(args[1]) if len(args) > 1 else None
        if lines is None:
            output = console_buffer.copy()
        elif lines < 0:
            output = console_buffer[lines:]  # Last N lines
        else:
            output = console_buffer[:lines]  # First N lines
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
    
    return {"status": "error", "message": f"Unknown command: {name}"}

def handle_client(sock, cond):
    """Handle incoming socket connection."""
    conn, _ = sock.accept()
    conn.setblocking(False)
    try:
        # Read all available data (non-blocking)
        import select
        data = b''
        while True:
            ready, _, _ = select.select([conn], [], [], 0.1)
            if ready:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                # Check if we have a complete message (newline terminated or JSON complete)
                if b'\n' in data:
                    break
            else:
                break
        
        if data:
            text = data.decode('utf-8').strip()
            # Accept plain "help" command
            if text == 'help':
                response = {"status": "ok", "data": show_help()}
            else:
                try:
                    cmd = json.loads(text)
                    response = handle_command(cmd)
                except json.JSONDecodeError:
                    response = {"status": "error", "message": "Invalid JSON. Use 'help' for usage."}
            conn.send(json.dumps(response).encode('utf-8'))
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()
    return True

def main():
    # Check if socket already exists and is active
    if os.path.exists(SOCKET_PATH):
        # Try to connect to see if it's active
        test_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            test_sock.connect(SOCKET_PATH)
            test_sock.close()
            print(f"Error: Socket {SOCKET_PATH} is already in use", file=sys.stderr)
            sys.exit(1)
        except (ConnectionRefusedError, FileNotFoundError):
            # Socket exists but not active - remove it
            os.unlink(SOCKET_PATH)
        finally:
            test_sock.close()
    
    # Create socket directory if needed
    os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
    
    # Setup socket
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(SOCKET_PATH)
    sock.listen(5)
    sock.setblocking(False)
    
    # Add socket to GLib main loop
    GLib.io_add_watch(sock, GLib.IO_IN, handle_client)
    
    print(f"Browser server listening on {SOCKET_PATH}")
    print(f"Buffers: console={CONSOLE_BUFFER_SIZE}, network={NETWORK_BUFFER_SIZE}")
    print(f"Test: echo '{{\"command\": [\"help\"]}}' | nc -U {SOCKET_PATH}")
    
    try:
        Gtk.main()
    finally:
        os.unlink(SOCKET_PATH)

if __name__ == '__main__':
    main()
