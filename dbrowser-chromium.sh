#!/bin/bash
# dbrowser-chromium - drop-in replacement for dbrowser (webkit) using chromium
# Same socket: /run/user/1000/tmp/dbrowser.sock
# Same JSON IPC protocol. Chromium is auto-launched if CDP is down.
exec python3 "$(dirname "$0")/chromium-server.py" "$@"
