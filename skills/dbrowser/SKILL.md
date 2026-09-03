---
name: dbrowser
description: >
  Headless WebKit browser via Unix socket IPC. Use for sites requiring JavaScript.

  **This is a learning/investigation tool. User scripts should use curl/lynx.**
---

# dbrowser - Headless WebKit Browser

## Overview

- **Browser runs externally** via Unix socket at `/run/user/1000/tmp/dbrowser.sock`
- **Single page only** - no tabs, no parallel loads
- **JavaScript** - renders SPAs, dynamic content

## Important: Investigation Tool Only

dbrowser helps you **learn how a site works**:
- Discover data structure and selectors
- Understand lazy-loaded content patterns
- Test JavaScript interactions

**User scripts must use curl/lynx.** Only use dbrowser as a dependency if the site is impossible to scrape any other way.

## How to Use

1. **Check if running** - try `status`. If socket fails, ask user to start
2. **Load URL** - `load-url <url>`
3. **Wait for load** - poll `status` until `"loading":false`
4. **Investigate** - use eval-js to explore, discover selectors, understand structure

## Investigation Example

```bash
# Load page
echo '{"command": ["load-url", "https://example.com"]}' | nc -U /run/user/1000/tmp/dbrowser.sock

# Wait (adapt to hardware)
for i in {1..60}; do sleep 2; result=$(echo '{"command": ["status"]}' | nc -U /run/user/1000/tmp/dbrowser.sock); 
  echo "$result" | grep -q '"loading":false' && break; 
done

# Explore page structure
echo '{"command": ["eval-js", "document.body.innerText"]}' | nc -U /run/user/1000/tmp/dbrowser.sock
echo '{"command": ["eval-js", "document.querySelectorAll('a').length"]}' | nc -U /run/user/1000/tmp/dbrowser.sock

# Check network requests for API endpoints
echo '{"command": ["list-network-requests", "20"]}' | nc -U /run/user/1000/tmp/dbrowser.sock
```

## Adapt and Experiment

- **Slow hardware?** Wait longer
- **Lazy content?** Try `window.scrollBy(0, 500)` via eval-js
- **Unclear structure?** Test eval-js, check network requests
- **Format unknown?** Read actual responses

## Commands

| Command | Description |
|---------|-------------|
| `status` | URL, title, loading state |
| `load-url <url>` | Navigate |
| `eval-js <code>` | Execute JS, return result |
| `back` / `forward` | History |
| `list-network-requests [max]` | Network debugging |
| `device [profile]` | Device emulation |
| `set-user-agent <ua>` | Custom UA |
| `screenshot` | PNG (vision only) |
| `help` | All commands |

## When to Use

| Use curl/lynx | Use dbrowser |
|---------------|--------------|
| Static pages | JavaScript-rendered only |
| User scripts | AI investigation |
| Production code | When no alternative |
