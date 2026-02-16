# dbrowser

A minimalist WebKit2 browser.

## Requirements

- Python 3
- GTK 3
- WebKit2GTK 4.0+
- dmenu (for URL input and find)

Install on Debian/Ubuntu:
```bash
apt install python3-gi gir1.2-webkit2-4.1
```

## Usage

```bash
./browser.py <URL>
```

## Keybindings

| Key | Action |
|-----|--------|
| F1 | Show help |
| Ctrl+Q | Quit |
| F5 / Ctrl+R | Reload page |
| F12 | Developer tools |
| Ctrl+P | Print dialog |
| Ctrl+Shift+P | Save as PDF |
| Ctrl+S | Save as HTML (MHTML) |
| Ctrl+Shift+S | Save screenshot (PNG) |
| Ctrl+L | Change URL (dmenu) |
| Ctrl+B | Open bookmark (dmenu) |
| Ctrl+G | Load URL from tmux buffer |
| Ctrl+Shift+G | Load URL from clipboard |
| Alt+Left / Alt+H / Alt+, | Go back |
| Alt+Right / Alt+L / Alt+. | Go forward |
| Alt+J | Scroll down |
| Alt+K | Scroll up |
| Alt+U | Page down |
| Alt+I | Page up |
| Ctrl+Shift+C | Copy page text |
| Ctrl+Shift+U | Copy current URL |
| Ctrl++ | Zoom in |
| Ctrl+- | Zoom out |
| Ctrl+0 | Zoom reset |
| Ctrl+F | Find in page |
| Ctrl+N | Find next |
| Ctrl+Shift+N | Find previous |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `DBROWSER_DOWNLOAD_DIR` | Download directory (default: `~/Downloads`) |
| `DBROWSER_CACHE_DIR` | Custom cache directory |
| `DBROWSER_NO_CACHE=1` | Disable disk cache |
| `DBROWSER_NO_JS=1` | Disable JavaScript |
| `DBROWSER_NO_IMAGES=1` | Don't load images |
| `DBROWSER_LOW_MEM=1` | Minimize memory usage |
| `DBROWSER_MEMORY_LIMIT` | Memory limit in MB |
| `DBROWSER_FAST=1` | Faster loading (DNS prefetch, page cache) |
| `DBROWSER_WEBGL=1` | Enable WebGL |
| `DBROWSER_SIZE` | Window size WxH (default: `800x600`) |
| `DBROWSER_DEBUG=1` | Show key events |
| `BOOKMARKS_FILE` | Bookmarks file path (default: `~/data/links.txt`) |

## License

MIT License

Copyright (c) DanielT

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
