/* NDM Capture: observes media responses only while explicitly enabled.
 * No page contents, cookies, browsing history, or credentials are collected.
 * The private token is scoped to the daemon's capture endpoint.
 */
const seen = new Map();
const MEDIA = /\.(mp4|webm|mkv|mov|m4v|mp3|m4a|wav|flac|ogg|opus|aac|m3u8|mpd)(?:\?|$)/i;
const IGNORE = /\.(ts|m4s|vtt|jpg|png|gif)(?:\?|$)/i;

chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());
chrome.webRequest.onHeadersReceived.addListener(details => {
  if (details.method !== 'GET' || details.tabId < 0 || IGNORE.test(details.url)) return;
  handle(details).catch(error => chrome.storage.local.set({ lastResult: `Capture error: ${error.message}` }));
}, { urls: ['http://*/*', 'https://*/*'] }, ['responseHeaders']);

async function handle(details) {
  const config = await chrome.storage.local.get(['enabled', 'endpoint', 'token']);
  if (!config.enabled || !config.endpoint || !config.token) return;
  const headers = details.responseHeaders || [];
  const mime = (headers.find(h => h.name.toLowerCase() === 'content-type')?.value || '').split(';')[0].toLowerCase();
  const length = Number(headers.find(h => h.name.toLowerCase() === 'content-length')?.value || 0);
  const manifest = /\.(m3u8|mpd)(?:\?|$)/i.test(details.url);
  const media = mime.startsWith('audio/') || mime.startsWith('video/') || MEDIA.test(details.url);
  if (!media || (!manifest && length > 0 && length < 250000)) return;
  if (details.statusCode < 200 || details.statusCode >= 300) return;
  const now = Date.now();
  if (now - (seen.get(details.url) || 0) < 600000) return;
  seen.set(details.url, now);
  if (seen.size > 1000) for (const [url, time] of seen) if (now - time > 600000) seen.delete(url);
  let filename = null;
  try { filename = decodeURIComponent(new URL(details.url).pathname.split('/').pop()) || null; } catch {}
  const response = await fetch(config.endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${config.token}` },
    body: JSON.stringify({ url: details.url, filename, mime, source: 'extension', authorized: true }),
    signal: AbortSignal.timeout(15000),
  });
  const data = await response.json().catch(() => ({}));
  const message = response.ok ? `Captured ${data.name || filename || 'media'} at ${new Date().toLocaleTimeString()}` : `Not captured: ${data.detail || response.status}`;
  await chrome.storage.local.set({ lastResult: message });
  if (response.ok) {
    chrome.action.setBadgeBackgroundColor({ color: '#7856e8' });
    chrome.action.setBadgeText({ text: '•' });
  }
}
