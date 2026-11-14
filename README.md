# NDM — native download manager

NDM is a **desktop application**, built in Python with the native
[nexa-ui](https://pypi.org/project/nexa-ui/) widget toolkit. Its interface follows
Internet Download Manager's working layout: menus, an icon toolbar, a category
tree, a detailed download table, and a status bar.

**There is no React app, web interface, browser extension, webview, or HTTP API.**
The background daemon communicates over an authenticated Unix domain socket
(Windows: named pipe). Closing the window leaves downloads running.

## Features

- **IDM-style native desktop:** File / Downloads / View / Tools / Help menus;
  Add URL, Spotify, Grabber, Resume, Stop, Stop All, Delete, Properties, Options;
  All Downloads / Unfinished / Finished / Compressed / Documents / Music / Video
  / Images / Other categories; sortable columns and a resizable divider.
- **Download engine:** up to eight HTTP range connections per file, three
  simultaneous files by default, persistent resume with ETag/Last-Modified
  validation, safe filenames, per-category folders, and an optional shared speed
  limit. Single-connection fallback when ranges are unavailable.
- **spotDL integration:** public Spotify track, album, and playlist links are
  expanded with the upstream spotDL Python API, previewed in a native track
  table, then queued individually. Audio is matched to YouTube Music and
  converted/tagged by spotDL. Formats: MP3, M4A, FLAC, Opus, Ogg.
- **yt-dlp media grabber:** authorized media-page and direct-media inspection,
  format selection, and background extraction.
- **Background capture:** opt-in link-inbox watching while the window is closed;
  opt-in clipboard capture while the desktop window is open. Optional host
  allowlist applies to both. Linux systemd and macOS LaunchAgent installers.
- **Desktop preferences:** light/dark/system theme, custom accent colors,
  connection and concurrency limits, destination folders, and audio settings.
- **Files stay local:** completed files open in your desktop applications.
  The offline-audio dialog lists your music/download folders and exports M3U
  playlists. It does not run an audio streaming server.

### Spotify boundary

NDM integrates [spotDL](https://github.com/spotdl/spotify-downloader), **not a
Spotify protected-stream ripper**. Spotify supplies track metadata; spotDL finds
a corresponding public audio source, normally YouTube Music. The matched
recording may differ, may be unavailable, or may fail. This is not Spotify
Premium offline caching and does not defeat DRM. Use it only for content you own
or have permission to save, subject to the source's terms.

No spotDL web UI is launched. NDM imports its downloader/search API directly;
spotDL's own web dependencies remain installed as part of the upstream package,
but NDM never starts them.

## Install and run

Python **3.11–3.15** and a normal desktop session are required. Nexa is a native
UI library, so its window cannot be hosted as a browser preview.

```bash
uv sync
uv run ndm                 # opens the desktop and starts its daemon if needed

# Or, without uv:
python -m pip install -e .
ndm
```

Install **FFmpeg** on your PATH for spotDL conversion/tagging and yt-dlp merging.
Nexa bundles its native UI runtime in the `nexa-ui` package. Linux desktops also
need working OpenGL/display libraries; the screenshot mode below does not need
a display server.

### Commands

```bash
ndm desktop
ndm daemon                           # local IPC only; no TCP port
ndm add https://example.org/file.zip --filename file.zip
ndm status                           # JSON queue snapshot
ndm pause <task-id>
ndm resume <task-id>
ndm remove <task-id>                 # keeps completed files
ndm spotify https://open.spotify.com/playlist/... --authorized
ndm stop                             # stop the background daemon
```

All commands accept `--data-dir PATH`. `NDM_HOME` sets the default state folder.
Multiple independent data directories can run separate daemons.

For public-only operation, `ndm daemon --block-private` rejects local/private
network targets. Local destinations are allowed by default for a desktop tool;
only add URLs you trust. Credentials embedded in URLs are rejected.

### Start at sign-in

```bash
ndm service install --start          # Linux systemd user service / macOS LaunchAgent
ndm service uninstall
```

Linux unattended operation without a logged-in session may additionally require
`loginctl enable-linger "$USER"`. On Windows, create a per-user Task Scheduler
task running `ndm.exe daemon --data-dir "C:\your\state\folder"`.

Closing the desktop window is not the same as `ndm stop`: the daemon continues.
Use **File → Stop daemon and exit** when you want both to stop.

## Using automatic media capture

Open **Options → Capture**. Capture is off by default.

1. Enable automatic capture.
2. Optionally set an allowed-host list.
3. Enable the inbox watcher for background operation.
4. Put a `.txt` file containing an HTTP(S) media URL, or an Internet Shortcut
   `.url` file, into the inbox directory shown in Options.

Example `.url` file:

```ini
[InternetShortcut]
URL=https://example.org/authorized/video.mp4
```

The daemon marks processed files `.processed`. Spotify inbox links are rejected;
use the Spotify dialog or CLI so the spotDL permission/queue path is explicit.
Clipboard monitoring requires the open desktop window and only captures direct
media extensions. There is no browser interception extension.

## Using spotDL

1. Click **Spotify** on the toolbar.
2. Paste a public `open.spotify.com` track, album, or playlist URL.
3. Confirm permission and click **Find Tracks**.
4. Select rows and click **Queue Selected**.

The daemon processes tracks using its normal concurrency limit. Each row shows
spotDL matching/conversion progress. Exact byte size and ETA are unavailable
until a matched file finishes. Stop is cooperative: a conversion may finish
before the track pauses. A paused/failed spotDL task starts the operation again;
byte-range resume applies to direct HTTP downloads, not spotDL jobs.

Output defaults to MP3 at 192 kbps. `auto` lets spotDL retain the matched source
bitrate where supported; a higher output bitrate does not improve source quality.
Public metadata access and YouTube Music availability depend on region, upstream
services, and occasionally a JavaScript runtime for yt-dlp. NDM does not request
Spotify account passwords or launch an OAuth web server. Advanced users can set
`SPOTIPY_CLIENT_ID` and `SPOTIPY_CLIENT_SECRET` for their own official Spotify API
application before launching NDM; default spotDL metadata access needs no login.

## Data and security

- Queue/settings/events: SQLite under `NDM_HOME` (default `~/.local/share/ndm`).
- Downloads: `~/Downloads/NDM`, split by category, configurable in Options.
- A store lock prevents two daemons from owning the same state directory.
- IPC uses a 32-byte private challenge key in `ipc.key`; the POSIX socket is
  owner-only. Windows uses a per-state-directory named pipe. There is no CORS,
  browser token, public bind address, or listening web port.
- HTTP redirects are revalidated. Direct downloads write `.part` files before
  atomically renaming the completed result.
- spotDL's upstream metadata cache is stored by spotDL in its usual per-user
  config directory. Temporary files also use that package's cache directory.

## Development and verification

```bash
uv sync
uv run pytest
uv run ruff check .
uv lock --check
uv build --wheel
```

The test suite uses local HTTP **test fixtures only**; the application itself has
no HTTP server. Native IPC, real-file integrity, pause/resume across daemon
restart, validator invalidation, capture opt-in, settings validation, media
extraction, and spotDL adapter/queue controls are covered. Tests do not contact
Spotify or download copyrighted audio.

Render the real Nexa UI without a display server:

```bash
NDM_HOME=.local/ndm-desktop ndm desktop --screenshot .local/ndm.png
```

This screenshot is a native raster render, not a website. To exercise the UI,
run `ndm` in a desktop session on your own machine.

### Layout

```text
src/ndm/
  desktop.py      native Nexa menus, toolbar, tree, tables, dialogs
  daemon.py       headless queue owner and validated local commands
  ipc.py          Unix socket / Windows pipe transport
  engine.py       segmented HTTP, queue, resume, capture, media workers
  spotify.py      upstream spotDL metadata/search/download adapter
  media.py        direct media/HTML inspection and yt-dlp
  models.py       validated settings and URL rules
  store.py        SQLite persistence and exclusive state ownership
  cli.py          native desktop, daemon, queue and service commands
```

## Current limits

- Native Windows/macOS/Linux sessions are the target. Linux rendering has been
  exercised here through Nexa's headless software renderer; Windows named pipes
  and macOS services are implemented but require testing on those systems.
- No scheduler, torrent protocol, system tray, or browser interception.
- A server must support byte ranges for segmented resume. Changed remote files
  are restarted rather than mixing incompatible bytes.
- spotDL is a metadata-to-source matcher, not guaranteed lossless or exact-track
  reproduction. Provider failures are shown in the row's Properties dialog.
- No Spotify DRM circumvention, protected-stream extraction, or Premium offline
  export is provided.

## License

MIT for NDM. Nexa, spotDL, yt-dlp, and their dependencies retain their own
licenses. The upstream spotDL project is MIT-licensed.
