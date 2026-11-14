const fields = ['enabled', 'endpoint', 'token'];
chrome.storage.local.get([...fields, 'lastResult']).then(config => {
  document.getElementById('enabled').checked = Boolean(config.enabled);
  document.getElementById('endpoint').value = config.endpoint || 'http://127.0.0.1:8765/api/capture';
  document.getElementById('token').value = config.token || '';
  document.getElementById('status').textContent = config.lastResult || '';
});
document.getElementById('save').addEventListener('click', async () => {
  const endpoint = document.getElementById('endpoint').value.trim();
  const token = document.getElementById('token').value.trim();
  const enabled = document.getElementById('enabled').checked;
  try {
    const url = new URL(endpoint);
    if (!['http:', 'https:'].includes(url.protocol) || !url.pathname.endsWith('/api/capture')) throw new Error();
    if (enabled && !token) throw new Error();
  } catch {
    document.getElementById('status').textContent = 'Enter the full /api/capture endpoint and your private token.';
    return;
  }
  await chrome.storage.local.set({ endpoint, token, enabled });
  if (!enabled) await chrome.action.setBadgeText({ text: '' });
  document.getElementById('status').textContent = enabled ? 'Saved. Media capture is on — turn it off when you’re done.' : 'Saved. Media capture is off.';
});
