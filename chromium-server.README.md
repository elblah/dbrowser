# dbrowser-chromium

Drop-in replacement for the WebKitGTK `dbrowser` server, backed by chromium
via the Chrome DevTools Protocol (CDP) over WebSocket.

## What it does

Same Unix-socket JSON IPC as `server.py`. Same command set. Same default
socket path (`/run/user/1000/tmp/dbrowser.sock`) — the `dbrowser` skill
works against this server with zero changes.

## Files

- `chromium-server.py` — the server (stdlib only, no pip)
- `dbrowser-chromium.sh` — launcher (exec into python3 with this dir on path)

## Usage

```bash
# default: listens on /run/user/1000/tmp/dbrowser.sock
./dbrowser-chromium.sh

# or explicitly
python3 chromium-server.py

# initial URL
python3 chromium-server.py https://example.com
```

If chromium is not already running with `--remote-debugging-port=9222`,
the server auto-launches it. The server refuses to start if another
dbrowser is already bound to the socket (so the WebKit and chromium
variants don't fight).

## Auto-launch flags

The auto-launched chromium is invoked with flags that suppress the
"Restore pages?" bubble and first-run popups:

```
--remote-debugging-port=9222
--disable-gpu --no-memcheck
--disable-session-crashed-bubble
--disable-features=InfiniteSessionRestore,SessionRestoreOnStartup
--noerrdialogs
--no-first-run --no-default-browser-check
--user-data-dir=/tmp/chromium-dbrowser-$UID
```

Plus a pre-seeded `Local State` with `exited_cleanly: true` and
`last_cleanup_exited_cleanly: true` so chrome treats the profile as a
clean exit.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `SOCKET_PATH` | `/run/user/$UID/tmp/dbrowser.sock` | Unix socket |
| `DBROWSER_CHROMIUM_BIN` | `chromium` | Browser binary |
| `DBROWSER_CDP_HOST` | `127.0.0.1` | CDP host |
| `DBROWSER_CDP_PORT` | `9222` | CDP port |
| `DBROWSER_CDP_URL` | `about:blank` | Initial URL (overridable via argv) |
| `DBROWSER_AUTO_LAUNCH` | `true` | Auto-start chromium if CDP down |
| `DBROWSER_WIDTH` | `1280` | Viewport width |
| `DBROWSER_HEIGHT` | `800` | Viewport height |
| `DBROWSER_TIMEOUT` | `10.0` | Per-command timeout (seconds) |
| `DBROWSER_CONSOLE_BUFFER` | `1000` | Max console log lines |
| `DBROWSER_NETWORK_BUFFER` | `100` | Max network requests tracked |

## Commands

Identical to `server.py`. Highlights:

- `status` — url, title, ready, viewport
- `load-url <url>` — navigate
- `eval-js <code>` — run JS, return value (awaitPromise enabled)
- `screenshot` — PNG, base64
- `back` / `forward` / `reload`
- `resize <w> <h>` — device metrics override
- `device [profile]` — phone/tablet portrait|landscape presets
- `set-user-agent <ua>` — `Network.setUserAgentOverride`
- `get-console-output [lines]`
- `list-network-requests [max]`
- `get-network-request <id>`
- `help`

## Notes vs WebKit server

- No GTK window → `fullscreen`/`maximize`/`rotate` are dropped
- `cookies <policy>` is a no-op (chromium cookie policy is per-context)
- `eval-js` returns a richer value (objects serialized via CDP's
  `returnByValue`); arrays/objects come back as native JSON
- Screenshot is the current viewport, not the full document
