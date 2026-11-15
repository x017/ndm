# NDM Firefox handoff

Firefox hands downloads to NDM. **NDM asks you in the app before anything
starts** — the extension never silently downloads.

## How it works

```
Firefox download starts
   └─> extension sends the URL over native messaging
         └─> ndm-browser-host forwards it to the daemon over local IPC
               └─> NDM shows "Firefox wants to download a file"
                     ├─ Download        → queued in NDM
                     └─ Leave in Firefox → nothing happens in NDM
```

The extension cancels Firefox's own transfer **only after** the daemon accepts
the offer. If NDM is closed, integration is off, or the link is refused, Firefox
keeps the download and no file is lost.

NDM still opens no network port. Firefox talks to a helper process over
stdin/stdout; that helper uses the same authenticated local IPC as the CLI, so
the browser never sees the IPC key.

## Setup

1. Register the native host (from your checkout):

   ```sh
   uv run ndm extension install
   ```

   This writes `~/.mozilla/native-messaging-hosts/ndm.json` and a launcher in
   your NDM data directory. The launcher pins the interpreter that ran the
   command, so no system-wide install is needed.

2. Load the extension:

   - Open `about:debugging#/runtime/this-firefox`
   - Click **Load Temporary Add-on…**
   - Choose `extension/firefox/manifest.json`

   Temporary add-ons are removed when Firefox closes; load it again next
   session. For a permanent install the extension must be signed by Mozilla.

3. In NDM: **Options → Capture → Hand Firefox downloads to NDM**.
   Leave **Ask me in the app before each browser download** checked.

4. Start NDM (`uv run ndm`). The extension popup should say it is running.

## Controls

**Extension popup** — turn the handoff on/off, skip files below a size, and
choose which kinds (video, audio, archive, document, image, other) are offered.

**Right-click a link → Download with NDM** — offers that link regardless of the
type/size filters. It still asks in the app.

**NDM → Downloads → Browser requests (N)** — review waiting offers if you
dismissed the dialog.

## Notes and limits

- `blob:` and `data:` URLs cannot be refetched by the engine, so they are left
  to Firefox.
- Offers expire after 5 minutes and are capped at 50 waiting entries.
- Private/local-network targets are still blocked unless the daemon was started
  with private addresses allowed.
- Turning off **Ask me in the app** makes downloads start immediately.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Popup says "not reachable" | Is NDM running? Re-run `uv run ndm extension install`. |
| "integration is off" | Options → Capture → Hand Firefox downloads to NDM. |
| Nothing is offered | Popup filters: enabled, size threshold, file type. |
| Host path warning | `uv run ndm extension status` — re-run install if it differs. |

Uninstall the host with `uv run ndm extension uninstall`.
