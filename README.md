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
- **yt-dlp media grabber:** media-page and direct-media inspection,
  format selection, background extraction, and one-click **MP3 conversion**
  (192 kbps audio extraction via FFmpeg). Every inspected item shows its file
  size — exact when the source reports it, `~` when estimated, and the real
  byte count replaces the estimate once the download finishes.
- **Optional Firefox cookies:** a boolean Capture option automatically reads
  local Firefox cookies for authenticated downloads, media inspection, and
  spotDL audio matching. Off by default; no extension or cookie file is needed.
- **Background capture:** opt-in link-inbox watching while the window is closed;
  opt-in clipboard capture while the desktop window is open. Optional host
  allowlist applies to both. Linux systemd and macOS LaunchAgent installers.
- **Fully custom colors:** light/dark/system base plus 34 independent semantic
  color overrides (surfaces, text, borders, accents, status colors, controls,
  hover/active states, focus ring, scrim, and chart series). Pick visually or enter
  `#RRGGBB` / `#RRGGBBAA`; reset one token or the whole palette.
- **HTTP and SOCKS5 proxy:** one daemon-wide proxy for segmented downloads,
  media inspection/extraction, and spotDL metadata/audio. HTTP, HTTPS, SOCKS5,
  and SOCKS5H URLs; optional username/password; remote DNS by default.
- **Ctrl+V URL paste:** copy a link anywhere, press Ctrl+V in NDM, and the Add
  Download dialog opens prefilled. Spotify links open the spotDL dialog instead.
  Focused text fields retain normal clipboard paste behavior.
- **Desktop preferences:** connection and concurrency limits, destination folders,
  capture options, and audio settings.
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

Install **FFmpeg** on your PATH for spotDL conversion/tagging, yt-dlp merging,
and Media Grabber MP3 conversion. Without FFmpeg the MP3 checkbox is disabled
and the daemon rejects audio-extraction requests with an actionable error.
Nexa bundles its native UI runtime in the `nexa-ui` package. Linux desktops also
need working OpenGL/display libraries.

### Commands

```bash
ndm desktop
ndm daemon                           # local IPC only; no TCP port
ndm add https://example.org/file.zip --filename file.zip
ndm status                           # JSON queue snapshot
ndm pause <task-id>
ndm resume <task-id>
ndm remove <task-id>                 # keeps completed files
ndm spotify https://open.spotify.com/playlist/...
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

## Proxy and appearance

Open **Options → Proxy**, enable the proxy, enter a URL such as
`http://proxy.local:8080` or `socks5://127.0.0.1:1080`, optionally add credentials,
and click **OK**. SOCKS5 remote DNS (`socks5h`) is the default; disable it only when
the proxy cannot resolve hostnames. An enabled NDM proxy overrides ambient proxy
environment variables. Proxy passwords are stored in the local NDM state directory
and masked in the UI/CLI status output; protect that directory like other secrets.
The status bar shows the active proxy without credentials.

Open **Options → Appearance** for the full palette. Valid color edits apply
immediately; incomplete hex text stays editable without being saved. Picker edits
preserve an existing alpha value. **Reset** beside a row restores that token and
**Reset palette** restores the base theme. The base accent presets remain
available for a quick starting point.

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
URL=https://example.org/media/video.mp4
```

The daemon marks processed files `.processed`. Spotify inbox links are rejected;
use the Spotify dialog or CLI so the spotDL metadata/matching path is explicit.
Clipboard monitoring requires the open desktop window and only captures direct
media extensions. There is no browser interception extension.

### Firefox cookies

Open **Options → Capture**, check **Automatically use my Firefox cookies**, and
click **OK**. The persisted boolean setting is `firefox_cookies` (default
`false`). It works independently of automatic capture: manual downloads and
Media Grabber inspections can use your existing Firefox login even when inbox
and clipboard capture are off.

When enabled, NDM reads a fresh cookie snapshot from the auto-detected Firefox
profile for each new HTTP download, HTML inspection, yt-dlp operation, or spotDL
YouTube Music search/audio transfer. This also works for downloads owned by the
background daemon. Firefox can stay open; yt-dlp reads a temporary copy of its
cookie database rather than modifying it. Only cookies matching the
destination's domain, path, and security rules are sent. This does not bypass
DRM or make an unauthorized account authorized.

Cookie values are not saved in NDM settings, queue entries, or a cookie-export
file. Turn the checkbox off to stop reading Firefox cookies for new operations.
If no readable Firefox profile is available, NDM shows an actionable error;
install/sign in to Firefox or disable the option. For spotDL, the cookies apply
to the YouTube Music matching/download clients; Spotify metadata still uses
spotDL's normal anonymous or official API credentials, not browser cookies.

## Using the Media Grabber

1. Click **Grabber** on the toolbar (or paste a media URL with Ctrl+V and open
   the grabber).
2. Enter a page URL, a direct media link, or a yt-dlp-supported site URL and
   click **Find Media**.
3. Each result row shows the format quality, file size, and a note. Sizes are
   exact when the source reports a byte count, prefixed with `~` when estimated
   (extractor approximation, duration × bitrate, or a 192 kbps MP3 projection),
   and shown as **Unknown** when the server provides no length. HEAD and
   one-byte ranged GET probes fill in sizes for direct and page-scanned links
   without downloading the body.
4. Optionally check **Convert to MP3** to extract audio at 192 kbps instead of
   saving the original video. This requires FFmpeg on PATH; the checkbox is
   disabled and the daemon refuses the request when FFmpeg is missing. MP3 and
   video downloads of the same URL are queued as separate tasks.
5. Click **Download** (or **Download MP3**) to queue the item. The background
   daemon handles extraction/conversion; the Size column and Properties dialog
   show the estimate until the real file size is known at completion.

Protected streams and DRM are never bypassed. Spotify links are redirected to
the spotDL dialog instead.

## Using spotDL

1. Click **Spotify** on the toolbar.
2. Paste a public `open.spotify.com` track, album, or playlist URL (Ctrl+V also
   opens this dialog automatically when the clipboard contains a Spotify link).
3. Click **Find Tracks**. A busy indicator and elapsed time stay visible while
   spotDL expands the list; large playlists can take a while. Results show the
   track count and the playlist/album title when available. Previews are capped
   at 500 tracks, with a warning when the list is longer.
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
- Firefox cookies are opt-in, read locally, kept in memory, and matched to the
  request destination, including spotDL's YouTube Music clients. NDM does not
  persist cookie values or transmit them to an NDM server. Upstream temporary
  database copies are removed after reading.
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
extraction, spotDL adapter/queue controls, proxy transport wiring, full-palette
rendering, and global Ctrl+V routing are covered. Tests do not contact
Spotify or download copyrighted audio.

Run `ndm` or `ndm desktop` in a desktop session on your own machine. The desktop
command opens the native window directly; it has no screenshot/headless mode.

### Layout

```text
src/ndm/
  desktop.py      native Nexa menus, toolbar, tree, tables, dialogs
  daemon.py       headless queue owner and validated local commands
  ipc.py          Unix socket / Windows pipe transport
  engine.py       segmented HTTP, queue, resume, capture, media workers
  spotify.py      upstream spotDL metadata/search/download adapter
  media.py        direct media/HTML inspection and yt-dlp
  models.py       validated settings, colors, proxy URLs, and URL rules
  network.py      shared proxy configuration and opt-in Firefox cookies
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
