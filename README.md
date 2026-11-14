# ndm.

**Your downloads, in flow.**

NDM is an IDM-style personal download manager written in Python, with a native
desktop UI built using **[nexa-ui](https://github.com/x017/nexa)** and an
independent background download daemon. A React browser companion shares the
same engine, queue, history, and preferences.

The original project goal was a Python alternative to IDM with media-site
support. This implementation keeps that goal while being explicit about what
sites and permissions allow.

## What works

- **Native Nexa desktop app** — real `nexa-ui` widgets, not a webview or a
  lookalike component library.
- **Segmented HTTP downloads** — up to eight connections per file when the
  server supports byte ranges; automatic single-connection fallback.
- **Persistent queue** — SQLite-backed tasks, settings, and activity history.
- **Pause and resume** — partial data and range validators survive a daemon
  restart. Changed ETags, lengths, or modification dates restart safely.
- **Concurrency and bandwidth controls** — simultaneous-file limit, connections
  per file, and a shared HTTP speed limit.
- **Background daemon** — closing the desktop window or browser does not stop a
  running daemon. Optional sign-in service on Linux and macOS.
- **Media grabber** — direct audio/video links, media elements in HTML pages, and
  supported sites through `yt-dlp`, including YouTube.
- **Opt-in automatic capture** — a Chromium browser companion, desktop clipboard
  capture, and a watched link inbox. Capture is off by default.
- **Offline music shelf** — scans your own music folder and downloaded audio,
  plays local files, and exports an M3U playlist. No cloud upload.
- **Customizable colors** — light/dark/system modes, six presets, and any hex
  accent, shared between the desktop and browser interfaces.
- **Browser preview** — responsive dashboard, search, filters, bulk actions,
  details drawer, real progress, activity chart, and local sample transfers.

### Spotify and protected media

NDM recognizes Spotify links and offers **Open in Spotify**. It does **not** rip
Spotify streams, remove DRM, or convert protected tracks to MP3. Spotify Premium's
offline mode is the supported way to save its catalog for an outage.

For your own audio, authorized direct links, and permitted media-site downloads,
NDM provides the normal download and offline-library tools. Download only content
you own or have permission to save, and respect each site's terms.

## Quick start

Requires **Python 3.11+**. The native desktop needs a graphical display; Linux
uses X11/OpenGL when available and Nexa's software renderer for headless use.

### With uv

```bash
uv sync
uv run ndm                 # opens the native desktop; starts a daemon if needed
```

### With pip

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .
ndm
```

The built browser assets are included under `src/ndm/static`, so a normal Python
installation does not require Node.js.

### Run the daemon by itself

```bash
ndm daemon                 # loopback-only API on port 8765
```

Leave this process running, use the desktop app, or open the companion:

```bash
ndm web                    # http://127.0.0.1:8765
```

`ndm daemon` already serves the browser companion; `ndm web` is an explicit alias
for the same server. For a separate browser UI process and daemon, use `ndm web`
with a different port/data directory only if you understand that each data
directory must have exactly one owning daemon.

### Queue from the command line

```bash
ndm add "https://example.com/archive.zip"
ndm add "https://example.com/my-song.mp3" --authorized --paused
ndm status
```

`--authorized` confirms ownership or permission for audio/video. The CLI talks to
an already running daemon; start one first.

### Hosted-style sample preview

```bash
NDM_HOME=.local/ndm-demo ndm web --host 0.0.0.0 --port 3000 --demo
```

The demo generates **real local sample files** and transfers them through the
actual segmented engine. Sample rows are labeled in the UI. The demo throttles
its fixture responses so progress is visible; this is not a benchmark. Demo files
and state live under the chosen `NDM_HOME`, not in Git.

Only bind to `0.0.0.0` behind a trusted preview proxy or access-controlled network.
The default daemon binds to loopback. The hosted demo rejects private-network
targets except its own registered fixture endpoint. It is a development preview,
not a hardened multi-user hosting service.

## Background startup

Install a user-level sign-in service:

```bash
ndm service install --start
```

- **Linux:** writes `~/.config/systemd/user/ndm.service`, then enables and starts
  it with `systemctl --user`. For operation without an active login, separately
  enable lingering: `loginctl enable-linger "$USER"`.
- **macOS:** writes `~/Library/LaunchAgents/io.ndm.daemon.plist` and bootstraps it.
- **Windows:** create a Task Scheduler task at sign-in running your installed
  `ndm.exe` with argument `daemon`. Run as your own user, not administrator.

Remove the Linux/macOS service without deleting downloads:

```bash
ndm service uninstall
```

The service uses the Python executable and data directory selected at install
time. If you move the virtual environment, uninstall and reinstall the service.
The desktop app starts a detached daemon if none is listening; it deliberately
leaves that daemon running when the window closes.

## Automatic media capture

All capture paths require the master **Enable media capture** switch.

1. **Browser companion:** open Media grabber → Set up the browser companion.
   Download the ZIP, load it unpacked in `chrome://extensions`, and enter the
   endpoint and private token in its options. Enable it only while wanted.
   The companion sends media URLs and MIME types, not cookies or page contents.
2. **Clipboard:** enable clipboard capture in the desktop app. While that window
   is open, copied direct media URLs are submitted to the daemon. Browsers cannot
   silently monitor the clipboard, so this is a desktop feature.
3. **Inbox:** enable the inbox watcher and drop `.txt` or `.url` files containing
   authorized HTTP(S) media links into the directory shown in Media grabber.
   Processed files gain a `.processed` suffix. This works without an open UI.

An optional hostname allowlist restricts capture to selected domains and their
subdomains. Video/audio type filters apply. HLS/DASH manifests are passed to
yt-dlp rather than saving only a playlist text file. Protected manifests still
fail normally; NDM does not supply decryption keys.

## Media-site requirements

`yt-dlp` is included. Install **FFmpeg** on the machine running the daemon for
separate video/audio merging and audio extraction:

```bash
# Debian/Ubuntu
sudo apt install ffmpeg
# macOS with Homebrew
brew install ffmpeg
# Windows
winget install Gyan.FFmpeg
```

Without FFmpeg, NDM prefers progressive formats that already contain audio.
Media extraction depends on the site, region, login requirements, and current
yt-dlp support. Update yt-dlp if a previously supported site changes. Live streams,
private/account-gated content, and DRM are not guaranteed or bypassed.

The HTTP speed limit is applied live to segmented transfers. For yt-dlp jobs,
the current per-job share is applied when the job starts; restart a media job to
pick up a changed limit. Post-processing cancellation is cooperative.

## Preferences and storage

Defaults:

- State: `~/.local/share/ndm` (override with `NDM_HOME` or `--data-dir`)
- Downloads: `~/Downloads/NDM`, organized by media category
- Music library: `~/Music` plus downloaded audio
- API port: `8765`
- Three simultaneous files, four connections per file, unlimited HTTP bandwidth
- Capture off; light theme; Iris accent

Settings are editable in both interfaces. In hosted mode, download/music folders
are fixed to the preview workspace. Browser **Save file** retrieves a completed
file from the daemon; **Show in folder** is desktop/local-only.

Removing a queue entry deletes unfinished partial data. Removing a completed
entry keeps its file on disk. Filenames are sanitized and collisions are numbered.
Resume metadata uses `.part` files and server validators; a server that changes
the resource mid-transfer cannot silently mix versions.

## Architecture

```text
src/ndm/
  engine.py       threaded scheduler, segmented HTTP, resume, capture inbox
  store.py        SQLite persistence and single-owner data-directory lock
  models.py       validated settings, request models, URL/filename helpers
  media.py        HTML discovery, Spotify handling, yt-dlp integration
  server.py       FastAPI local API and static browser companion
  desktop.py      native Nexa UI, background state polling, clipboard capture
  cli.py          desktop/daemon/web/add/status/service commands
  demo.py         real, labeled local preview fixtures
  extension/      opt-in Chromium MV3 capture companion
  static/         built browser assets shipped with the Python package
web/              React companion source and browser smoke tests
tests/            engine, persistence, API, media, and validation tests
```

The daemon is the single owner of a data directory. Do not run two daemons
against the same state. The desktop and browser are clients of that daemon.

### Local API security

- Loopback binding by default.
- Mutations require the `X-NDM-Client: 1` custom header; browser mutations also
  check Origin against the request/forwarded host.
- The capture token is scoped to `/api/capture`; it cannot change settings,
  delete tasks, or read the queue.
- CORS preflight is limited to Chromium extension origins.
- Download URLs reject embedded credentials and non-HTTP schemes. Hosted mode
  rejects private/local targets.
- File responses are resolved from tracked download/library IDs, not arbitrary
  path query parameters.

Use HTTPS and an authenticated reverse proxy before exposing an API beyond a
trusted local/preview environment. The custom header is a CSRF boundary, not a
replacement for user authentication on a public service.

## Development and checks

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check src tests

npm install
npm run build                  # outputs to src/ndm/static
npm run test:ui                # expects the demo server on port 3000
```

The browser smoke test uses the npm-distributed headless Chromium bundle and
checks search, queue creation/removal, pause/resume, themes, Spotify handling,
capture opt-in, local music playback, and mobile navigation. It restores the
default theme/accent and removes its temporary queue entry.

Render the actual native UI without a display:

```bash
ndm desktop --screenshot ndm-desktop.png
```

You can also use Nexa's environment controls:

```bash
NEXA_HEADLESS=1 CUI_SCREENSHOT=ndm.png CUI_SCREENSHOT_FRAMES=12 ndm desktop
```

## Current limitations

- No Spotify DRM removal or protected-stream downloading.
- No browser extension for Firefox/Safari yet; the included companion targets
  Chromium MV3.
- No built-in torrent client, browser download interception, or system tray icon.
- Clipboard capture needs the desktop window; inbox/browser capture need only the
  daemon.
- A server must support byte ranges for segmented resume. Single-stream servers
  still download, but interrupted transfers may need to restart.
- The music shelf scans up to 500 audio files and does not read ID3 tags yet.
- Media-site success depends on yt-dlp, site permissions, and FFmpeg availability.
- The hosted preview is single-workspace and development-oriented.
