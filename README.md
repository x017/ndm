# NDM — native download manager

NDM is a **desktop application**, built in Python with the native
[nexa-ui](https://pypi.org/project/nexa-ui/) widget toolkit. Its interface follows
Internet Download Manager's working layout: menus, an icon toolbar, a category
tree, a detailed download table, and a status bar.

**There is no React app, web interface, webview, or HTTP API.** The background
daemon communicates over an authenticated Unix domain socket (Windows: named
pipe). Optional Firefox and Chrome/Chromium extensions use native messaging
(stdin/stdout), not a web server. Closing the window leaves downloads running.

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
  table, then queued individually. spotDL selects the YouTube Music match;
  NDM's own yt-dlp pipeline downloads and converts it, then spotDL metadata is
  embedded. Formats: MP3, M4A, FLAC, Opus, Ogg.
- **yt-dlp media grabber:** media-page and direct-media inspection,
  format selection, background extraction, and one-click **MP3 conversion**
  (192 kbps audio extraction via FFmpeg). Every inspected item shows its file
  size — exact when the source reports it, `~` when estimated, and the real
  byte count replaces the estimate once the download finishes.
- **Browser handoff and page grabs:** optional Firefox and Chrome/Chromium
  extensions intercept downloads with an in-app approval step and send the
  current YouTube video or Spotify track/album/playlist to the existing
  yt-dlp / spotDL pipelines. Brave and Edge can load the Chromium extension.
- **Optional browser cookies:** select Firefox, Chrome, Chromium, Brave, Edge,
  Vivaldi, Opera, or Safari in Capture options for authenticated downloads,
  media inspection, and spotDL audio matching. Off by default; no cookie export
  file is needed.
- **Background capture:** opt-in link-inbox watching while the window is closed;
  opt-in clipboard capture while the desktop window is open. Optional host
  allowlist applies to both. Linux systemd and macOS LaunchAgent installers.
- **Fully custom colors:** light/dark/system base plus 34 independent semantic
  color overrides (surfaces, text, borders, accents, status colors, controls,
  hover/active states, focus ring, scrim, and chart series). Pick visually or enter
  `#RRGGBB` / `#RRGGBBAA`; reset one token or the whole palette.
- **HTTP and SOCKS5 proxy:** one daemon-wide proxy for segmented downloads,
  media inspection/extraction, spotDL metadata/matching, and NDM's matched-audio
  transfers. HTTP, HTTPS, SOCKS5, and SOCKS5H URLs; optional username/password;
  remote DNS by default.
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

No spotDL web UI is launched. NDM imports spotDL's metadata/matching API to
select the source, then downloads that source through NDM's own yt-dlp pipeline.
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

Install **FFmpeg** on your PATH for converting spotDL matches, yt-dlp merging,
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
ndm logs                             # read the failure log after an error
ndm logs --path                      # print only the log file location
ndm doctor                           # test Spotify/YouTube reachability + tools
ndm match-check <spotify-url>        # compare NDM vs spotDL matching (no download)
ndm extension install --browser chrome   # register Firefox/Chrome/Chromium/Brave/Edge host
ndm extension doctor --browser chrome    # test the actual native-host launch
ndm config cookie_browser chrome         # select local browser cookies (or none)
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
media extensions. Browser interception is a separate opt-in integration; see
[extension/README.md](extension/README.md) for Firefox and Chrome setup.

### Browser cookies

Open **Options → Capture → Use cookies from**, select the browser where you are
signed in, and click **OK**. Choices are Off, Firefox, Chrome, Chromium, Brave,
Edge, Vivaldi, Opera, and Safari. The setting is `cookie_browser` (default
`none`):

```bash
uv run ndm config cookie_browser chrome
uv run ndm config cookie_browser firefox
uv run ndm config cookie_browser none
```

This works independently of automatic capture and browser handoff: manual
downloads, Media Grabber inspection, and Spotify matching can use an existing
browser login even when inbox/clipboard capture is off. The older
`firefox_cookies=true` setting is still accepted and maps to
`cookie_browser=firefox`; an explicit browser selection takes precedence.

NDM reads a fresh cookie snapshot locally for each new HTTP download, HTML
inspection, yt-dlp operation, spotDL YouTube Music match, or transfer of a
matched audio source. Only cookies matching the destination's domain, path, and
security rules are sent. Cookie values are not saved in NDM settings, queue
entries, or an export file. Select **Off** to stop reading cookies for new
operations.

Chromium-family cookie stores may require a keyring unlock and, on some systems,
the browser to be fully closed. If no readable profile is available, NDM shows
an actionable error without exposing profile paths or cookie values. Cookies do
not bypass DRM or authorize an account. Spotify metadata still uses spotDL's
normal anonymous/official API credentials; browser cookies apply to YouTube
Music matching and the matched-audio transfer, not Spotify protected audio.

### Browser extensions and page grabs

The checkout ships `extension/firefox/` and `extension/chrome/`. They can hand
ordinary downloads to NDM and grab the current YouTube/Spotify page. File
handoff asks in the app by default and cancels the browser copy only after NDM
accepts; a closed/refusing NDM leaves the browser download intact. Page grabs
always ask and reuse the existing media pipelines.

```bash
uv run ndm extension install --browser firefox   # or chrome/chromium/brave/edge/all
uv run ndm config browser_integration true
uv run ndm extension doctor --browser firefox
```

Load the matching extension folder as described in
[extension/README.md](extension/README.md). The extensions use native messaging;
NDM still opens no HTTP listener.

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
the spotDL dialog instead. The browser extensions' page-grab button sends
YouTube/Spotify links into this same approval/queue flow.

## Using spotDL

1. Click **Spotify** on the toolbar.
2. Paste a public `open.spotify.com` track, album, or playlist URL (Ctrl+V also
   opens this dialog automatically when the clipboard contains a Spotify link).
3. Click **Find Tracks**. Titles, artists, albums, and durations appear in the
   table as each Spotify page is read, so you can review or queue tracks while
   the rest of a long playlist is still loading. The heading shows the live
   count and the playlist/album title when available. Previews are capped at
   500 tracks, with a warning when the list is longer.
4. Select rows and click **Queue Selected**.

The daemon processes tracks using its normal concurrency limit. spotDL selects
the YouTube Music match, then NDM's yt-dlp pipeline performs the transfer and
FFmpeg conversion. Rows show matching, byte-level download progress, transfer
speed/ETA while transferring, converting, and tagging. The Size column starts
with a `~` estimate from track duration and the selected lossy bitrate. FLAC,
`auto`, and `disable` have no final-size estimate, so active rows show
`Estimating` or a `~` source-transfer length instead of `Unknown`; the real
saved-file size replaces it at completion. Properties also shows the matched
source URL and transfer size. Stop is cooperative: matching or a
conversion may finish before the track pauses. A paused/failed spotDL task
starts the operation again; yt-dlp resume applies to the matched source, not to
Spotify.

NDM can also do the YouTube Music matching itself (`Options → Source matcher`).
It calls `ytmusicapi` directly and scores candidates on normalized title,
artists, duration, album, and verified status, penalizing versions you did not
ask for (live, cover, sped up, karaoke). An ISRC hit short-circuits the search.
Every candidate and its score is written to the diagnostics log.

**spotDL remains the default matcher** because its scoring has more real-world
tuning. Compare them on your own music before switching:

```bash
ndm match-check https://open.spotify.com/playlist/... --limit 10
```

That prints both choices per track and counts agreements/differences without
downloading anything. Open the differing links, listen, then switch to
`NDM (experimental)` in Options if you prefer its picks.

NDM writes the tags itself (`Options → Tag writer → NDM`). Only fields that
actually have a value are written, so a track with no ISRC, label, or release
date tags cleanly instead of failing after the audio was saved. Cover art is
fetched through your proxy, and a cover that cannot be downloaded is logged and
skipped rather than failing the track. Choose `spotDL (legacy)` to use upstream's
embedder instead. NDM uses standard Vorbis `publisher`/`website` comments (spotDL
used `encodedby`/`woas`) and the `com.apple.iTunes` M4A atoms that other tag
editors read.

Output defaults to MP3 at 192 kbps. `auto`/`disable` leave the bitrate to
yt-dlp/FFmpeg defaults; a higher output bitrate does not improve source quality.
Public metadata access and YouTube Music availability depend on region and
upstream services. Some YouTube sources also need a JavaScript runtime: install
Deno system-wide, or run `spotdl --download-deno` once; NDM reuses that local
Deno for both matching and the matched-source transfer. NDM does not request
Spotify account passwords or launch an OAuth web server. Advanced users can set
`SPOTIPY_CLIENT_ID` and `SPOTIPY_CLIENT_SECRET` for their own official Spotify API
application before launching NDM; default spotDL metadata access needs no login.

### When a download fails

Failures are recorded in a rotating log at `NDM_HOME/logs/ndm.log` (default
`~/.local/share/ndm/logs/ndm.log`, 2 MB × 3 backups). Three ways to read it:

- **Tools → Diagnostics…** in the desktop, or **Diagnostics** in a failed row's
  Properties. It shows the log tail, resolved `ffmpeg`/`ffprobe`/`deno` paths,
  library versions, and a **Test connection** button that probes
  `open.spotify.com`, `music.youtube.com`, and `www.youtube.com` through your
  configured proxy.
- `ndm logs` (works even when the daemon is stopped) and `ndm doctor`.
- Open the log file directly.

The queue row itself leads with an actionable cause — DNS, proxy, TLS
interception, rate limiting, a YouTube sign-in/bot check, a missing JavaScript
runtime, or missing FFmpeg — followed by the upstream message. Full upstream
detail (yt-dlp warnings, the exception chain, and a traceback) goes to the log,
tagged with the task id so a single track can be traced.

Set `NDM_DEBUG=1` for verbose yt-dlp/spotDL output, including each retried
request:

```bash
ndm stop
NDM_DEBUG=1 ndm daemon               # verbose; then retry the track
```

`ndm doctor` probes each host on **both** HTTP stacks NDM uses, because they do
not treat `socks5://` the same way: httpx always sends the hostname to the proxy,
while requests (spotDL's YouTube Music client) resolves it locally first. With
**remote DNS off**, a censored or poisoned local resolver can therefore break
matching while the httpx probes still return HTTP 200. If matching times out
through a SOCKS proxy, enable **Options → Network → remote DNS** so the proxy
resolves hostnames (`socks5h://`).

Proxy passwords, proxy usernames, `Cookie`/`Set-Cookie` values, and
`Authorization` headers are redacted before anything is written or displayed,
so the log is safe to share when reporting a problem.

## Data and security

- Queue/settings/events: SQLite under `NDM_HOME` (default `~/.local/share/ndm`).
- Diagnostics: `NDM_HOME/logs/ndm.log`, rotating, with credentials and cookie
  values redacted before writing.
- Downloads: `~/Downloads/NDM`, split by category, configurable in Options.
- A store lock prevents two daemons from owning the same state directory.
- IPC uses a 32-byte private challenge key in `ipc.key`; the POSIX socket is
  owner-only. Windows uses a per-state-directory named pipe. There is no CORS,
  browser token, public bind address, or listening web port.
- HTTP redirects are revalidated. Direct downloads write `.part` files before
  atomically renaming the completed result.
- Browser cookies are opt-in per selected browser, read locally, kept in memory,
  and matched to the request destination, including spotDL's YouTube Music
  clients. NDM does not persist cookie values or transmit them to an NDM server.
  Upstream temporary database copies are removed after reading.
- spotDL's upstream metadata cache is stored by spotDL in its usual per-user
  config directory. Matched audio transfers use NDM's destination folder and
  yt-dlp partial files, not spotDL's downloader temp directory.

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
  spotify.py      spotDL metadata/matching adapter; NDM owns the transfer
  media.py        direct media/HTML inspection and yt-dlp
  models.py       validated settings, colors, proxy URLs, and URL rules
  network.py      shared proxy configuration and opt-in browser cookies
  browser_host.py native-messaging stdio helper (no HTTP listener)
  extension.py    per-browser host registration and doctor
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

## Web UI (HTMX + Alpine.js)

The optional web frontend mirrors the Nexa desktop layout: download sidebar,
searchable live table, progress bars, selection, resume/stop/delete toolbar,
Add URL, Spotify, Media Grabber, browser handoff approvals, and tabbed options.
It is a separate frontend to the **same native daemon**, not a second engine.
The desktop and web UI can be used together with the same `--data-dir`.

```bash
uv run ndm web                     # http://127.0.0.1:8765
uv run ndm web --port 9000
# Deliberately expose to a trusted network / HTTPS reverse proxy:
uv run ndm web --host 0.0.0.0 --port 8765
```

Open the normal web URL and sign in with a username and password. Configure them
with `NDM_WEB_USERNAME` (default `admin`) and `NDM_WEB_PASSWORD` environment
variables before starting the server. If no password is configured, NDM generates
a random password and prints it in the terminal. Restarting invalidates sessions.
Network deployments should use HTTPS and firewall access appropriately.
The daemon itself still uses authenticated local IPC and does not listen on TCP.
Write requests also require a session-specific CSRF header and same-origin checks.

HTMX 2.0.4 and Alpine.js 3.14.9 are bundled locally: no CDN, JavaScript build step,
or additional Python dependencies. The semantic Nexa surfaces, buttons, inputs,
selects, tabs, dialogs, badges, progress bars, and table selection are implemented
in HTML/CSS rather than trying to run the native renderer in the browser. Use the
moon button for browser-local dark mode. The queue refreshes every two seconds.

Files and cookies belong to the **machine running NDM**, not the visiting browser.
Spotify currently queues all tracks from the supplied public link; the desktop's
track picker and media format inspector are not yet ported. Native clipboard
watching, OS file dialogs/open-file actions, and desktop notifications remain
native-only. The web settings expose connection, proxy, folders, browser capture,
cookies, and Spotify settings; they do not change browser-extension registration.

Frontend source: `src/ndm/web/`. Adapter and security tests:
`python -m pytest tests/test_web.py`.
