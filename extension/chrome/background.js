/*
 * Chrome/Chromium MV3 wiring. The offer flow and filters live in core.js.
 *
 * A service worker is torn down between events, so settings are read from
 * storage on every event rather than cached in a module variable.
 */
importScripts("core.js");

const api = {
  send: (message) => chrome.runtime.sendNativeMessage(NDM_HOST, message),
  cancel: (id) => chrome.downloads.cancel(id),
  erase: (query) => chrome.downloads.erase(query),
  notify: (title, message) => {
    chrome.notifications.create({
      type: "basic", iconUrl: chrome.runtime.getURL("icon48.png"), title, message,
    }).catch(() => {});
  },
};

async function currentSettings() {
  const stored = await chrome.storage.local.get(NDM_DEFAULTS);
  return { ...NDM_DEFAULTS, ...stored };
}

chrome.downloads.onCreated.addListener(async (item) => {
  const settings = await currentSettings();
  if (!ndmWanted(item, settings)) return;
  await ndmOfferDownload(item, api);
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "ndm-link", title: "Download with NDM", contexts: ["link", "image", "video", "audio"],
    });
    chrome.contextMenus.create({
      id: "ndm-page", title: "Grab this page with NDM (YouTube / Spotify)", contexts: ["page"],
    });
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
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
  await ndmOfferDownload({ id: `menu-${Date.now()}`, url, filename: ndmBaseName(url),
                           mime: "", totalBytes: 0 }, api);
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message) return false;
  if (message.type === "status") {
    api.send({ type: "ping" })
      .then((reply) => sendResponse(reply || { ok: false, running: false }))
      .catch((error) => sendResponse({ ok: false, running: false, error: String(error) }));
    return true;  // keep the channel open for the async reply
  }
  if (message.type === "grab") {
    chrome.tabs.query({ active: true, currentWindow: true })
      .then((tabs) => ndmGrabPage((tabs[0] && tabs[0].url) || "", api))
      .then((result) => sendResponse(result))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  return false;
});
