/* Firefox wiring. The offer flow and filters live in core.js. */
let settings = { ...NDM_DEFAULTS };
browser.storage.local.get(NDM_DEFAULTS).then((stored) => { settings = { ...NDM_DEFAULTS, ...stored }; });
browser.storage.onChanged.addListener((changes) => {
  for (const [key, change] of Object.entries(changes)) settings[key] = change.newValue;
});

const handedOff = new Set();

const api = {
  send: (message) => browser.runtime.sendNativeMessage(NDM_HOST, message),
  cancel: (id) => browser.downloads.cancel(id),
  erase: (query) => browser.downloads.erase(query),
  notify: (title, message) => {
    browser.notifications.create({
      type: "basic", iconUrl: browser.runtime.getURL("icon48.png"), title, message,
    }).catch(() => {});
  },
};

browser.downloads.onCreated.addListener(async (item) => {
  if (handedOff.has(item.id) || !ndmWanted(item, settings)) return;
  if (await ndmOfferDownload(item, api)) handedOff.add(item.id);
});

browser.menus.create({
  id: "ndm-link", title: "Download with NDM", contexts: ["link", "image", "video", "audio"],
});
browser.menus.create({
  id: "ndm-page", title: "Grab this page with NDM (YouTube / Spotify)", contexts: ["page"],
});

browser.menus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === "ndm-page") {
    await ndmGrabPage((tab && tab.url) || info.pageUrl || "", api);
    return;
  }
  const url = info.linkUrl || info.srcUrl;
  if (!url) return;
  if (ndmPageKind(url)) {
    await ndmGrabPage(url, api);
    return;
  }
  // An explicit click bypasses the type/size filters but still asks in the app.
  await ndmOfferDownload({ id: `menu-${Date.now()}`, url, filename: ndmBaseName(url),
                           mime: "", totalBytes: 0 }, api);
});

browser.runtime.onMessage.addListener((message) => {
  if (!message) return undefined;
  if (message.type === "status") {
    return api.send({ type: "ping" })
      .then((reply) => reply || { ok: false, running: false })
      .catch((error) => ({ ok: false, running: false, error: String(error) }));
  }
  if (message.type === "grab") {
    return browser.tabs.query({ active: true, currentWindow: true })
      .then((tabs) => ndmGrabPage((tabs[0] && tabs[0].url) || "", api))
      .then((result) => result)
      .catch((error) => ({ ok: false, error: String(error) }));
  }
  return undefined;
});
