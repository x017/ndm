# NDM browser handoff — Firefox and Chrome/Chromium

Two extensions ship in this checkout:

- `extension/firefox/` — Firefox (Manifest V2)
- `extension/chrome/` — Chrome and Chromium (Manifest V3); the same folder can
  be loaded in Brave and Edge

They do two jobs:

1. **Intercept ordinary browser downloads** and ask NDM whether to take them.
2. **Grab the current YouTube video or Spotify track/album/playlist** using
   NDM's existing yt-dlp / spotDL pipelines.

NDM opens **no HTTP server or network port**. The extension talks to a small
native helper over stdin/stdout; the helper uses NDM's authenticated local IPC.
The browser never receives the IPC key.

## Safety rule

For an intercepted file, the extension cancels the browser's transfer **only
after** the native host confirms that NDM accepted it. If NDM is closed,
integration is off, or the target is refused, the browser keeps its download.
`blob:` and `data:` URLs are left to the browser because NDM cannot refetch them.

Page grabs never cancel anything: there was no browser file transfer to lose.
They always create an approval request in NDM, even if automatic file handoff
is enabled. NDM inspects/expands the page only after you approve it.

## Setup

Run these commands from your NDM checkout. No system-wide install is needed.

### Firefox

```sh
uv run ndm extension install --browser firefox
```

This writes `~/.mozilla/native-messaging-hosts/ndm.json` (the platform's
per-user location on macOS/Windows) and a launcher in your NDM data directory.
The launcher pins the interpreter used by `uv run ndm`, including its venv.

Then:

1. Open `about:debugging#/runtime/this-firefox`.
2. Click **Load Temporary Add-on…**.
3. Choose `extension/firefox/manifest.json`.

Temporary add-ons are removed when Firefox closes; load it again next session.
A permanent Firefox install requires Mozilla signing.

### Chrome

```sh
uv run ndm extension install --browser chrome
```

Then:

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Click **Load unpacked**.
4. Choose the `extension/chrome/` folder.

The manifest contains a fixed public key, so the unpacked extension always has
ID `cmmjmjhilkeomgcclkdknpkiekeefbbj`. The registered native host allows exactly
that origin. Keep this folder in place after loading it.

### Chromium, Brave, Edge

Use the matching registration command, then load **`extension/chrome/`** as an
unpacked extension in that browser:

```sh
uv run ndm extension install --browser chromium
# or:
uv run ndm extension install --browser brave
uv run ndm extension install --browser edge
```

Open `chrome://extensions` (Chromium), `brave://extensions`, or
`edge://extensions`. The installer writes a separate per-vendor native-host
manifest; uninstalling one browser does not remove a launcher still used by
another.

To register every supported browser at once:

```sh
uv run ndm extension install --browser all
```

### Enable and check

```sh
uv run ndm config browser_integration true
uv run ndm                 # keep the desktop available to answer requests
uv run ndm extension doctor --browser firefox
uv run ndm extension doctor --browser chrome
```

Use the browser you registered. `--browser all` checks every vendor. Every line
should read `[OK]`; the popup should say NDM is running.

You can also enable it in **Options → Capture → Hand browser downloads to NDM**.
Leave **Ask before automatic browser file downloads** checked unless you want
intercepted files to start immediately. Page grabs still ask.

If you updated NDM while its daemon was already running, close the NDM window,
run `uv run ndm stop`, then start `uv run ndm` again so the daemon loads the new
settings schema and grab handler.

## Using it

### Ordinary files

Download a file normally. NDM shows a request with the filename, host, size,
destination, and URL. Choose **Download**, optionally **Add paused**, or
**Leave in browser**.

The popup controls automatic interception: enabled, minimum size, and file
types. Right-click a link/image/video/audio and choose **Download with NDM** to
offer it regardless of those filters.

### YouTube / Spotify pages

On a supported page, either:

- click the extension's **Grab this YouTube / Spotify page** button, or
- right-click the page and choose **Grab this page with NDM (YouTube / Spotify)**.

NDM shows a page-specific approval dialog. YouTube uses the Media Grabber's
yt-dlp transfer; Spotify uses spotDL metadata expansion and NDM's matched-audio
pipeline. Spotify albums/playlists may queue multiple tracks. Spotify protected
audio/DRM is never extracted.

Supported links include YouTube watch/shorts/embed/live and `youtu.be`, plus
Spotify track/album/playlist URLs (including `intl-xx` paths). Other pages are
not sent as page grabs.

If you dismissed a dialog, open **Downloads → Browser requests (N)**.

## Cookies are a separate setting

The extension does **not** export or transmit browser cookies. For authenticated
media, open **Options → Capture → Use cookies from** and select Firefox, Chrome,
Chromium, Brave, Edge, Vivaldi, Opera, or Safari. Off is the default.

```sh
uv run ndm config cookie_browser chrome
# or firefox / chromium / none
```

NDM reads a fresh local snapshot per operation through yt-dlp. Values are not
saved in NDM settings or the queue. Chromium-family browsers may require a
keyring unlock and, on some systems, a fully closed browser. See the root README
for the cookie boundary and troubleshooting.

## Limits

- Offers expire after 5 minutes; at most 50 can wait for approval.
- Private/local-network targets follow the daemon's private-address policy.
- The popup's enabled switch controls automatic file interception. NDM's
  `browser_integration` switch controls all browser handoffs.
- Chrome's MV3 worker can restart between events; settings are read from storage
  each time. Firefox's temporary add-on must be reloaded after a browser restart.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Popup says “not reachable” | `uv run ndm extension doctor --browser <name>` prints the actual launcher/import/daemon error. |
| Only “browser integration enabled” fails | `uv run ndm config browser_integration true`; no reinstall is needed. |
| Nothing is intercepted | Check the popup's enabled/type/size filters and NDM's integration switch. |
| Chrome says native host not found/forbidden | Re-run install for the correct vendor, reload NDM on the extensions page, and verify the ID above. |
| Firefox lost the add-on | Temporary add-ons do not survive restart; load `extension/firefox/manifest.json` again. |
| Host path warning | `uv run ndm extension status --browser <name>`; re-run install from the same checkout/venv if it differs. |
| Page grab fails after approval | The request stays available for retry. Check NDM's Diagnostics/log; YouTube/Spotify network or cookie errors are not extension-install errors. |

After reinstalling the host, reload the extension: remove/load the temporary
Firefox add-on, or press Reload on NDM in the Chromium-family extensions page.

Uninstall registrations with:

```sh
uv run ndm extension uninstall --browser firefox
uv run ndm extension uninstall --browser chrome
# or --browser all
```

Then remove the loaded extension from the browser. Uninstalling the native host
does not delete downloads or preferences.

## Development checks

```sh
node --test tests/extension_runtime.test.cjs
uv run pytest tests/test_browser_extension.py tests/test_desktop.py
```

The Node test executes both shipped backgrounds against browser-event doubles,
including host refusal, cancellation ordering, popup grabs, and Chrome worker
restart. Python tests cover the same URL matrix, native framing/IPC, installer
registration, cookie routing, and rendered approval dialogs.
