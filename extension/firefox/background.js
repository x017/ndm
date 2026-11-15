/*
 * Hand Firefox downloads to NDM.
 *
 * Ordering matters: the browser download is only cancelled after the native host
 * confirms NDM accepted the offer. If NDM is closed, integration is off, or the
 * user's allowlist rejects the link, Firefox keeps the download and nothing is
 * lost.
 */
const HOST = "ndm";
const DEFAULTS = { enabled: true, minSizeKB: 0, types: "video,audio,archive,document,image,other" };

let settings = { ...DEFAULTS };
browser.storage.local.get(DEFAULTS).then((stored) => { settings = { ...DEFAULTS, ...stored }; });
browser.storage.onChanged.addListener((changes) => {
  for (const [key, change] of Object.entries(changes)) settings[key] = change.newValue;
});

// Downloads we cancelled ourselves must not be re-offered when erase fires.
const handedOff = new Set();
// Firefox reports the final URL only after redirect resolution; keep the recent
// ones so a re-created entry for the same file is not offered twice.
const recent = new Map();

function extensionOf(name) {
  const match = /\.([a-z0-9]{1,8})$/i.exec(name || "");
  return match ? match[1].toLowerCase() : "";
}

const TYPES = {
  video: ["mp4", "mkv", "webm", "mov", "avi", "m4v", "flv", "ts"],
  audio: ["mp3", "m4a", "wav", "flac", "ogg", "opus", "aac", "m4b"],
  archive: ["zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "deb", "rpm"],
  document: ["pdf", "epub", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt"],
  image: ["jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "svg"],
};

function categoryOf(item) {
  const ext = extensionOf(item.filename || item.url);
  for (const [kind, list] of Object.entries(TYPES)) if (list.includes(ext)) return kind;
  if ((item.mime || "").startsWith("video/")) return "video";
  if ((item.mime || "").startsWith("audio/")) return "audio";
  return "other";
}

function wanted(item) {
  if (!settings.enabled) return false;
  // Only http(s) can be refetched by the engine; blob: and data: cannot.
  if (!/^https?:/i.test(item.url || "")) return false;
  if (item.byExtensionID) return false;
  const enabled = String(settings.types || "").split(",").map((s) => s.trim()).filter(Boolean);
  if (!enabled.includes(categoryOf(item))) return false;
  const size = item.totalBytes > 0 ? item.totalBytes : item.fileSize || 0;
  const min = Number(settings.minSizeKB) || 0;
  // Unknown size (-1 / 0) is offered rather than silently skipped.
  if (min > 0 && size > 0 && size < min * 1024) return false;
  const seen = recent.get(item.url);
  if (seen && Date.now() - seen < 5000) return false;
  return true;
}

function baseName(path) {
  return String(path || "").split(/[\\/]/).pop() || "";
}

async function offer(item) {
  const message = {
    type: "offer",
    id: `dl-${item.id}`,
    url: item.url,
    filename: baseName(item.filename) || undefined,
    mime: item.mime || "",
    size: item.totalBytes > 0 ? item.totalBytes : 0,
    page_url: item.referrer || "",
  };
  let reply;
  try {
    reply = await browser.runtime.sendNativeMessage(HOST, message);
  } catch (error) {
    notify("NDM is not reachable", "Start NDM and run: uv run ndm extension install");
    return false;
  }
  if (!reply || !reply.ok) {
    // Leave the browser download alone; NDM refused or is not running.
    if (reply && reply.error) notify("NDM left this download in Firefox", reply.error);
    return false;
  }
  try {
    await browser.downloads.cancel(item.id);
    await browser.downloads.erase({ id: item.id });
  } catch (error) {
    // Already finished or gone; nothing to hand over.
  }
  handedOff.add(item.id);
  recent.set(item.url, Date.now());
  if (recent.size > 200) recent.delete(recent.keys().next().value);
  notify(reply.status === "started" ? "Sent to NDM" : "Waiting in NDM",
         reply.status === "started"
           ? `${message.filename || "Download"} is downloading in NDM.`
           : `Approve ${message.filename || "the download"} in the NDM window.`);
  return true;
}

function notify(title, message) {
  browser.notifications.create({
    type: "basic", iconUrl: browser.runtime.getURL("icon.svg"), title, message,
  }).catch(() => {});
}

browser.downloads.onCreated.addListener((item) => {
  if (handedOff.has(item.id) || !wanted(item)) return;
  offer(item);
});

browser.menus.create({
  id: "ndm-link", title: "Download with NDM", contexts: ["link", "image", "video", "audio"],
});

browser.menus.onClicked.addListener(async (info) => {
  const url = info.linkUrl || info.srcUrl;
  if (!url) return;
  // An explicit click bypasses the type/size filters but still asks in the app.
  await offer({ id: `menu-${Date.now()}`, url, filename: baseName(url), mime: "", totalBytes: 0 });
});

browser.runtime.onMessage.addListener((message) => {
  if (message && message.type === "status") {
    return browser.runtime.sendNativeMessage(HOST, { type: "ping" })
      .then((reply) => reply || { ok: false, running: false })
      .catch((error) => ({ ok: false, running: false, error: String(error) }));
  }
  return undefined;
});
