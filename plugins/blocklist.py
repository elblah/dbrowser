# plugins/blocklist.py - Block ads/trackers via hosts file
# ruff: noqa: F821
# Access: web, ctx, win, settings

import os
import time
import pickle
import urllib.request
import urllib.parse

# Only load if DBROWSER_NO_ADS=1 (or DBROWSER_BLOCK_MODE for AI mode)
if os.getenv('DBROWSER_NO_ADS') or os.getenv('DBROWSER_BLOCK_MODE'):
    BLOCKLIST_URL = 'https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts'
    BLOCKLIST_CACHE = os.path.expanduser('~/.cache/dbrowser/blocklist.pkl')
    CACHE_MAX_AGE = 3600  # 1 hour

    _blocklist = None
    _block_fonts = os.getenv('DBROWSER_NO_FONTS')

    _FONT_EXTENSIONS = ('.woff', '.woff2', '.ttf', '.otf', '.eot')

    def _get_blocklist():
        global _blocklist
        if _blocklist is not None:
            return _blocklist
        
        # Check cache
        if os.path.exists(BLOCKLIST_CACHE):
            age = time.time() - os.path.getmtime(BLOCKLIST_CACHE)
            if age < CACHE_MAX_AGE:
                with open(BLOCKLIST_CACHE, 'rb') as f:
                    _blocklist = pickle.load(f)
                    print(f'Blocklist: {len(_blocklist)} domains (cached)')
                    return _blocklist
        
        # Download fresh
        print('Downloading blocklist...')
        try:
            with urllib.request.urlopen(BLOCKLIST_URL, timeout=60) as resp:
                data = resp.read().decode()
            
            blocked = set()
            for line in data.split('\n'):
                if line.startswith('0.0.0.0') or line.startswith('127.0.0.1'):
                    parts = line.split()
                    if len(parts) > 1:
                        blocked.add(parts[1])
            
            # Cache it
            os.makedirs(os.path.dirname(BLOCKLIST_CACHE), exist_ok=True)
            with open(BLOCKLIST_CACHE, 'wb') as f:
                pickle.dump(blocked, f)
            
            _blocklist = blocked
            print(f'Blocklist: {len(blocked)} domains')
            return blocked
        except Exception as e:
            print(f'Blocklist download failed: {e}')
            return set()

    def _is_blocked(domain):
        blocklist = _get_blocklist()
        if domain in blocklist:
            return True
        # Check parent domains (subdomain matching)
        parts = domain.split('.')
        for i in range(1, len(parts)):
            parent = '.'.join(parts[i:])
            if parent in blocklist:
                return True
        return False

    def _is_font_request(uri):
        if not _block_fonts:
            return False
        return any(uri.endswith(ext) for ext in _FONT_EXTENSIONS)

    def _on_decide_policy(webview, decision, decision_type):
        uri = None
        
        # Get URI based on decision type
        if decision_type == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            try:
                action = decision.get_navigation_action()
                if action:
                    req = action.get_request()
                    if req:
                        uri = req.get_uri()
            except Exception:
                pass
        elif decision_type == WebKit2.PolicyDecisionType.RESPONSE:
            try:
                resp = decision.get_response()
                if resp:
                    uri = resp.get_uri()
            except Exception:
                pass
        
        if uri:
            domain = urllib.parse.urlparse(uri).netloc
            if domain and _is_blocked(domain):
                decision.ignore()
                return True
            if _is_font_request(uri):
                decision.ignore()
                return True
        return False

    web.connect("decide-policy", _on_decide_policy)
    print(f'Blocklist plugin active: {_get_blocklist().__len__()} domains')
else:
    print('Blocklist plugin disabled (set DBROWSER_NO_ADS=1 to enable)')