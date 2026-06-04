#!/usr/bin/env python3
# ruff: noqa: E402
"""
palemoon-server.py — AI-friendly CLI for Pale Moon via Remote Debugging Protocol.

Wire protocol (Pale Moon RDP, non-WS):
  Each packet is "<length>:<json>" where length is decimal ASCII bytes.

Self-contained: uses only the Python standard library (no pip deps).

SETUP — required once, in Pale Moon's about:config (search each pref, set to true
unless noted):
  devtools.debugger.remote-enabled         = true
  devtools.chrome.enabled                  = true
  devtools.debugger.prompt-connection      = false   (avoids the "allow connection?" dialog)

Then start Pale Moon with the debugger enabled:
  palemoon --start-debugger-server 6000 &

Then use this CLI:
  palemoon status
  palemoon load-url <url>
  palemoon eval-js <code>
  palemoon back / forward / reload
  palemoon tabs
  palemoon switch <n>
  palemoon open <url>
  palemoon close
  palemoon screenshot [path]
  palemoon set-user-agent <ua>
  palemoon get-user-agent
  palemoon get-console-output [N]
  palemoon list-network-requests [N]
  palemoon get-network-request <id>
  palemoon resize <w> <h>
  palemoon fullscreen / unfullscreen / maximize / unmaximize
  palemoon rotate
  palemoon device <profile>
  palemoon raw <json>        # escape hatch: send raw RDP packet
  palemoon help

All commands print a JSON envelope to stdout:
  {"status": "ok"|"error", "data": ..., "message": "..."}

Env:
  DBROWSER_PM_HOST=127.0.0.1  DBROWSER_PM_PORT=6000
  DBROWSER_TIMEOUT=10
  DISPLAY=:0                  (required for screenshot/window commands)
"""
import os
import sys
import json
import socket
import queue
import base64
import time
import shutil
import subprocess
import threading
import re

# ── Config ──────────────────────────────────────────────────────────────
DEBUGGER_HOST = os.getenv('DBROWSER_PM_HOST', '127.0.0.1')
DEBUGGER_PORT = int(os.getenv('DBROWSER_PM_PORT', '6000'))
EVAL_TIMEOUT = float(os.getenv('DBROWSER_TIMEOUT', '10'))
CONSOLE_BUFFER_SIZE = int(os.getenv('DBROWSER_CONSOLE_BUFFER', '1000'))
NETWORK_BUFFER_SIZE = int(os.getenv('DBROWSER_NETWORK_BUFFER', '100'))

DEVICE_PROFILES = {
    'phone-portrait':   (375, 812),
    'phone-landscape':  (812, 375),
    'tablet-portrait':  (768, 1024),
    'tablet-landscape': (1024, 768),
}


# ── Errors / response envelope ─────────────────────────────────────────
class PMoonError(Exception):
    pass


def ok(data=None, message=None):
    out = {"status": "ok"}
    if data is not None:
        out["data"] = data
    if message is not None:
        out["message"] = message
    return out


def err(message):
    return {"status": "error", "message": str(message)}


# ── Pale Moon RDP client (length-prefixed JSON packets) ─────────────────
class PMoonClient:
    """Talks Pale Moon's non-WebSocket debugger protocol (default port 6000)."""

    def __init__(self, host=DEBUGGER_HOST, port=DEBUGGER_PORT):
        self.host = host
        self.port = port
        self.s = None
        self.buf = b''
        self._lock = threading.Lock()
        # all incoming packets land here; events are also demuxed into buffers
        self.packet_queue = queue.Queue()
        self.console_buffer = []
        self.network_buffer = []
        self._network_index = {}
        self._closed = False
        # per-actor FIFO of (request_type, response_event, error_event) for
        # outstanding requests. Packets from actor that are not recognized
        # events are delivered to the head of this queue.
        self._outstanding = {}
        # packet types we recognize as events and DON'T treat as responses
        self._event_types = {
            'tabListChanged', 'tabNavigated', 'consoleAPICall',
            'networkEvent', 'networkEventUpdate', 'frameUpdate',
            'propertyChange', 'childProcessChange', 'tabDetached',
            'resource-available', 'resource-destroyed', 'resources',
            'styleApplied', 'pageError',
        }
        # state
        self.active_tab = None
        self.console_actor = None
        self.emulation_actor = None
        self.tabs_cache = []

    # ── low level
    def connect(self):
        s = socket.create_connection((self.host, self.port), timeout=10)
        s.settimeout(None)
        self.s = s
        hello = self.recv_packet()  # drain server's initial hello
        if hello.get('from') != 'root':
            raise PMoonError(f'unexpected initial packet: {hello}')
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self.refresh_tabs()
        # always activate first tab on connect so console/emulation are ready
        if self.tabs_cache:
            try:
                self.activate(self.tabs_cache[0]['actor'])
            except PMoonError:
                pass

    def close(self):
        self._closed = True
        try:
            if self.s:
                self.s.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            if self.s:
                self.s.close()
        except Exception:
            pass
        self.s = None

    def send(self, obj):
        data = json.dumps(obj).encode('utf-8')
        self.s.sendall(f'{len(data)}:'.encode() + data)

    def recv_packet(self, timeout=10):
        self.s.settimeout(timeout)
        deadline = time.time() + timeout
        while True:
            idx = self.buf.find(b':')
            if idx != -1:
                try:
                    n = int(self.buf[:idx])
                    need = idx + 1 + n
                    if len(self.buf) >= need:
                        data = self.buf[idx+1:need]
                        self.buf = self.buf[need:]
                        return json.loads(data.decode('utf-8'))
                except ValueError:
                    pass
            remaining = max(0.05, deadline - time.time())
            self.s.settimeout(remaining)
            try:
                chunk = self.s.recv(8192)
            except socket.timeout:
                raise PMoonError('read timeout')
            if not chunk:
                raise PMoonError('server closed connection')
            self.buf += chunk

    def _read_loop(self):
        while not self._closed and self.s:
            try:
                pkt = self.recv_packet(timeout=60)
            except Exception:
                if not self._closed:
                    self._closed = True
                return
            self._dispatch(pkt)

    def _dispatch(self, pkt):
        """Route incoming packet: events go to handlers, responses wake waiters."""
        frm = pkt.get('from')
        typ = pkt.get('type')
        # 1. recognized event types are processed and dropped
        if typ in self._event_types:
            self._handle_event(pkt)
            return
        # 2. response with type matching a known event-name string from server
        #    (e.g. "navigated", "frameUpdate") — also skip if no outstanding
        if not frm:
            return
        outstanding = self._outstanding.get(frm)
        if not outstanding:
            # unexpected packet; ignore
            return
        # 3. deliver to next waiter for this actor
        _, ev = outstanding.pop(0)
        if not outstanding:
            self._outstanding.pop(frm, None)
        # signal any listeners on packet_queue (escape hatch) too
        self.packet_queue.put(pkt)
        ev['pkt'] = pkt
        ev['event'].set()

    def _handle_event(self, pkt):
        t = pkt.get('type', '')
        if t == 'consoleAPICall':
            self._on_console(pkt)
        elif t == 'networkEvent':
            self._on_network_event(pkt)
        elif t == 'networkEventUpdate':
            self._on_network_update(pkt)
        # tabListChanged, tabNavigated, responses -> left in queue for call()

    def _on_console(self, pkt):
        msg = pkt.get('message', {})
        try:
            level = msg.get('level', 'log')
            args = msg.get('arguments', [])
            parts = []
            for a in args:
                if isinstance(a, dict):
                    if a.get('type') == 'string':
                        parts.append(a.get('value', ''))
                    elif 'value' in a:
                        v = a['value']
                        parts.append(str(v) if v is not None else str(a.get('type', '')))
                    else:
                        parts.append(a.get('type', '?'))
                else:
                    parts.append(str(a))
            line = f"[{level}] " + ' '.join(parts)
        except Exception as e:
            line = f"[log] <parse error: {e}>"
        self.console_buffer.append(line)
        if len(self.console_buffer) > CONSOLE_BUFFER_SIZE:
            self.console_buffer.pop(0)

    def _on_network_event(self, pkt):
        ev = pkt.get('eventActor', pkt.get('eventDoc', pkt))
        actor = pkt.get('actor') or ''
        net_actor = ev.get('actor') or actor
        req_id = net_actor or f'req_{len(self.network_buffer)}_{int(time.time()*1000)}'
        rec = {
            'id': req_id,
            'uri': ev.get('url', ''),
            'method': ev.get('method', 'GET'),
            'headers': self._flat(ev.get('requestHeaders') or ev.get('headers', {})),
            'response_headers': {},
            'status': 'loading',
            'startedDateTime': ev.get('timeStamp') or int(time.time() * 1000),
        }
        if len(self.network_buffer) >= NETWORK_BUFFER_SIZE:
            old = self.network_buffer.pop(0)
            self._network_index.pop(old['id'], None)
        self.network_buffer.append(rec)
        self._network_index[req_id] = len(self.network_buffer) - 1
        if net_actor:
            self._network_index[net_actor] = len(self.network_buffer) - 1

    def _on_network_update(self, pkt):
        # The networkEventUpdate packets identify the network event by `from`
        # (they are sent from the netEvent<id> actor), not by an `actor` field.
        actor = pkt.get('from') or pkt.get('actor', '')
        idx = self._network_index.get(actor)
        if idx is None:
            return
        rec = self.network_buffer[idx]
        update = pkt.get('updateType', '')
        if update == 'requestHeaders':
            rec['headers_size'] = pkt.get('headersSize')
        elif update == 'requestPostData':
            rec['postData'] = pkt.get('data', '')
        elif update == 'responseStart':
            resp = pkt.get('response', {})
            rec['status_code'] = resp.get('status')
            rec['status_text'] = resp.get('statusText')
            rec['http_version'] = resp.get('httpVersion')
            rec['remote_address'] = resp.get('remoteAddress')
        elif update == 'responseHeaders':
            rec['response_headers_size'] = pkt.get('headersSize')
        elif update == 'responseContent':
            rec['mime_type'] = pkt.get('mimeType', '')
            rec['contentSize'] = pkt.get('contentSize', 0)
            rec['transferredSize'] = pkt.get('transferredSize', 0)
        elif update == 'eventTimings':
            rec['totalTime'] = pkt.get('totalTime')
        elif update == 'securityInfo':
            rec['security_state'] = pkt.get('state')
        if pkt.get('state') == 'stopped' or update == 'securityInfo':
            rec['status'] = 'complete'

    @staticmethod
    def _flat(headers):
        if isinstance(headers, dict):
            return dict(headers)
        if isinstance(headers, list):
            out = {}
            for h in headers:
                if isinstance(h, dict) and 'name' in h:
                    out[h['name']] = h.get('value', '')
            return out
        return {}

    # ── request/response
    def call(self, to, typ, **extra):
        msg = {'to': to, 'type': typ}
        msg.update(extra)
        ev = threading.Event()
        slot = {'pkt': None, 'event': ev}
        with self._lock:
            self._outstanding.setdefault(to, []).append((typ, slot))
        try:
            self.send(msg)
            if not ev.wait(timeout=EVAL_TIMEOUT):
                raise PMoonError(f'timeout waiting for {typ} -> {to}')
            pkt = slot['pkt']
            if pkt and pkt.get('type') == 'error':
                raise PMoonError(pkt.get('message', 'unknown error'))
            return pkt
        finally:
            with self._lock:
                lst = self._outstanding.get(to)
                if lst:
                    try:
                        lst.remove((typ, slot))
                    except ValueError:
                        pass
                    if not lst:
                        self._outstanding.pop(to, None)

    # ── high level
    def refresh_tabs(self):
        r = self.call('root', 'listTabs')
        self.tabs_cache = r.get('tabs', [])
        if not self.tabs_cache:
            return r
        # if active tab died, fall back to first
        actors = {t.get('actor') for t in self.tabs_cache}
        if self.active_tab not in actors:
            self.active_tab = self.tabs_cache[r.get('selected', 0)]['actor']
        return r

    def tabs(self):
        self.refresh_tabs()
        return [{
            'index': i,
            'actor': t.get('actor'),
            'url': t.get('url'),
            'title': t.get('title'),
        } for i, t in enumerate(self.tabs_cache)]

    def activate(self, target):
        """target: int index, str actor, or None for first."""
        if isinstance(target, int):
            self.refresh_tabs()
            if not self.tabs_cache:
                raise PMoonError('no tabs')
            if target < 0 or target >= len(self.tabs_cache):
                raise PMoonError(f'tab index {target} out of range (0..{len(self.tabs_cache)-1})')
            target = self.tabs_cache[target]['actor']
        # Refresh and look up the tab record (it carries the child actor ids)
        self.refresh_tabs()
        tab = next((t for t in self.tabs_cache if t.get('actor') == target), None)
        if not tab:
            raise PMoonError(f'tab {target} not found')
        self.active_tab = target
        self.console_actor = tab.get('consoleActor')
        self.emulation_actor = tab.get('emulationActor')
        # Attach so the tab is "live" (required by some actors)
        try:
            self.call(target, 'attach')
        except PMoonError:
            pass
        if self.console_actor:
            try:
                self.call(self.console_actor, 'startListeners',
                          listeners=['ConsoleAPI', 'NetworkActivity'])
            except PMoonError:
                pass

    def navigate(self, url):
        if not self.active_tab:
            raise PMoonError('no active tab')
        return self.call(self.active_tab, 'navigateTo', url=url)

    def reload(self):
        if not self.active_tab:
            raise PMoonError('no active tab')
        return self.call(self.active_tab, 'reload')

    def _history_url(self, direction):
        """Return URL to navigate to for back/forward, or None."""
        self.refresh_tabs()
        for t in self.tabs_cache:
            if t.get('actor') == self.active_tab:
                history = t.get('history', {})
                entries = history.get('entries', [])
                cur = history.get('current', 0)
                if direction == 'back' and cur > 0:
                    return entries[cur - 1].get('url')
                if direction == 'forward' and cur + 1 < len(entries):
                    return entries[cur + 1].get('url')
        return None

    def back(self):
        # Pale Moon has no goBack actor method; use history
        url = self._history_url('back')
        if not url:
            raise PMoonError('no previous page in history')
        return self.navigate(url)

    def forward(self):
        url = self._history_url('forward')
        if not url:
            raise PMoonError('no next page in history')
        return self.navigate(url)

    def close_tab(self):
        if not self.active_tab:
            raise PMoonError('no active tab')
        return self.call(self.active_tab, 'close')

    def open_tab(self, url):
        # no dedicated openTab actor; navigate active to URL is simpler
        return self.navigate(url)

    def evaluate(self, code, frame_actor=None):
        if not self.console_actor:
            raise PMoonError('no console actor (tab not attached?)')
        extra = {'text': code}
        if frame_actor:
            extra['frameActor'] = frame_actor
        r = self.call(self.console_actor, 'evaluateJS', **extra)
        if r.get('exception'):
            exc = r['exception']
            return {'value': None, 'error': exc.get('text') or str(exc.get('class', 'Error'))}
        return {'value': self._grip_to_python(r.get('result', {})), 'grip': r.get('result', {})}

    @staticmethod
    def _grip_to_python(g):
        if not isinstance(g, dict):
            return g
        t = g.get('type')
        if t == 'undefined':
            return None
        if 'value' in g:
            return None if t == 'null' else g['value']
        # objects with preview -> return preview contents (often a nice stringification)
        if 'preview' in g:
            pv = g['preview']
            if 'items' in pv:
                # array-like preview: {items: [{name:"0", value:"..."}, ...], length: N}
                items = pv['items']
                rendered = []
                for it in items:
                    if isinstance(it, dict):
                        if it.get('type') == 'string' and 'value' in it:
                            rendered.append(it['value'])
                        elif 'value' in it:
                            rendered.append(it['value'])
                        else:
                            rendered.append(it.get('description', it.get('name', '?')))
                    else:
                        rendered.append(str(it))
                if pv.get('length', 0) > len(rendered):
                    rendered.append(f'... ({pv["length"] - len(rendered)} more)')
                return rendered
            return pv.get('description', '[object]')
        return g.get('description', f'<{t}>')

    def set_user_agent(self, ua):
        if not self.active_tab:
            raise PMoonError('no active tab')
        if not self.emulation_actor:
            # try re-attach
            self.activate(self.active_tab)
        if not self.emulation_actor:
            raise PMoonError('no emulation actor (attach failed?)')
        return self.call(self.emulation_actor, 'setUserAgentOverride', flag=ua)

    def status(self):
        self.refresh_tabs()
        sel = next((t for t in self.tabs_cache if t.get('actor') == self.active_tab), None)
        return {
            'url': (sel or {}).get('url', ''),
            'title': (sel or {}).get('title', ''),
            'tab_count': len(self.tabs_cache),
            'active_tab': self.active_tab,
        }

    def raw(self, packet):
        """Send a raw packet and return the next matching response.
        packet: dict with 'to' and 'type' at minimum.
        Returns the response dict.
        """
        to = packet.get('to', 'root')
        typ = packet.get('type')
        if not typ:
            raise PMoonError("raw packet needs 'type'")
        ev = threading.Event()
        slot = {'pkt': None, 'event': ev}
        with self._lock:
            self._outstanding.setdefault(to, []).append((typ, slot))
        try:
            self.send(packet)
            if not ev.wait(timeout=EVAL_TIMEOUT):
                raise PMoonError(f'timeout waiting for {typ}')
            pkt = slot['pkt']
            if pkt and pkt.get('type') == 'error':
                raise PMoonError(pkt.get('message', 'unknown error'))
            return pkt
        finally:
            with self._lock:
                lst = self._outstanding.get(to)
                if lst:
                    try:
                        lst.remove((typ, slot))
                    except ValueError:
                        pass
                    if not lst:
                        self._outstanding.pop(to, None)


# ── X11 / window helpers (fallback for screenshot + window control) ───
def _display():
    return os.getenv('DISPLAY') or ':0'

def find_palemoon_window():
    if not shutil.which('wmctrl'):
        return None
    try:
        out = subprocess.run(['wmctrl', '-l'], capture_output=True, text=True,
                             env={**os.environ, 'DISPLAY': _display()}, timeout=3).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        wid, _, title = parts[0], parts[1], parts[3]
        if 'Pale Moon' in title or 'palemoon' in title.lower():
            return wid
    return None

def take_screenshot(out_path):
    wid = find_palemoon_window()
    env = {**os.environ, 'DISPLAY': _display()}
    if wid and shutil.which('import'):
        subprocess.run(['import', '-window', wid, out_path], check=True, env=env, timeout=15)
    elif shutil.which('scrot'):
        subprocess.run(['scrot', '-u', out_path], check=True, env=env, timeout=15)
    elif shutil.which('import'):
        subprocess.run(['import', '-window', 'root', out_path], check=True, env=env, timeout=15)
    else:
        raise RuntimeError('install scrot or imagemagick for screenshots')
    return out_path

def wmctrl_do(action, *args):
    if not shutil.which('wmctrl'):
        raise RuntimeError('wmctrl not installed')
    wid = find_palemoon_window()
    if not wid:
        raise RuntimeError('Pale Moon window not found')
    env = {**os.environ, 'DISPLAY': _display()}
    if action in ('focus', 'resize'):
        subprocess.run(['wmctrl', '-i', '-a', wid], check=False, env=env, timeout=3)
    if action == 'maximize':
        subprocess.run(['wmctrl', '-i', '-r', wid, '-b', 'add,maximized_vert,maximized_horz'],
                       check=True, env=env, timeout=3)
    elif action == 'unmaximize':
        subprocess.run(['wmctrl', '-i', '-r', wid, '-b', 'remove,maximized_vert,maximized_horz'],
                       check=True, env=env, timeout=3)
    elif action == 'fullscreen':
        subprocess.run(['wmctrl', '-i', '-r', wid, '-b', 'add,fullscreen'],
                       check=True, env=env, timeout=3)
    elif action == 'unfullscreen':
        subprocess.run(['wmctrl', '-i', '-r', wid, '-b', 'remove,fullscreen'],
                       check=True, env=env, timeout=3)
    elif action == 'resize':
        w, h = args
        if not shutil.which('xdotool'):
            raise RuntimeError('xdotool required for resize')
        subprocess.run(['xdotool', 'windowsize', wid, str(w), str(h)],
                       check=True, env=env, timeout=3)


# ── Command dispatch ────────────────────────────────────────────────────
HELP_TEXT = """
palemoon — AI-friendly CLI for Pale Moon (RDP)

Required about:config prefs (set once, all in about:config):
  devtools.debugger.remote-enabled     = true
  devtools.chrome.enabled              = true
  devtools.debugger.prompt-connection  = false

Start browser:  palemoon --start-debugger-server 6000 &

Navigation:
  status                                    URL/title of active tab
  load-url <url>                            Navigate active tab
  back / forward                            History (via listTabs history)
  reload                                    Reload active tab
  open <url>                                Open URL in active tab (alias for load-url)
  close                                     Close active tab

Tabs:
  tabs                                      List open tabs
  switch <n|actor>                          Switch active tab by index or actor id

Inspection:
  eval-js <code>                            Run JavaScript, return result
  set-user-agent <ua>                       Set UA via emulation actor, reload
  get-user-agent                            Current navigator.userAgent

Console / network (active tab):
  get-console-output [N]                    Last N console lines (default all)
  list-network-requests [N]                 Last N network requests
  get-network-request <id>                  Full request record

Window / display (need DISPLAY):
  screenshot [path]                         Save PNG (default: /tmp/pm-<ts>.png)
  resize <w> <h>                            Resize window
  fullscreen / unfullscreen                 Fullscreen toggle
  maximize / unmaximize                     Maximize toggle
  rotate                                    Swap width/height
  device <profile>                          phone-portrait | phone-landscape |
                                            tablet-portrait | tablet-landscape

Escape hatch:
  raw <json>                                Send raw RDP packet, return response
                                            e.g. raw '{"to":"root","type":"listAddons"}'

Other:
  help                                      This help
"""


def cmd_help(args):
    print(HELP_TEXT)
    sys.exit(0)


def cmd_status(client, args):
    return ok(client.status())


def cmd_load_url(client, args):
    if not args:
        return err('load-url requires URL argument')
    client.navigate(args[0])
    return ok(f'loading {args[0]}')


def cmd_open(client, args):
    return cmd_load_url(client, args)


def cmd_back(client, args):
    client.back()
    return ok('went back')


def cmd_forward(client, args):
    client.forward()
    return ok('went forward')


def cmd_reload(client, args):
    client.reload()
    return ok('reloading')


def cmd_close(client, args):
    client.close_tab()
    return ok('closed active tab')


def cmd_tabs(client, args):
    return ok(client.tabs())


def cmd_switch(client, args):
    if not args:
        return err('switch requires index or actor id')
    target = args[0]
    if target.isdigit() or (target.startswith('-') and target[1:].isdigit()):
        target = int(target)
    client.activate(target)
    return ok(f'switched to {target}')


def cmd_eval_js(client, args):
    if not args:
        return err('eval-js requires code argument')
    code = ' '.join(args)
    # wrap so we always get a JSON-serializable return value
    # exceptions are caught and surfaced
    # NOTE: avoid `var x = (function(){...})()` — Pale Moon's eval chokes on it
    wrapped = (
        "(function(){try{"
        "var __r = eval('" + code.replace("\\", "\\\\").replace("'", "\\'") + "');"
        "return (typeof __r==='string'||typeof __r==='number'"
        "||typeof __r==='boolean'||__r===null||__r===undefined)"
        "?__r:JSON.stringify(__r);"
        "}catch(e){return 'ERROR: '+(e&&e.message?e.message:String(e));}})()"
    )
    r = client.evaluate(wrapped)
    if 'error' in r:
        return ok(r['value'], message=r['error'])
    val = r.get('value')
    if isinstance(val, str) and val and val[0] in '{[':
        try:
            val = json.loads(val)
        except Exception:
            pass
    if val is None or val == 'undefined':
        return ok(message='undefined')
    return ok(val)


def cmd_set_user_agent(client, args):
    if not args:
        return err('set-user-agent requires UA string')
    ua = ' '.join(args)
    client.set_user_agent(ua)
    client.reload()
    return ok(f'user agent set to: {ua[:80]}...')


def cmd_get_user_agent(client, args):
    r = client.evaluate('navigator.userAgent')
    return ok(r.get('value', 'unknown'))


def cmd_get_console_output(client, args):
    n = int(args[0]) if args else None
    buf = client.console_buffer
    if n is None:
        out = list(buf)
    elif n < 0:
        out = buf[n:]
    else:
        out = buf[:n]
    return ok(out)


def cmd_list_network_requests(client, args):
    n = int(args[0]) if args else None
    reqs = list(client.network_buffer)
    if n and n > 0:
        reqs = reqs[:n]
    return ok(reqs)


def cmd_get_network_request(client, args):
    if not args:
        return err('get-network-request requires id')
    target = args[0]
    for r in client.network_buffer:
        if r.get('id') == target:
            return ok(r)
    return err(f'request {target} not found')


def cmd_screenshot(client, args):
    path = args[0] if args else f'/tmp/pm-{int(time.time())}.png'
    try:
        take_screenshot(path)
        with open(path, 'rb') as f:
            data = base64.b64encode(f.read()).decode('ascii')
        return ok({'path': path, 'base64': data, 'bytes': os.path.getsize(path)})
    except Exception as e:
        return err(f'screenshot failed: {e}')


def cmd_resize(client, args):
    if len(args) < 2:
        return err('resize requires width and height')
    try:
        w, h = int(args[0]), int(args[1])
    except ValueError:
        return err('width and height must be integers')
    if w <= 0 or h <= 0:
        return err('width and height must be positive')
    try:
        wmctrl_do('resize', w, h)
        return ok(f'resized to {w}x{h}')
    except Exception as e:
        return err(str(e))


def cmd_maximize(client, args):
    try:
        wmctrl_do('maximize')
        return ok('window maximized')
    except Exception as e:
        return err(str(e))


def cmd_unmaximize(client, args):
    try:
        wmctrl_do('unmaximize')
        return ok('window restored')
    except Exception as e:
        return err(str(e))


def cmd_fullscreen(client, args):
    try:
        wmctrl_do('fullscreen')
        return ok('entered fullscreen')
    except Exception as e:
        return err(str(e))


def cmd_unfullscreen(client, args):
    try:
        wmctrl_do('unfullscreen')
        return ok('exited fullscreen')
    except Exception as e:
        return err(str(e))


def cmd_rotate(client, args):
    wid = find_palemoon_window()
    if not wid or not shutil.which('xdotool'):
        return err('cannot read window geometry (need xdotool)')
    try:
        geom = subprocess.run(['xdotool', 'getwindowgeometry', wid],
                              capture_output=True, text=True,
                              env={**os.environ, 'DISPLAY': _display()}, timeout=3).stdout
        m = re.search(r'(\d+)x(\d+)', geom)
        if not m:
            return err(f'could not parse geometry: {geom!r}')
        w, h = int(m.group(1)), int(m.group(2))
        wmctrl_do('resize', h, w)
        return ok(f'rotated to {h}x{w}')
    except Exception as e:
        return err(str(e))


def cmd_device(client, args):
    if not args:
        return err(f'device requires profile. Available: {", ".join(DEVICE_PROFILES)}')
    p = args[0]
    if p not in DEVICE_PROFILES:
        return err(f'unknown profile: {p}. Available: {", ".join(DEVICE_PROFILES)}')
    w, h = DEVICE_PROFILES[p]
    try:
        wmctrl_do('resize', w, h)
        return ok(f'resized to {p} ({w}x{h})')
    except Exception as e:
        return err(str(e))


def cmd_raw(client, args):
    if not args:
        return err('raw requires JSON packet string')
    try:
        pkt = json.loads(' '.join(args))
    except json.JSONDecodeError as e:
        return err(f'invalid JSON: {e}')
    try:
        r = client.raw(pkt)
        return ok(r)
    except PMoonError as e:
        return err(str(e))


COMMANDS = {
    'help':              (cmd_help,             False),
    'status':            (cmd_status,           True),
    'load-url':          (cmd_load_url,         True),
    'open':              (cmd_open,             True),
    'back':              (cmd_back,             True),
    'forward':           (cmd_forward,          True),
    'reload':            (cmd_reload,           True),
    'close':             (cmd_close,            True),
    'tabs':              (cmd_tabs,             True),
    'switch':            (cmd_switch,           True),
    'eval-js':           (cmd_eval_js,          True),
    'set-user-agent':    (cmd_set_user_agent,   True),
    'get-user-agent':    (cmd_get_user_agent,   True),
    'get-console-output':(cmd_get_console_output, True),
    'list-network-requests': (cmd_list_network_requests, True),
    'get-network-request':   (cmd_get_network_request,   True),
    'screenshot':        (cmd_screenshot,       True),
    'resize':            (cmd_resize,           True),
    'maximize':          (cmd_maximize,         True),
    'unmaximize':        (cmd_unmaximize,       True),
    'fullscreen':        (cmd_fullscreen,       True),
    'unfullscreen':      (cmd_unfullscreen,     True),
    'rotate':            (cmd_rotate,           True),
    'device':            (cmd_device,           True),
    'raw':               (cmd_raw,              True),
}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    name = sys.argv[1]
    if name in ('-h', '--help'):
        print(__doc__)
        sys.exit(0)
    if name not in COMMANDS:
        print(json.dumps(err(f'unknown command: {name}. Run with --help or help')))
        sys.exit(2)
    handler, needs_client = COMMANDS[name]
    args = sys.argv[2:]
    if not needs_client:
        handler(args)
        return
    client = PMoonClient()
    try:
        client.connect()
    except Exception as e:
        print(json.dumps(err(f'connect failed: {e}')))
        sys.exit(1)
    try:
        result = handler(client, args)
        if result is not None:
            print(json.dumps(result))
    except PMoonError as e:
        print(json.dumps(err(str(e))))
        sys.exit(1)
    except Exception as e:
        print(json.dumps(err(f'{type(e).__name__}: {e}')))
        sys.exit(1)
    finally:
        client.close()


if __name__ == '__main__':
    main()
