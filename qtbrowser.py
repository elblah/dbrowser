#!/usr/bin/env python3
import os
import random
import string
import subprocess
import sys
import urllib.parse

from PyQt6.QtCore import QEvent, QObject, Qt, QUrl, QTimer
from PyQt6.QtPrintSupport import QPrinter
from PyQt6.QtWebEngineCore import QWebEngineDownloadRequest, QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication, QMainWindow


def show_help():
    print('''
Usage: qtbrowser.py <URL>

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
  DBROWSER_NO_JS=1      - Disable JavaScript
  DBROWSER_NO_IMAGES=1  - Don't load images
  DBROWSER_LOW_MEM=1    - Minimize memory usage
  DBROWSER_MEMORY_LIMIT - Memory limit in MB (e.g., 256)
  DBROWSER_FAST=1       - Faster loading
  DBROWSER_WEBGL=1      - Enable WebGL
  DBROWSER_MEDIA=1      - Enable media streaming
  DBROWSER_SIZE         - Window size WxH (default: 800x600)
  DBROWSER_DEBUG=1      - Show key events
''')


if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    show_help()
    sys.exit(0)

url = sys.argv[1]

# Disable hardware acceleration for RPi compatibility
os.environ.setdefault('QT_QUICK_BACKEND', 'software')
os.environ.setdefault('QTWEBENGINE_DISABLE_GPU', '1')
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu --disable-software-rasterizer')

debug = os.getenv('DBROWSER_DEBUG')
cache_dir = os.getenv('DBROWSER_CACHE_DIR')
no_cache = os.getenv('DBROWSER_NO_CACHE')
no_js = os.getenv('DBROWSER_NO_JS')
low_mem = os.getenv('DBROWSER_LOW_MEM')
fast = os.getenv('DBROWSER_FAST')
no_images = os.getenv('DBROWSER_NO_IMAGES')
enable_webgl = os.getenv('DBROWSER_WEBGL')
enable_media = os.getenv('DBROWSER_MEDIA')
memory_limit = os.getenv('DBROWSER_MEMORY_LIMIT')

app = QApplication(sys.argv)

# Window setup first
size_str = os.getenv('DBROWSER_SIZE', '800x600')
w, h = map(int, size_str.split('x'))

# Custom WebEngineView that loads URL after initialization
class BrowserView(QWebEngineView):
    def __init__(self, start_url):
        super().__init__()
        self._start_url = start_url
        
    def showEvent(self, event):
        super().showEvent(event)
        # Store URL to load when web engine is ready
        if hasattr(self, '_start_url') and self._start_url:
            _pending_url[0] = self._start_url

# WebEngine view first (Qt6 requires view creation before profile access)
web = BrowserView(url)
page = web.page()

# Profile configuration
profile = page.profile()
if cache_dir:
    profile.setCachePath(os.path.expanduser(cache_dir))
    profile.setPersistentStoragePath(os.path.expanduser(cache_dir))
if no_cache or low_mem:
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
    profile.clearHttpCache()

# Window setup
win = QMainWindow()
win.resize(w, h)
win.setCentralWidget(web)

# Settings
settings = page.settings()
settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, True)
settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, not bool(no_js))
settings.setAttribute(QWebEngineSettings.WebAttribute.AutoLoadIconsForPage, True)
settings.setAttribute(QWebEngineSettings.WebAttribute.AllowRunningInsecureContent, False)
settings.setAttribute(QWebEngineSettings.WebAttribute.AllowGeolocationOnInsecureOrigins, False)
settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False)

if no_images:
    settings.setAttribute(QWebEngineSettings.WebAttribute.AutoLoadImages, False)
if enable_webgl:
    settings.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
if low_mem:
    settings.setAttribute(QWebEngineSettings.WebAttribute.AutoLoadImages, not bool(no_images))

# User agent
profile.setHttpUserAgent('Mozilla/5.0')

find_text = ['']

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

def get_clipboard_text():
    return QApplication.clipboard().text()

def set_clipboard_text(text):
    QApplication.clipboard().setText(text)

def copy_to_tmux_and_clipboard(text):
    subprocess.run(['tmux', 'set-buffer', text], check=False)
    set_clipboard_text(text)

def run_js(code, callback=None):
    if callback:
        page.runJavaScript(code, callback)
    else:
        page.runJavaScript(code)

def update_title(title, progress=None):
    if progress is not None and progress < 100:
        win.setWindowTitle(f'{title} - qtbrowser ({progress}%)')
    else:
        win.setWindowTitle(f'{title} - qtbrowser')

def on_load_started():
    update_title('Loading...', 0)

def on_load_progress(progress):
    title = web.title() or 'Loading...'
    update_title(title, progress)

def on_load_finished(ok):
    title = web.title() or 'Done'
    update_title(title)

def on_title_changed(title):
    update_title(title)

def on_url_changed(qurl):
    pass

web.loadStarted.connect(on_load_started)
web.loadProgress.connect(on_load_progress)
web.loadFinished.connect(on_load_finished)
web.titleChanged.connect(on_title_changed)
web.urlChanged.connect(on_url_changed)

# Polling approach - check if web engine is ready
_has_loaded = [False]
_pending_url = [None]

def try_load_url():
    if _pending_url[0] and not _has_loaded[0]:
        # Try loading
        web.load(QUrl(_pending_url[0]))
        # Check if actually started loading
        if page.isLoading():
            _has_loaded[0] = True
            _pending_url[0] = None
            poll_timer.stop()  # Stop polling once loaded

# Timer to poll for readiness
poll_timer = QTimer()
poll_timer.timeout.connect(try_load_url)
poll_timer.start(500)  # Check every 500ms

# Download handling
def handle_download(item):
    def on_state_changed(state):
        if state == QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
            print(f'Download completed: {item.downloadDirectory()}')
    path = os.path.expanduser(os.getenv('DBROWSER_DOWNLOAD_DIR', '~/Downloads'))
    os.makedirs(path, exist_ok=True)
    suggested = item.suggestedFileName() or 'download'
    dest = os.path.join(path, suggested)
    item.setPath(dest)
    item.stateChanged.connect(on_state_changed)
    item.accept()
    print(f'Downloading to {dest} ...')

profile.downloadRequested.connect(handle_download)

# Key handling
def on_key(event):
    key = event.key()
    modifiers = event.modifiers()
    
    if debug:
        print(f'key pressed: key={key}, modifiers={modifiers}')

    # F1 - Help
    if key == Qt.Key.Key_F1:
        show_help()
    
    # Ctrl+Q - Quit
    elif key == Qt.Key.Key_Q and modifiers == Qt.KeyboardModifier.ControlModifier:
        print('Quitting...')
        QApplication.quit()
    
    # F5 or Ctrl+R - Reload
    elif key == Qt.Key.Key_F5 or (key == Qt.Key.Key_R and modifiers == Qt.KeyboardModifier.ControlModifier):
        print('Reloading...')
        web.reload()
    
    # F12 - Developer tools
    elif key == Qt.Key.Key_F12:
        print('Opening inspector...')
        page.triggerAction(QWebEnginePage.WebAction.InspectElement)
    
    # Ctrl+P - Print dialog
    elif key == Qt.Key.Key_P and modifiers == Qt.KeyboardModifier.ControlModifier:
        print('Printing...')
        page.runJavaScript('window.print()')
    
    # Ctrl+Shift+P - Save as PDF
    elif key == Qt.Key.Key_P and modifiers == (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
        dest = get_save_path(web.title() or 'page', 'pdf')
        print(f'Saving PDF to {dest}...')
        page.printToPdf(dest)
    
    # Ctrl+S - Save as HTML
    elif key == Qt.Key.Key_S and modifiers == Qt.KeyboardModifier.ControlModifier:
        def save_html(title):
            dest = get_save_path(title or 'page', 'html')
            page.save(dest, QWebEngineDownloadRequest.SavePageFormat.CompleteHtmlSaveFormat)
            print(f'Saved HTML to {dest}')
        run_js('document.title', save_html)
    
    # Ctrl+Shift+S - Screenshot
    elif key == Qt.Key.Key_S and modifiers == (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
        def save_screenshot(title):
            dest = get_save_path(title or 'page', 'png')
            pixmap = web.grab()
            pixmap.save(dest)
            print(f'Saved screenshot to {dest}')
        run_js('document.title', save_screenshot)
    
    # Ctrl+Shift+C - Copy page text
    elif key == Qt.Key.Key_C and modifiers == (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
        print('Copying page text to tmux and clipboard...')
        def copy_text(text):
            if text:
                copy_to_tmux_and_clipboard(text)
                print(f'Copied {len(text)} chars to tmux and clipboard')
        run_js('document.body.innerText', copy_text)
    
    # Ctrl+G - Load URL from tmux buffer
    elif key == Qt.Key.Key_G and modifiers == Qt.KeyboardModifier.ControlModifier:
        url_text = subprocess.run(['tmux', 'show-buffer'], capture_output=True, text=True).stdout.strip()
        if is_valid_url(url_text):
            print(f'Loading from tmux buffer: {url_text}')
            web.load(QUrl(url_text))
        else:
            print(f'Tmux buffer is not a valid URL: {url_text[:50] if url_text else "(empty)"}...')
    
    # Ctrl+Shift+G - Load URL from clipboard
    elif key == Qt.Key.Key_G and modifiers == (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
        url_text = get_clipboard_text()
        if is_valid_url(url_text):
            print(f'Loading from clipboard: {url_text}')
            web.load(QUrl(url_text))
        else:
            print(f'Clipboard is not a valid URL: {url_text[:50] if url_text else "(empty)"}...')
    
    # Ctrl+L - Change URL via dmenu
    elif key == Qt.Key.Key_L and modifiers == Qt.KeyboardModifier.ControlModifier:
        print('Opening dmenu for URL...')
        current_url_disp = web.url().toString() if web.url() else ''
        new_url = subprocess.run(['dmenu', '-p', 'URL'], input=current_url_disp,
                                 capture_output=True, text=True).stdout.strip()
        if new_url:
            print(f'Navigating to: {new_url}')
            web.load(QUrl(new_url))
    
    # Ctrl+B - Open bookmark via dmenu
    elif key == Qt.Key.Key_B and modifiers == Qt.KeyboardModifier.ControlModifier:
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
            web.load(QUrl(selected))
    
    # Alt+Left - Back
    elif key == Qt.Key.Key_Left and modifiers == Qt.KeyboardModifier.AltModifier:
        print('Going back...')
        web.back()
    
    # Alt+Right - Forward
    elif key == Qt.Key.Key_Right and modifiers == Qt.KeyboardModifier.AltModifier:
        print('Going forward...')
        web.forward()
    
    # Alt+H - Back
    elif key == Qt.Key.Key_H and modifiers == Qt.KeyboardModifier.AltModifier:
        print('Going back...')
        web.back()
    
    # Alt+L - Forward
    elif key == Qt.Key.Key_L and modifiers == Qt.KeyboardModifier.AltModifier:
        print('Going forward...')
        web.forward()
    
    # Alt+J - Scroll down
    elif key == Qt.Key.Key_J and modifiers == Qt.KeyboardModifier.AltModifier:
        run_js('window.scrollBy(0, 100)')
    
    # Alt+K - Scroll up
    elif key == Qt.Key.Key_K and modifiers == Qt.KeyboardModifier.AltModifier:
        run_js('window.scrollBy(0, -100)')
    
    # Alt+U - Page down
    elif key == Qt.Key.Key_U and modifiers == Qt.KeyboardModifier.AltModifier:
        run_js('window.scrollBy(0, window.innerHeight)')
    
    # Alt+I - Page up
    elif key == Qt.Key.Key_I and modifiers == Qt.KeyboardModifier.AltModifier:
        run_js('window.scrollBy(0, -window.innerHeight)')
    
    # Alt+, - Back
    elif key == Qt.Key.Key_Comma and modifiers == Qt.KeyboardModifier.AltModifier:
        print('Going back...')
        web.back()
    
    # Alt+. - Forward
    elif key == Qt.Key.Key_Period and modifiers == Qt.KeyboardModifier.AltModifier:
        print('Going forward...')
        web.forward()
    
    # Ctrl+Shift+U - Copy URL
    elif key == Qt.Key.Key_U and modifiers == (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
        url_text = web.url().toString() if web.url() else ''
        copy_to_tmux_and_clipboard(url_text)
        print(f'Copied URL to tmux and clipboard: {url_text}')
    
    # Ctrl++ - Zoom in
    elif (key == Qt.Key.Key_Plus or key == Qt.Key.Key_Equal) and modifiers == Qt.KeyboardModifier.ControlModifier:
        factor = web.zoomFactor() + 0.1
        web.setZoomFactor(factor)
        print(f'Zoom: {factor:.1f}')
    
    # Ctrl+- - Zoom out
    elif key == Qt.Key.Key_Minus and modifiers == Qt.KeyboardModifier.ControlModifier:
        factor = web.zoomFactor() - 0.1
        web.setZoomFactor(factor)
        print(f'Zoom: {factor:.1f}')
    
    # Ctrl+0 - Zoom reset
    elif key == Qt.Key.Key_0 and modifiers == Qt.KeyboardModifier.ControlModifier:
        web.setZoomFactor(1.0)
        print('Zoom: 1.0')
    
    # Ctrl+F - Find
    elif key == Qt.Key.Key_F and modifiers == Qt.KeyboardModifier.ControlModifier:
        search = subprocess.run(['dmenu', '-p', 'Find'], input=find_text[0],
                                capture_output=True, text=True).stdout.strip()
        if search:
            find_text[0] = search
            page.findText(search, QWebEnginePage.FindFlag.FindCaseSensitively, lambda found: None)
            print(f'Searching: {search}')
    
    # Ctrl+N - Find next
    elif key == Qt.Key.Key_N and modifiers == Qt.KeyboardModifier.ControlModifier:
        if find_text[0]:
            page.findText(find_text[0], QWebEnginePage.FindFlag.FindCaseSensitively, lambda found: None)
            print('Find next')
    
    # Ctrl+Shift+N - Find previous
    elif key == Qt.Key.Key_N and modifiers == (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
        if find_text[0]:
            page.findText(find_text[0], QWebEnginePage.FindFlag.FindBackward | QWebEnginePage.FindFlag.FindCaseSensitively, lambda found: None)
            print('Find previous')
    
    else:
        return False
    return True

# Install event filter for key handling
class KeyFilter(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyPress:
            if on_key(event):
                return True
        return False

key_filter = KeyFilter()
app.installEventFilter(key_filter)

# Load URL and show
win.setWindowTitle('qtbrowser')
win.show()

sys.exit(app.exec())
