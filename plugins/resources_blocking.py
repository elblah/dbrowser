# plugins/resources_blocking.py - Block resources using WebKit2 UserContentFilter
# ruff: noqa: F821
# Access: web, ctx, win, settings

import os
import json
from gi.repository import GLib, WebKit2

_filter_loaded = set()

def add_filter(filter_id, rules, manager):
    """Load or create a content filter."""
    if filter_id in _filter_loaded:
        return
    _filter_loaded.add(filter_id)
    
    cache_dir = os.path.join(os.path.expanduser('~/.cache/dbrowser'), filter_id)
    # Clear stale cache to force rebuild
    if os.path.exists(cache_dir):
        import shutil
        shutil.rmtree(cache_dir)
    os.makedirs(cache_dir, exist_ok=True)
    
    store = WebKit2.UserContentFilterStore.new(cache_dir)
    
    def on_saved(store, result, user_data):
        try:
            filt = store.save_finish(result)
            manager.add_filter(filt)
            print(f'resources_blocking: {filter_id} applied')
        except GLib.Error as e:
            print(f'resources_blocking: {filter_id} error: {e}')
    
    data = GLib.Bytes.new(json.dumps(rules).encode())
    store.save(filter_id, data, None, on_saved, None)

def init_filters():
    manager = web.get_user_content_manager()
    
    if os.getenv('DBROWSER_NO_FONTS'):
        add_filter('dbrowser-fonts', [
            {"action": {"type": "block"}, "trigger": {"url-filter": ".*", "resource-type": ["font"]}}
        ], manager)
    
    if os.getenv('DBROWSER_NO_CSS'):
        add_filter('dbrowser-css', [
            {"action": {"type": "block"}, "trigger": {"url-filter": ".*", "resource-type": ["style-sheet"]}}
        ], manager)
    
    if os.getenv('DBROWSER_NO_IMAGES'):
        add_filter('dbrowser-images', [
            {"action": {"type": "block"}, "trigger": {"url-filter": ".*", "resource-type": ["image"]}}
        ], manager)

init_filters()