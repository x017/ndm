/*
 * Shared NDM handoff logic for Firefox and Chrome.
 *
 * This file is identical in extension/firefox/ and extension/chrome/; a test
 * asserts they match. Each browser's background script supplies a promise-based
 * `api` so the offer flow, filters and safety ordering live in one place.
 *
 * Safety ordering: the browser's own transfer is cancelled only after the
 * native host confirms NDM accepted. A closed NDM, a disabled integration or a
 * refused target leaves the download in the browser.
 */
const NDM_HOST = "ndm";
const NDM_DEFAULTS = {
  enabled: true,
  minSizeKB: 0,
  types: "video,audio,archive,document,image,other",
};

const NDM_TYPES = {
  video: ["mp4", "mkv", "webm", "mov", "avi", "m4v", "flv", "ts"],
  audio: ["mp3", "m4a", "wav", "flac", "ogg", "opus", "aac", "m4b"],
  archive: ["zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "deb", "rpm"],
  document: ["pdf", "epub", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt"],
  image: ["jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "svg"],
};

// Best-effort only: a Chrome service worker can be torn down between events.
const ndmRecent = new Map();

function ndmBaseName(path) {
  return String(path || "").split(/[\\/]/).pop() || "";
}

function ndmExtensionOf(name) {
  const match = /\.([a-z0-9]{1,8})$/i.exec(name || "");
  return match ? match[1].toLowerCase() : "";
}

function ndmCategoryOf(item) {
  const ext = ndmExtensionOf(item.filename || item.url);
  for (const [kind, list] of Object.entries(NDM_TYPES)) {
    if (list.includes(ext)) return kind;
  }
  if ((item.mime || "").startsWith("video/")) return "video";
  if ((item.mime || "").startsWith("audio/")) return "audio";
  return "other";
}

/* A YouTube watch/short link or a Spotify track/album/playlist is a page the
 * engine can extract, not a direct file, so it goes to browser.grab instead.
 * Keep this classifier in step with media.grab_page_kind(). */
function ndmPageKind(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch (error) {
    return "";
  }
  const host = parsed.hostname.replace(/^www\./, "").toLowerCase();
  if (host === "youtube.com" || host === "m.youtube.com" || host === "youtu.be") {
    if (host === "youtu.be") return /^\/[A-Za-z0-9_-]{11}$/.test(parsed.pathname) ? "youtube" : "";
    if (parsed.pathname === "/watch" && parsed.searchParams.get("v")) return "youtube";
    if (/^\/(?:shorts|embed|live)\/[A-Za-z0-9_-]{11}$/.test(parsed.pathname)) return "youtube";
    return "";
  }
  if (host === "open.spotify.com" || host === "play.spotify.com") {
    return /^\/(?:intl-[a-z]{2}\/)?(?:track|album|playlist)\/[A-Za-z0-9]{22}\/?$/.test(parsed.pathname)
      ? "spotify" : "";
  }
  return "";
}

function ndmWanted(item, settings) {
  if (!settings.enabled) return false;
  // blob: and data: cannot be refetched by the engine.
  if (!/^https?:/i.test(item.url || "")) return false;
  if (item.byExtensionID) return false;
  const enabled = String(settings.types || "").split(",").map((s) => s.trim()).filter(Boolean);
  if (!enabled.includes(ndmCategoryOf(item))) return false;
  const size = item.totalBytes > 0 ? item.totalBytes : item.fileSize || 0;
  const min = Number(settings.minSizeKB) || 0;
  // An unknown size is offered rather than silently skipped.
  if (min > 0 && size > 0 && size < min * 1024) return false;
  const seen = ndmRecent.get(item.url);
  if (seen && Date.now() - seen < 5000) return false;
  return true;
}

function ndmRemember(url) {
  ndmRecent.set(url, Date.now());
  if (ndmRecent.size > 200) ndmRecent.delete(ndmRecent.keys().next().value);
}

/* Send a direct-file offer. Returns true when the browser copy was cancelled. */
async function ndmOfferDownload(item, api) {
  const message = {
    type: "offer",
    id: `dl-${item.id}`,
    url: item.url,
    filename: ndmBaseName(item.filename) || undefined,
    mime: item.mime || "",
    size: item.totalBytes > 0 ? item.totalBytes : 0,
    page_url: item.referrer || "",
  };
  let reply;
  try {
    reply = await api.send(message);
  } catch (error) {
    api.notify("NDM is not reachable", "Start NDM, then run: uv run ndm extension doctor");
    return false;
  }
  if (!reply || !reply.ok) {
    if (reply && reply.error) api.notify("NDM left this download in the browser", reply.error);
    return false;
  }
  // Context-menu offers have a synthetic id: no browser transfer exists yet.
  if (Number.isInteger(item.id)) {
    try {
      await api.cancel(item.id);
      await api.erase({ id: item.id });
    } catch (error) {
      // Already finished or gone; the browser copy is not lost.
    }
  }
  ndmRemember(item.url);
  api.notify(reply.status === "started" ? "Sent to NDM" : "Waiting in NDM",
             reply.status === "started"
               ? `${message.filename || "Download"} is downloading in NDM.`
               : `Approve ${message.filename || "the download"} in the NDM window.`);
  return true;
}

/* Hand a YouTube or Spotify page to the grabber. Nothing is cancelled here:
 * the browser was never downloading a file. */
async function ndmGrabPage(url, api) {
  const kind = ndmPageKind(url);
  if (!kind) {
    const error = "Open a YouTube video or a Spotify track, album or playlist.";
    api.notify("Not a media page", error);
    return { ok: false, error };
  }
  let reply;
  try {
    reply = await api.send({ type: "grab", id: `grab-${Date.now()}`, url, kind });
  } catch (error) {
    const detail = String(error);
    api.notify("NDM is not reachable", detail);
    return { ok: false, error: detail };
  }
  if (!reply || !reply.ok) {
    const detail = (reply && reply.error) || "Unknown error.";
    api.notify("NDM could not take this page", detail);
    return { ok: false, error: detail };
  }
  ndmRemember(url);
  api.notify(kind === "spotify" ? "Spotify link sent to NDM" : "YouTube link sent to NDM",
             "Review it in the NDM window.");
  return { ok: true, status: reply.status || "pending" };
}
