# ruff: noqa: F821, E402
"""
WebKitGTK engine for dbrowser.

Loaded by browser.py — runs in its global scope.
Exposes: web, win, ctx, settings, clipboard, find, find_text
"""
import warnings
warnings.filterwarnings('ignore', category=DeprecationWarning, module='gi.repository')
import gi
for ver in ('4.1', '4.0'):
    try:
        gi.require_version('WebKit2', ver)
        break
    except ValueError:
        pass
else:
    raise SystemExit('No WebKit2 found')
gi.require_version('Gdk', '3.0')
from gi.repository import WebKit2, Gtk, Gdk, GLib

# ── Config ──────────────────────────────────────────────────────────────
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

# ── Context ─────────────────────────────────────────────────────────────
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
cookie_manager.set_accept_policy(WebKit2.CookieAcceptPolicy.NO_THIRD_PARTY)

if low_mem:
    ctx.set_process_model(WebKit2.ProcessModel.SHARED_SECONDARY_PROCESS)

# ── Window & WebView ───────────────────────────────────────────────────
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

win.add(web)

# ── State ──────────────────────────────────────────────────────────────
is_fullscreen = fullscreen
redirect_new_windows = False

if is_fullscreen:
    win.fullscreen()

# ── Helpers ────────────────────────────────────────────────────────────
import subprocess
import urllib.parse
import random
import string

clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
find = web.get_find_controller()
find_text = ['']

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

# ── JS Console (optional) ──────────────────────────────────────────────
if show_js_console:
    user_content = web.get_user_content_manager()
    user_content.register_script_message_handler('console')

    def on_console_message(user_content, result):
        message = result.get_js_value().to_string()
        print(f'[JS] {message}')
    user_content.connect('script-message-received::console', on_console_message)

    user_content.add_script(WebKit2.UserScript('''
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
    ''', 0, 0, None, None))

# ── Downloads & Policy ─────────────────────────────────────────────────
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

# ── Title & Progress ───────────────────────────────────────────────────
def update_title():
    title = web.get_title() or 'Loading...'
    progress = web.get_estimated_load_progress()
    if progress < 1.0:
        win.set_title(f'{title} - dbrowser ({int(progress * 100)}%)')
    else:
        win.set_title(f'{title} - dbrowser')

web.connect('notify::title', lambda wv, pspec: update_title())
web.connect('notify::estimated-load-progress', lambda wv, pspec: update_title())

# ── Load initial page ──────────────────────────────────────────────────
web.load_uri(url)
win.show_all()
win.set_title('dbrowser')
