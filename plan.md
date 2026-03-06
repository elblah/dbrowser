# dbrowser-server Plan

## Purpose

A daemon that allows AI agents to control a WebKit2 browser via IPC socket.
No Chromium, no Node.js, no bloat.

---

## Architecture

```
┌─────────────┐                          ┌─────────────┐
│  AI / CLI   │ ◄──── Unix socket ─────► │  dbrowser   │
│  (client)   │   /run/user/1000/tmp/... │   server    │
└─────────────┘                          └──────┬──────┘
                                                │
                                         ┌──────▼──────┐
                                         │ WebKit2 GTK │
                                         │   WebView   │
                                         └─────────────┘
```

---

## Paths

| Resource | Path |
|----------|------|
| Socket | `/run/user/1000/tmp/dbrowser.sock` |
| Screenshot | `/run/user/1000/tmp/browser-screenshot.png` |

Both accessible from inside and outside bwrap sandbox.

---

## Protocol (mpv-style JSON IPC)

**Request format:**
```json
{"command": ["command_name", "param1", "param2"]}
```

**Success response:**
```json
{"error": "success", "data": <result>}
```

**Error response:**
```json
{"error": "error message", "data": null}
```

**Rules:**
- Each message terminated with `\n` (newline)
- Sync only: send command → wait for response → send next command
- No request_id (no async, no events, no multiplexing)
- UTF-8 encoding
- If first char is not `{`, treat as plain text command (no response)

---

## Commands

### Navigation

| Command | Example | Response |
|---------|---------|----------|
| `navigate` | `["navigate", "https://example.com"]` | `{"url": "...", "title": "..."}` |
| `back` | `["back"]` | `{"url": "..."}` |
| `forward` | `["forward"]` | `{"url": "..."}` |
| `reload` | `["reload"]` | `{"url": "..."}` |

### Content

| Command | Example | Response |
|---------|---------|----------|
| `get_url` | `["get_url"]` | `"https://example.com"` |
| `get_title` | `["get_title"]` | `"Page Title"` |
| `get_content` | `["get_content"]` | `{"text": "...", "html": "..."}` |

### JavaScript

| Command | Example | Response |
|---------|---------|----------|
| `eval_js` | `["eval_js", "document.title"]` | JS result (stringified) |

### Screenshots

| Command | Example | Response |
|---------|---------|----------|
| `screenshot` | `["screenshot"]` | `{"path": "/run/user/1000/tmp/browser-screenshot.png"}` |

### Network Tracking

| Command | Example | Response |
|---------|---------|----------|
| `start_network_tracking` | `["start_network_tracking"]` | `{}` |
| `stop_network_tracking` | `["stop_network_tracking"]` | `{}` |
| `get_network_requests` | `["get_network_requests"]` | `[{"id": 0, "url": "...", "method": "GET", "status": 200}]` |
| `get_network_request` | `["get_network_request", 0]` | `{"url": "...", "method": "GET", "request_headers": {...}, "response_headers": {...}, "response_body": "..."}` |
| `clear_network_requests` | `["clear_network_requests"]` | `{}` |

**Network tracking behavior:**
- When started, captures all resource requests via `resource-load-started` and `resource-load-finished` signals
- Stores: URL, method, status, request headers, response headers, response body
- Each request gets sequential ID (0, 1, 2...)
- `get_network_requests` returns summary list (no bodies)
- `get_network_request <id>` returns full details for that request
- Tracking stops on daemon exit (no persistence)

---

## File Structure

```
/home/blah/poc/dbrowser/
├── browser.py         # existing (human interactive, GTK/WebKit2)
├── qtbrowser.py       # existing (Qt version)
└── dbrowser-server.py # NEW (daemon with IPC)
```

Single file. No client.py needed - AI talks raw JSON over socket directly.

---

## Usage

**Start daemon (outside sandbox):**
```bash
./dbrowser-server.py
# Output: [daemon] Listening on /run/user/1000/tmp/dbrowser.sock
```

**Send commands (inside sandbox):**
```bash
# With socat
echo '{"command": ["navigate", "https://example.com"]}' | \
  socat - UNIX-CONNECT:/run/user/1000/tmp/dbrowser.sock

# Python
import socket, json

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect('/run/user/1000/tmp/dbrowser.sock')

def cmd(command):
    msg = json.dumps({"command": command}) + '\n'
    sock.sendall(msg.encode())
    return json.loads(sock.recv(65536).decode())

cmd(["navigate", "https://example.com"])
cmd(["eval_js", "document.title"])
cmd(["screenshot"])
```

**Get screenshot (inside sandbox):**
```bash
cat /run/user/1000/tmp/browser-screenshot.png
```

**Kill daemon (outside sandbox):**
```bash
kill %1
```

---

## Implementation Details

### Socket Setup
- Path: `/run/user/1000/tmp/dbrowser.sock`
- Type: Unix domain socket (SOCK_STREAM)
- Permissions: 0600 (owner read/write only)
- Remove existing socket on startup

### Command Loop
```python
while True:
    line = sock.recv(65536).decode().strip()
    if not line:
        break
    if not line.startswith('{'):
        continue  # ignore non-JSON
    try:
        msg = json.loads(line)
        command = msg.get("command", [])
        result = handle_command(command)
        response = {"error": "success", "data": result}
    except Exception as e:
        response = {"error": str(e), "data": None}
    sock.sendall((json.dumps(response) + '\n').encode())
```

### Network Tracking Storage
```python
network_tracking = {
    "enabled": False,
    "requests": [
        {
            "id": 0,
            "url": "https://...",
            "method": "GET",
            "status": 200,
            "request_headers": {...},
            "response_headers": {...},
            "response_body": "...",
        }
    ]
}
```

### WebKit2 Signals
- `resource-load-started`: capture URL, method, headers
- `resource-load-finished`: capture status, response headers, body

### Screenshot
- Always saves to: `/run/user/1000/tmp/browser-screenshot.png`
- Overwrites previous screenshot
- Accessible from inside and outside sandbox

---

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Backend | WebKit2 GTK | Lightweight, no Chromium dependency |
| Socket | Unix domain socket | Simple, local-only, no network exposure |
| Socket path | `/run/user/1000/tmp/dbrowser.sock` | Shared with bwrap sandbox |
| Screenshot path | `/run/user/1000/tmp/browser-screenshot.png` | Shared with bwrap sandbox |
| Protocol | JSON (mpv-style) | Simple, well-understood |
| Sync/Async | Sync only | Simpler implementation, AI doesn't need async |
| Request IDs | None | No async = no correlation needed |
| Events | None | AI polls for state, no push notifications |
| Element refs | None | AI writes JS directly, more flexible |
| Sessions | Single instance | Your requirement |
| Logging | Stdout only | Unix philosophy, user can redirect |
| Lifecycle | External (kill from outside) | No quit command needed |

---

## Dependencies

- Python 3
- GTK 3
- WebKit2GTK 4.0+
- PyGObject

```bash
apt install python3-gi gir1.2-webkit2-4.1
```

---

## TODO

- [ ] Create `dbrowser-server.py`
- [ ] Implement Unix socket listener
- [ ] Implement command router
- [ ] Implement navigation commands
- [ ] Implement content commands
- [ ] Implement `eval_js`
- [ ] Implement screenshot
- [ ] Implement network tracking
- [ ] Test with socat
- [ ] Test with Python client

---

## Notes

- Keep it simple
- No snapshot/ref system (AI writes JS directly)
- No async, no events, no IDs
- One socket, one instance
- Network tracking is opt-in (start/stop)
- No quit command (kill from outside sandbox)
