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

3. Turn the handoff on — either from the shell:

   ```sh
   uv run ndm config browser_integration true
   ```

   or in NDM: **Options → Capture → Hand Firefox downloads to NDM**.
   Leave **Ask me in the app before each browser download** checked
   (`browser_ask`, also settable via `ndm config`).

4. Start NDM (`uv run ndm`) and confirm:

   ```sh
   uv run ndm extension doctor
   ```

   Every line should read `[OK]`. The extension popup should say NDM is running.

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
| Popup says "not reachable" | Run `uv run ndm extension doctor` — it launches the host exactly as Firefox does and prints the real error. Then re-install and **reload the add-on**. |
| "integration is off" | `uv run ndm config browser_integration true` |
| Nothing is offered | Popup filters: enabled, size threshold, file type. |
| Host path warning | `uv run ndm extension status` — re-run install if it differs. |

### Diagnosing

```sh
uv run ndm extension doctor
```

This checks the manifest, the launcher's permissions, the running daemon, then
spawns the launcher with Firefox's arguments and reports the ping reply and any
stderr. An import error there means the launcher points at the wrong
interpreter — re-run `install` from the same environment you use for `uv run ndm`.

After re-installing, **remove and reload the temporary add-on** in
`about:debugging`; Firefox keeps the old connection otherwise.

Uninstall the host with `uv run ndm extension uninstall`.
