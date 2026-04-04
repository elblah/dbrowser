#!/usr/bin/env python3
# ruff: noqa: F821, E402
"""
dbrowser — minimal core that loads an engine + plugins.

Env vars:
  DBROWSER_WEBENGINE    - Engine to use: webkit, qt (default: webkit)
  DBROWSER_SIZE         - Window size WxH (default: 800x600)
  DBROWSER_FULLSCREEN=1 - Start in fullscreen mode
  DBROWSER_DEBUG=1      - Show key events
  ... (engine-specific vars)

Usage: browser.py <URL>
"""
import sys
import os

def show_help():
    print('''
Usage: browser.py <URL>

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
  Ctrl+Shift+Del  - Clear all browsing data (cache, cookies, storage)
  Ctrl+W          - Toggle new window redirect (open popup links in current window)
  Ctrl+Shift+M    - Rotate (swap width/height)

Env vars:
  DBROWSER_WEBENGINE    - Engine: webkit, qt (default: webkit)
  DBROWSER_DOWNLOAD_DIR - Download directory (default: ~/Downloads)
  DBROWSER_CACHE_DIR    - Custom cache directory
  DBROWSER_NO_CACHE=1   - Disable disk cache
  DBROWSER_NO_JS=1      - Disable JavaScript (also disables JIT)
  DBROWSER_NO_IMAGES=1  - Don't load images
  DBROWSER_LOW_MEM=1    - Minimize memory usage
  DBROWSER_MEMORY_LIMIT - Memory limit in MB (e.g., 256)
  DBROWSER_FAST=1       - Faster loading (DNS prefetch, page cache)
  DBROWSER_WEBGL=1      - Enable WebGL (disabled by default)
  DBROWSER_MEDIA=1      - Enable media streaming (YouTube, etc)
  DBROWSER_DRM=1        - Enable DRM/encrypted media (Netflix, etc)
  DBROWSER_SIZE         - Window size WxH (default: 800x600)
  DBROWSER_FULLSCREEN=1 - Start in fullscreen mode
  DBROWSER_DEBUG=1      - Show key events
  DBROWSER_JS_CONSOLE=1 - Log JavaScript console.log/warn/error to console
''')

if len(sys.argv) >= 2 and sys.argv[1] in ('-h', '--help'):
    show_help()
    sys.exit(0)

# ── Load engine ──────────────────────────────────────────────────────────
engine_name = os.getenv('DBROWSER_WEBENGINE', 'webkit')
engines_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'engines')

# Try requested engine first, then auto-detect
candidates = [engine_name]
if engine_name == 'webkit':
    candidates.append('qt')
elif engine_name == 'qt':
    candidates.append('webkit')

loaded = False
for name in candidates:
    path = os.path.join(engines_dir, f'{name}.py')
    if os.path.isfile(path):
        print(f'Loading engine: {name}')
        with open(path) as f:
            exec(compile(f.read(), path, 'exec'), globals())
        loaded = True
        break

if not loaded:
    raise SystemExit(f'No engine found. Tried: {", ".join(candidates)}')

# ── Keyboard handler ─────────────────────────────────────────────────────
def on_key(w, e):
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
        alloc = win.get_allocation()
        win.resize(alloc.height, alloc.width)
        print(f'Rotated to {alloc.height}x{alloc.width}')
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
        print(f'New window redirect: {status} (links opening in new window will load here instead)')
    else:
        return False
    return True

win.connect('destroy', Gtk.main_quit)
win.connect('key-press-event', on_key)

# ── Load plugins ─────────────────────────────────────────────────────────
plugins_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'plugins')
if os.path.isdir(plugins_dir):
    for fname in sorted(os.listdir(plugins_dir)):
        if fname.endswith('.py') and not fname.startswith('_'):
            path = os.path.join(plugins_dir, fname)
            print(f'Loading plugin: {fname}')
            with open(path) as f:
                exec(compile(f.read(), path, 'exec'), globals())

Gtk.main()
