const BROWSER = "chrome";
const KINDS = ["video", "audio", "archive", "document", "image", "other"];
const DEFAULTS = { enabled: true, minSizeKB: 0, types: KINDS.join(",") };
const statusEl = document.getElementById("status");
const enabledEl = document.getElementById("enabled");
const sizeEl = document.getElementById("minSize");
const typesEl = document.getElementById("types");
document.getElementById("setupCommand").textContent = `uv run ndm extension install --browser ${BROWSER}`;
document.getElementById("doctorCommand").textContent = `uv run ndm extension doctor --browser ${BROWSER}`;

function selectedTypes() {
  return KINDS.filter((kind) => document.getElementById(`t-${kind}`).checked).join(",");
}

async function save() {
  await browser.storage.local.set({
    enabled: enabledEl.checked,
    minSizeKB: Math.max(0, Number(sizeEl.value) || 0),
    types: selectedTypes(),
  });
}

async function load() {
  const stored = await browser.storage.local.get(DEFAULTS);
  const value = { ...DEFAULTS, ...stored };
  enabledEl.checked = value.enabled;
  sizeEl.value = value.minSizeKB;
  const active = String(value.types || "").split(",").map((s) => s.trim());
  for (const kind of KINDS) {
    const wrap = document.createElement("div");
    wrap.className = "row";
    wrap.style.margin = "0";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.id = `t-${kind}`;
    box.checked = active.includes(kind);
    box.addEventListener("change", save);
    const label = document.createElement("label");
    label.htmlFor = box.id;
    label.textContent = kind.charAt(0).toUpperCase() + kind.slice(1);
    wrap.append(box, label);
    typesEl.append(wrap);
  }
}

async function status() {
  let reply;
  try {
    reply = await chrome.runtime.sendMessage({ type: "status" });
  } catch (error) {
    reply = { ok: false, error: String(error) };
  }
  if (reply && reply.ok && reply.running) {
    statusEl.className = "status ok";
    statusEl.textContent = reply.enabled
      ? (reply.ask ? "NDM is running — it will ask before downloading."
                   : "NDM is running — downloads start automatically.")
      : "NDM is running, but browser integration is off. Run: uv run ndm config browser_integration true";
  } else {
    statusEl.className = "status bad";
    const detail = reply && reply.error ? String(reply.error) : "";
    statusEl.textContent = "NDM is not reachable. Start it, then run: uv run ndm extension doctor";
    if (detail) {
      // The real cause (missing host, import error, stopped daemon) beats a
      // generic hint; show it verbatim so it can be reported.
      const pre = document.createElement("div");
      pre.style.cssText = "margin-top:6px;font-size:11px;word-break:break-word;opacity:.85";
      pre.textContent = detail;
      statusEl.append(pre);
    }
  }
}

enabledEl.addEventListener("change", save);
sizeEl.addEventListener("change", save);
load().then(status);

// Hand the active tab to the grabber; NDM still asks before downloading.
document.getElementById("grab").addEventListener("click", async () => {
  const out = document.getElementById("grabResult");
  out.textContent = "Sending to NDM…";
  try {
    const reply = await chrome.runtime.sendMessage({ type: "grab" });
    out.textContent = reply && reply.ok ? "Sent. Review it in the NDM window."
                                        : (reply && reply.error) || "Could not send this page to NDM.";
  } catch (error) {
    out.textContent = String(error);
  }
});
