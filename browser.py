#!/usr/bin/env python3
import sys
import os

def show_help():
    print('''
Usage: browser.py <URL>

Keybindings:
  F1              - Show this help
  Ctrl+Q          - Quit
  F5 / Ctrl+R     - Reload page
  F12             - Developer tools
  Ctrl+P          - Print dialog
  Ctrl+Shift+P    - Save page as PDF
  Ctrl+S          - Save page as HTML
  Ctrl+Shift+S    - Save page screenshot as PNG
  Ctrl+L          - Change URL (dmenu)
  Ctrl+B          - Open link from bookmarks (dmenu)
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
  Ctrl+F          - Find in page (dmenu)
  Ctrl+N          - Find next
  Ctrl+Shift+N    - Find previous

Env vars:
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
  DBROWSER_DEBUG=1      - Show key events
''')

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
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
from gi.repository import WebKit2, Gtk, Gdk, GLib, Gio  # noqa: E402

url = sys.argv[1]
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

# Context config (must be before WebView creation)
if memory_limit:
    # Create custom data manager for memory control
    data_manager = WebKit2.WebsiteDataManager()
    try:
        mem_mb = int(memory_limit)
        mps = WebKit2.MemoryPressureSettings()
        mps.set_memory_limit(mem_mb)
        mps.set_kill_threshold(0.95)         # 95% of limit (set first)
        mps.set_strict_threshold(0.85)       # 85% of limit
        mps.set_conservative_threshold(0.7)  # 70% of limit
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

# Block third-party cookies
cookie_manager = ctx.get_cookie_manager()
cookie_manager.set_accept_policy(WebKit2.CookieAcceptPolicy.NO_THIRD_PARTY)

# Use single process to reduce memory
if low_mem:
    ctx.set_process_model(WebKit2.ProcessModel.SHARED_SECONDARY_PROCESS)

win = Gtk.Window()
size = os.getenv('DBROWSER_SIZE', '800x600')
w, h = map(int, size.split('x'))
win.set_default_size(w, h)
web = WebKit2.WebView()
settings = web.get_settings()
settings.set_enable_developer_extras(True)
settings.set_enable_mediasource(bool(enable_media))
settings.set_enable_media_stream(bool(enable_media))
settings.set_enable_encrypted_media(bool(enable_drm))
settings.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.ON_DEMAND)
# Disable expensive/insecure features by default
settings.set_enable_webgl(False)
settings.set_enable_plugins(False)
settings.set_enable_smooth_scrolling(False)
settings.set_enable_hyperlink_auditing(False)
# File access isolation - prevent local file exfiltration
settings.set_allow_file_access_from_file_urls(False)
settings.set_allow_universal_access_from_file_urls(False)
# Restrict JS capabilities
settings.set_javascript_can_access_clipboard(False)
settings.set_javascript_can_open_windows_automatically(False)
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
settings.set_user_agent('Mozilla/5.0')
win.add(web)
win.show_all()

# Lazy imports and initialization
import subprocess  # noqa: E402
import urllib.parse  # noqa: E402
import random  # noqa: E402
import string  # noqa: E402

clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
find = web.get_find_controller()
find_text = ['']

def is_valid_url(text):
    """Check if text looks like a valid URL."""
    if not text:
        return False
    return text.startswith(('http://', 'https://', 'ftp://', 'file://'))

def get_save_path(title, ext):
    """Generate a safe file path with random suffix for saving."""
    safe_title = ''.join(c if c.isalnum() or c in '-_' else '_' for c in title)
    rand_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=7))
    path = os.path.expanduser(os.getenv('DBROWSER_DOWNLOAD_DIR', '~/Downloads'))
    os.makedirs(path, exist_ok=True)
    return f'{path}/{safe_title}__{rand_suffix}.{ext}'

def on_key(w, e):
    if debug:
        print(f'key pressed: keyval={e.keyval}, state={e.state}')
    if e.keyval == Gdk.KEY_F1:
        show_help()
    elif e.keyval == Gdk.KEY_q and e.state & Gdk.ModifierType.CONTROL_MASK:
        print('Quitting...')
        Gtk.main_quit()
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
            
            # Use PrintOperation to save directly to PDF without dialog
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
                data = stream.read_bytes(10 * 1024 * 1024, None)  # Read up to 10MB
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
        print('Opening dmenu for URL...')
        new_url = subprocess.run(['dmenu', '-p', 'URL'], input=web.get_uri(),
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
        selected = subprocess.run(['dmenu', '-i', '-l', '20', '-p', 'Open link:'],
                                  input=links, capture_output=True, text=True).stdout.strip()
        if selected:
            print(f'Opening: {selected}')
            web.load_uri(selected)
    elif e.keyval == Gdk.KEY_Left and e.state & Gdk.ModifierType.MOD1_MASK:
        print('Going back...')
        web.go_back()
    elif e.keyval == Gdk.KEY_Right and e.state & Gdk.ModifierType.MOD1_MASK:
        print('Going forward...')
        web.go_forward()
    elif e.keyval == Gdk.KEY_h and e.state & Gdk.ModifierType.MOD1_MASK:
        print('Going back...')
        web.go_back()
    elif e.keyval == Gdk.KEY_l and e.state & Gdk.ModifierType.MOD1_MASK:
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
    elif e.keyval == Gdk.KEY_comma and e.state & Gdk.ModifierType.MOD1_MASK:
        print('Going back...')
        web.go_back()
    elif e.keyval == Gdk.KEY_period and e.state & Gdk.ModifierType.MOD1_MASK:
        print('Going forward...')
        web.go_forward()
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
        search = subprocess.run(['dmenu', '-p', 'Find'], input=find_text[0],
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
    else:
        return False
    return True

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

WebKit2.WebContext.get_default().connect('download-started', on_download)
win.connect('destroy', Gtk.main_quit)
win.connect('key-press-event', on_key)

def on_title_changed(webview, pspec):
    title = webview.get_title() or 'Loading...'
    progress = webview.get_estimated_load_progress()
    if progress < 1.0:
        win.set_title(f'{title} - dbrowser ({int(progress * 100)}%)')
    else:
        win.set_title(f'{title} - dbrowser')
web.connect('notify::title', on_title_changed)

def on_load_progress(webview, pspec):
    title = webview.get_title() or 'Loading...'
    progress = webview.get_estimated_load_progress()
    if progress < 1.0:
        win.set_title(f'{title} - dbrowser ({int(progress * 100)}%)')
    else:
        win.set_title(f'{title} - dbrowser')
web.connect('notify::estimated-load-progress', on_load_progress)

win.set_title('dbrowser')

web.load_uri(url)
win.show_all()
Gtk.main()
