# Subix Studio

**SaaS-level V2Ray Subscription Testing Suite**

A self-contained web application for automated bulk testing and management of V2Ray subscriptions.

## Features

### ✨ Core Features
- **Subscription Management**: Add subscription links and auto-fetch hundreds of configs
- **Intelligent Testing**: Automated connection testing with ping measurements
- **Smart Retirement**: Configs that fail 3+ consecutive tests are automatically retired
- **Engine Mode**: Continuous testing mode that never stops
- **Advanced Filtering**: Search, sort, and filter configs by any attribute
- **Multi-Select Operations**: Batch test, export, or delete multiple configs
- **QR Code Generation**: Instant QR codes for each config in preview pane
- **Export Functionality**: Export selected configs to text files

### 🎯 Tabs

| Tab | Description |
|-----|-------------|
| **Subscriptions** | Manage subscription links, view config counts |
| **All Configs** | View all configs with full sorting and filtering |
| **Working Configs** | Only configs that passed testing with success rates |
| **Retired** | Configs that failed 3+ consecutive tests |
| **Preferences** | Auto mode, engine mode, and testing settings |

### ⚙️ Operating Modes

**Auto Mode** (Default)
- Periodically re-tests working configs (configurable interval)
- Runs in background on schedule

**Engine Mode** (Aggressive)
- Continuously tests configs without stopping
- Prioritizes untested configs first
- Then continuously re-tests working configs
- Perfect for finding working configs quickly

**Manual Mode**
- No automatic testing
- Test only when you click "Test All" or select configs

## Quick Start

### Installation

```bash
# No dependencies required! Pure Python + built-in libraries
python3 main.py --port 8080
```

### Usage

1. **Open browser**: `http://localhost:8080`

2. **Add a subscription**:
   - Go to "Subscriptions" tab
   - Click "+ Add Subscription"
   - Enter name and URL
   - Click "Update" to fetch configs

3. **Add single configs**:
   - Go to "All Configs" tab
   - Click "+ Add Single"
   - Paste config URI

4. **Enable Engine Mode** (optional):
   - Go to "Preferences" tab
   - Enable "Engine Mode"
   - Sit back and watch it test everything continuously

5. **Select and export**:
   - Select configs with checkboxes (Ctrl+click, Shift+click)
   - Right-click for context menu
   - Click "Export Selected" to save to file

## API Reference

### Get Statistics
```bash
curl http://localhost:8080/api/stats
```

### Add Subscription
```bash
curl -X POST http://localhost:8080/api/subscriptions \
  -H "Content-Type: application/json" \
  -d '{"name":"My Sub","url":"https://example.com/sub"}'
```

### Update Subscription (Fetch Configs)
```bash
curl -X POST http://localhost:8080/api/subscriptions/1/update
```

### Get All Configs (with filters)
```bash
curl "http://localhost:8080/api/configs?status=working&search=vless&sort_by=last_ping_ms&sort_order=asc"
```

### Add Single Config
```bash
curl -X POST http://localhost:8080/api/configs \
  -H "Content-Type: application/json" \
  -d '{"uris":["vless://..."]}'
```

### Start Testing
```bash
# Test specific configs
curl -X POST http://localhost:8080/api/test \
  -H "Content-Type: application/json" \
  -d '{"config_ids":[1,2,3]}'

# Test all configs
curl -X POST http://localhost:8080/api/test \
  -H "Content-Type: application/json" \
  -d '{}'
```

### Enable Engine Mode
```bash
curl -X POST http://localhost:8080/api/settings \
  -H "Content-Type: application/json" \
  -d '{"engine_mode":true,"auto_mode":false,"test_interval_minutes":30,"max_concurrent_tests":10,"test_timeout_seconds":15,"failure_threshold":3,"auto_deduplicate":true,"auto_test_new_configs":true,"speed_test_bytes":100000,"dark_mode":true}'
```

### Get QR Code
```bash
curl "http://localhost:8080/api/qrcode?uri=vless://..."
```

## Keyboard Shortcuts

- **Click** config: Show details in preview pane
- **Ctrl+Click**: Multi-select configs
- **Shift+Click**: Range select configs
- **Right-Click**: Context menu with batch actions
- **Esc**: Close modals

## Database

All data is stored in `subix_studio.db` (SQLite).

Tables:
- `subscriptions`: Subscription links and metadata
- `configs`: All V2Ray configs with test results
- `test_history`: Historical test results
- `settings`: User preferences

## Configuration

Settings can be adjusted in the Preferences tab:

| Setting | Default | Description |
|---------|---------|-------------|
| Auto Mode | On | Periodic re-testing |
| Engine Mode | Off | Continuous testing |
| Test Interval | 30 min | How often to re-test (Auto Mode) |
| Concurrent Tests | 10 | Parallel test connections |
| Test Timeout | 15s | Max wait time per test |
| Failure Threshold | 3 | Failures before retirement |
| Auto Deduplicate | On | Remove duplicate configs |
| Auto Test New | On | Test configs when added |

## Architecture

- **Backend**: Pure Python HTTP server (no Flask/Django needed)
- **Database**: SQLite with connection pooling
- **Frontend**: Vanilla JS (no CDN, fully self-contained)
- **Testing**: Integrates with `python_v2ray` library
- **Scheduling**: Background thread for auto/engine modes

## Performance

- Handles **thousands** of configs efficiently
- Multi-threaded concurrent testing
- Indexed database queries
- Deduplicated configs by URI hash
- Memory-efficient streaming

## Troubleshooting

**Subscription parser finds 0 configs**
- Fixed! Now uses direct base64 decoding without library dependency
- Works with standard base64-encoded subscription links

**Testing not working**
- Make sure `python_v2ray` library is installed
- If not installed, app runs in simulation mode (for testing UI)

**Port already in use**
```bash
# Kill existing process
pkill -f main.py
fuser -k 8080/tcp

# Start on different port
python3 main.py --port 8888
```

## Command Line Options

```bash
python3 main.py --host 0.0.0.0 --port 8080
```

- `--host`: Bind address (default: 0.0.0.0)
- `--port`: Port to listen on (default: 8080)

## License

Free to use for V2Ray subscription testing.