export async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', 'X-NDM-Client': '1', ...options.headers },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    throw new Error(typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(e => e.msg).join(' ') : 'NDM could not complete this request.');
  }
  return data;
}

export function bytes(value, digits = 1) {
  if (!value) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** index).toFixed(index ? digits : 0)} ${units[index]}`;
}

export function duration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '—';
  if (seconds < 60) return `${Math.ceil(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.ceil(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
}

export function percent(task) {
  return task.size ? Math.min(100, task.downloaded / task.size * 100) : 0;
}

export function relativeTime(timestamp) {
  const seconds = Math.max(0, Date.now() / 1000 - timestamp);
  if (seconds < 60) return 'Just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return new Date(timestamp * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export const ACCENTS = [
  { color: '#7856e8', name: 'Iris' }, { color: '#4778e6', name: 'Sky' },
  { color: '#169c83', name: 'Jade' }, { color: '#e29342', name: 'Amber' },
  { color: '#dc657e', name: 'Rose' }, { color: '#64748b', name: 'Slate' },
];
