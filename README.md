# NDM

**A persistent download manager with a native desktop app and a lightweight web interface.**

NDM combines resumable HTTP downloads, yt-dlp media extraction, and spotDL music
matching in a shared background engine. Use the Nexa UI desktop app locally or
manage the same queue from the HTMX and Alpine.js web frontend. Downloads continue
when you close either interface.

[Quick start](#quick-start) · [Web UI](#web-ui) · [Browser integration](#browser-integration) · [Configuration](#configuration) · [Troubleshooting](#troubleshooting) · [Development](#development)

## Features

- **Resumable downloads.** Persistent queue, up to eight HTTP range connections
  per file, configurable concurrency, shared speed limits, and single-connection
  fallback when a server does not support ranges.
- **Native desktop.** An IDM-style layout with menus, a toolbar, category tree,
  sortable download table, progress details, and light/dark themes with semantic
  color customization.
- **Web frontend.** Live queue updates, search and category filters, download
  controls, dialogs, settings, browser handoff approvals, and password login.
  HTMX and Alpine.js are bundled locally; no CDN or JavaScript build step is needed.
- **Media downloads.** yt-dlp integration, desktop media inspection and format
  selection, and FFmpeg-based conversion and merging.
- **Spotify links.** Expand public tracks, albums, and playlists with spotDL;
  match an available audio source, download it through NDM, and write metadata.
- **Browser integration.** Firefox and Chromium-family extensions for file
  handoff and YouTube/Spotify page grabs through native messaging.
- **Optional browser cookies and proxies.** Select a local browser cookie store
  and configure HTTP, HTTPS, SOCKS5, or SOCKS5H proxy transport.
- **Background operation.** Authenticated local daemon IPC, SQLite persistence,
  opt-in link-inbox capture, and sign-in service installation on Linux and macOS.

## Requirements

| Component | Requirement |
| --- | --- |
| Python | 3.11–3.15 |
| Package manager | [uv](https://docs.astral.sh/uv/) recommended; pip is also supported |
| Desktop | A graphical session supported by Nexa UI; Linux requires working display/OpenGL libraries |
| Media processing | FFmpeg on `PATH` for conversion and merging |
| Some YouTube sources | A supported JavaScript runtime, such as Deno |

The web frontend does not need a desktop window. Native platform integrations
and browser-cookie access depend on the operating system and local environment.
Windows and macOS integrations should be verified on the target platform.

## Quick start

```bash
git clone https://github.com/x017/ndm.git
cd ndm
uv sync

# Open the native desktop; start the daemon if needed.
uv run ndm
```

To use the web interface instead:

```bash
uv run ndm web
```

Open **http://127.0.0.1:8765**. The default username is `admin`; if no password
is configured, NDM generates one and prints it in the terminal.

Alternatively, install into a Python environment with pip:

```bash
python -m pip install -e .
ndm desktop
```

> Use the branch containing the features you need. If your checkout predates the
> web frontend, update that branch before running `ndm web`.

## Web UI

### Login and network access

Set credentials before starting the web server. For example, in a POSIX shell:

```bash
export NDM_WEB_USERNAME=admin
read -rs -p "NDM web password: " NDM_WEB_PASSWORD; echo
export NDM_WEB_PASSWORD
uv run ndm web
```

On PowerShell:

```powershell
$env:NDM_WEB_USERNAME = "admin"
# Omit NDM_WEB_PASSWORD to generate a random password at startup.
uv run ndm web
```

| Variable / option | Default | Purpose |
| --- | --- | --- |
| `NDM_WEB_USERNAME` | `admin` | Login username |
| `NDM_WEB_PASSWORD` | Generated at startup | Login password |
| `--host` | `127.0.0.1` | Listening address |
| `--port` | `8765` | HTTP port |
| `--data-dir` | `NDM_HOME` or the default state directory | Queue and daemon to manage |

For access from another device:

```bash
uv run ndm web --host 0.0.0.0 --port 8765
```

**Network access is opt-in.** Use a trusted network, restrict access with a
firewall, and place remote deployments behind an HTTPS reverse proxy. The web
server does not provide TLS itself. Treat access to it as access to a download
manager running under your operating-system account.

Login uses an HttpOnly, SameSite session cookie. Login forms and application
writes require CSRF tokens; these checks support reverse proxies that rewrite
the backend host header. Restarting the web server invalidates existing sessions.
When no password is configured, it also generates a new password.

### Desktop and web capabilities

Both interfaces control the same queue when using the same data directory.

| Capability | Desktop | Web |
| --- | --- | --- |
| Queue, progress, search, and categories | Yes | Yes |
| Add URL, resume, pause, and remove | Yes | Yes |
| Media downloads | Inspection and format selection | URL and quality selection |
| Spotify links | Preview and select tracks | Queue all returned tracks |
| Browser handoff approvals | Yes | Yes |
| Connection, proxy, folder, cookie, and Spotify settings | Yes | Yes |
| Appearance | Full semantic palette | Browser-local dark mode |
| Clipboard capture, OS file dialogs, and open-file actions | Native integrations | Not ported |

The web queue refreshes every two seconds. Download paths and cookie stores
belong to the **machine running NDM**, not the device viewing the web page.

## Command-line usage

All examples use `uv run`; after a pip installation, use `ndm` directly.

```bash
uv run ndm desktop
uv run ndm web
uv run ndm daemon                  # Headless engine; authenticated local IPC only
uv run ndm add https://example.org/file.zip --filename file.zip
uv run ndm add https://example.org/file.zip --paused
uv run ndm status                  # Queue snapshot as JSON
uv run ndm pause <task-id>
uv run ndm resume <task-id>
uv run ndm remove <task-id>         # Keeps completed files
uv run ndm spotify <spotify-url>
uv run ndm config --list
uv run ndm logs --lines 80
uv run ndm doctor
uv run ndm stop                    # Stops the daemon
```

Run `uv run ndm --help` or `uv run ndm <command> --help` for available options.
All commands accept `--data-dir PATH`; `NDM_HOME` sets the default state directory.
Separate data directories allow independent queues and daemons.

Closing the desktop, closing a browser tab, or stopping the web frontend does
not stop the daemon. Use `uv run ndm stop` to stop the background engine.

### Start the daemon at sign-in

```bash
uv run ndm service install --start   # Linux systemd user service / macOS LaunchAgent
uv run ndm service uninstall
```

Linux unattended operation may also require `loginctl enable-linger "$USER"`.
On Windows, use a per-user Task Scheduler task to run `ndm daemon` with the
appropriate data directory. These services start the daemon, not the web server.

## Browser integration

The repository includes a Firefox extension and a Chromium Manifest V3 extension
for Chrome, Chromium, Brave, and Edge.

```bash
uv run ndm extension install --browser chrome
uv run ndm config browser_integration true
uv run ndm extension doctor --browser chrome
```

Replace `chrome` with `firefox`, `chromium`, `brave`, or `edge`; use `all` to
register all supported native hosts.

Load `extension/chrome/` as an unpacked extension in your Chromium browser, or
load `extension/firefox/manifest.json` through Firefox's temporary add-on loader.
Registration alone does not load the extension. See the
[browser extension guide](extension/README.md) for platform-specific instructions.

- **File handoff:** asks for approval by default. The extension cancels the
  browser download only after NDM accepts it; unsupported `blob:` and `data:`
  downloads remain in the browser.
- **Page grabs:** send supported YouTube video or Spotify track, album, and
  playlist pages into NDM's existing media pipelines. Page grabs always ask.
- **Transport:** extensions use native messaging, not the web frontend. The
  optional web server is not required for browser integration.

## Configuration

### Folders and download behavior

Use **Options** in the desktop or web interface to configure destination folders,
concurrency, connections per file, and speed limits. Direct downloads default to
`~/Downloads/NDM`, organized by category. Music has a separate configurable folder.

Segmented downloads and persistent resume depend on server support. NDM validates
ETag/Last-Modified information and restarts changed remote files rather than
combining incompatible bytes. Downloads use partial files before completion.

### Proxies

Configure **Options → Proxy**, or set the URL before enabling the proxy:

```bash
uv run ndm config proxy_url socks5://127.0.0.1:1080
uv run ndm config proxy_enabled true
```

HTTP, HTTPS, SOCKS5, and SOCKS5H are supported, with optional credentials. Remote
DNS is enabled by default. An enabled NDM proxy overrides ambient proxy settings.
Proxy credentials are stored in local settings; protect the state directory.

### Browser cookies

Select **Options → Capture → Use cookies from**, or use the CLI:

```bash
uv run ndm config cookie_browser chrome
uv run ndm config cookie_browser firefox
uv run ndm config cookie_browser none
```

Supported choices are `none`, `firefox`, `chrome`, `chromium`, `brave`, `edge`,
`vivaldi`, `opera`, and `safari`. Availability depends on the platform, browser
profile, and keyring access. Chromium-family browsers may need to be closed
before their cookie store can be read.

Cookies are opt-in and independent of automatic capture. They can be used for
downloads, media inspection, and YouTube Music matching. Cookie values are not
persisted in NDM settings or exported to a cookie file; destination matching
controls which cookies are sent. The legacy `firefox_cookies` setting remains
supported, but an explicit `cookie_browser` selection takes precedence.

Cookies do not bypass DRM, and Spotify browser cookies are not used to extract
Spotify's protected audio.

### Automatic capture

Desktop **Options → Capture** provides opt-in clipboard capture while the window
is open and link-inbox watching in the background. An optional host allowlist
restricts capture. The inbox accepts text files containing HTTP(S) media URLs
and Internet Shortcut files:

```ini
[InternetShortcut]
URL=https://example.org/media/video.mp4
```

Processed inbox files receive a `.processed` suffix. Use the Spotify dialog or
CLI for Spotify links rather than the inbox.

## Media and Spotify

In the desktop **Grabber**, inspect a supported page or direct-media URL, review
available formats, and queue a download. FFmpeg is required for audio conversion
and merging; the desktop also offers MP3 extraction at 192 kbps. File sizes may
be estimates until the transfer completes.

The desktop **Spotify** dialog previews tracks and supports queueing a selection.
The web Spotify form queues all returned tracks. Public metadata access and
audio-source availability depend on region and upstream services.

**NDM does not download Spotify's protected streams or bypass DRM.** Spotify
provides metadata; spotDL matches a corresponding public source, usually YouTube
Music. The recording may differ from the original or be unavailable. This is
not Spotify Premium offline caching, and output conversion cannot improve the
source's audio quality. Save only content you own or have permission to download,
subject to applicable law and service terms.

spotDL is the default source matcher. An experimental NDM matcher can be compared
without downloading audio:

```bash
uv run ndm match-check <spotify-url> --limit 10
```

If a source requires JavaScript execution, install Deno on `PATH` or use
`spotdl --download-deno`. NDM reuses that local runtime. NDM does not launch
spotDL's web interface or request a Spotify account password. Upstream Spotify
API credentials, when needed, can be supplied through `SPOTIPY_CLIENT_ID` and
`SPOTIPY_CLIENT_SECRET`.

## Security and local data

- State defaults to `~/.local/share/ndm`; override it with `NDM_HOME` or `--data-dir`.
- The daemon uses authenticated Unix domain sockets or Windows named pipes. Only
  the explicit `web` command opens an HTTP listener.
- Private/local download targets are allowed by default. To reject them, start
  the daemon with `uv run ndm daemon --block-private` before connecting a frontend.
- URLs with embedded credentials are rejected. Add only URLs you trust.
- Protect the state directory, IPC key, web credentials, proxy credentials, and
  downloaded files. Redaction in diagnostic output is not a substitute for
  operating-system access controls.
- Downloaded content is not guaranteed safe. NDM is not a malware scanner.

## Troubleshooting

| Problem | Recommended action |
| --- | --- |
| `invalid choice: 'web'` | Update the branch containing the web frontend, then run `uv sync`. |
| Login fails | Check the current startup password or configured environment variables. Refresh the login page after restarting the server. |
| Stale login form or CSRF error | Reload the page to obtain the current server token. Update older versions that reject proxy origins. |
| Conversion or merging fails | Install FFmpeg and verify it is on the daemon's `PATH`. |
| YouTube sign-in or bot check | Select the local browser where you are signed in; inspect the diagnostic log. Cookies are not a guaranteed workaround. |
| Cookie store cannot be read | Check profile/keyring access and try closing the browser. |
| Extension cannot reach NDM | Run `uv run ndm extension doctor --browser <browser>` and check that the extension is loaded. |
| Download or Spotify matching fails | Run `uv run ndm logs` and `uv run ndm doctor`; check proxy, DNS, runtime availability, and upstream restrictions. |
| Settings differ between interfaces | Ensure both use the same `NDM_HOME` or `--data-dir`. |

Useful diagnostics:

```bash
uv run ndm logs --path
uv run ndm logs --lines 100
uv run ndm logs --match --lines 40
uv run ndm doctor
```

Logs are stored at `<data-dir>/logs/ndm.log`. Desktop users can also open
**Tools → Diagnostics**. Before sharing logs or filing an issue, review them for
personal URLs, file paths, and other sensitive information.

## Development

```bash
uv sync --extra dev
uv run pytest tests/test_web.py
uv run pytest tests
uv run ruff check src tests
node --test tests/extension_runtime.test.cjs
uv lock --check
uv build --wheel
```

Node.js is needed for the extension runtime tests, not for running NDM or its
web frontend. Some media tests depend on external tools or upstream behavior;
report environment details alongside failures. A passing local suite does not
guarantee availability of third-party providers.

```text
src/ndm/
├── cli.py              Command-line entry points
├── desktop.py          Native Nexa UI frontend
├── web/                HTMX/Alpine frontend and authenticated HTTP adapter
├── daemon.py           Shared background service and command validation
├── ipc.py              Authenticated local transport
├── engine.py           Queue, segmented transfers, resume, and workers
├── media.py            Media inspection and yt-dlp integration
├── spotify.py          spotDL metadata and source matching
├── network.py          Proxy and browser-cookie configuration
├── models.py           Validated settings and request models
├── store.py            SQLite persistence
├── browser_host.py     Native-messaging host
└── extension.py        Per-browser host registration and diagnostics
extension/              Firefox and Chromium extensions
tests/                  Python and JavaScript tests
```

When contributing, keep desktop and web operations on the shared daemon, validate
new request payloads, add regression tests, and document platform limitations.
Report bugs and propose changes through the repository's
[issues](https://github.com/x017/ndm/issues) and pull requests.

## Limitations

NDM does not currently provide torrents, scheduled downloads, a system tray, an
audio streaming service, or complete desktop/web feature parity. Media extraction
and Spotify matching rely on third-party services and may break as those services
change. Browser extensions loaded in development mode may require reloading after
browser restarts or updates.

## Licensing

NDM is described as MIT-licensed in the project's existing documentation; this
checkout does not yet include a standalone `LICENSE` file. Third-party components,
including Nexa UI, yt-dlp, spotDL, HTMX, and Alpine.js, retain their respective
licenses. Bundled web-library license notices are in `src/ndm/web/static/`.
