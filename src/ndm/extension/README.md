# NDM Capture

Opt-in Chromium MV3 companion for NDM. It observes response headers for direct audio/video URLs and sends only the media URL, filename, MIME type, and your authorization flag to the endpoint you configure. It does not read cookies, page contents, or browsing history, and it does not bypass DRM.

1. Open `chrome://extensions`, enable Developer mode, and choose **Load unpacked** → this folder.
2. Open the extension's options.
3. In NDM, open **Media grabber**, enable automatic capture, and expand **Set up the browser companion**.
4. Copy the endpoint and private token into the extension options.
5. Enable capture only while you want it. Configure an allowed-domain list in NDM for tighter control.

The token is stored in the extension's local storage and is scoped to `/api/capture`. Use HTTPS for a remote daemon; the default desktop endpoint is HTTP on loopback only. Firefox needs an MV3-compatible build with persistent event-page/background adaptations and is not yet validated.
