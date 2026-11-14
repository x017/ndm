import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity, ArrowDown, ArrowDownToLine, ArrowRight, ArrowUpRight, Bell, Check, CheckCheck,
  ChevronDown, ChevronRight, CircleHelp, Clipboard, Clock3, Copy, Download, Ellipsis,
  ExternalLink, FileText, Folder, FolderOpen, Gauge, HardDrive, Image, Layers3, Link2,
  Menu, Monitor, Moon, MoreHorizontal, Music2, Pause, Play, Plus, Radio, RefreshCw,
  ScanLine, Search, Settings2, ShieldCheck, SlidersHorizontal, Sparkles, Sun, Trash2,
  Video, X, Zap, Archive, CircleAlert, CheckCircle2,
} from 'lucide-react';
import { api, bytes, duration, percent, relativeTime, ACCENTS } from './api.js';
import { EmptyState, FileIcon, Loading, Logo, MiniSpark, Modal, SpeedChart, Status, Toasts, Toggle } from './components.jsx';
import { MediaPage, MusicPage, SettingsPage } from './pages.jsx';

const NAV = [
  { id: 'all', label: 'All files', icon: FolderOpen },
  { id: 'video', label: 'Videos', icon: Video },
  { id: 'audio', label: 'Audio', icon: Music2 },
  { id: 'archive', label: 'Archives', icon: Archive },
  { id: 'image', label: 'Images', icon: Image },
  { id: 'document', label: 'Documents', icon: FileText },
];

export default function App() {
  const [state, setState] = useState(null);
  const [connectionError, setConnectionError] = useState('');
  const [page, setPage] = useState('downloads');
  const [category, setCategory] = useState('all');
  const [tab, setTab] = useState('all');
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState('newest');
  const [selected, setSelected] = useState(new Set());
  const [modal, setModal] = useState(null);
  const [detailsId, setDetailsId] = useState(null);
  const [toasts, setToasts] = useState([]);
  const [notifications, setNotifications] = useState(false);
  const [lastRead, setLastRead] = useState(Number(localStorage.getItem('ndm-last-read') || 0));
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [systemDark, setSystemDark] = useState(matchMedia('(prefers-color-scheme: dark)').matches);
  const [bannerDismissed, setBannerDismissed] = useState(localStorage.getItem('ndm-banner') === 'hidden');
  const searchRef = useRef(null);
  const fetching = useRef(false);
  const knownCompleted = useRef(null);

  const toast = useCallback((message, tone = 'success') => {
    const id = crypto.randomUUID();
    setToasts(items => [...items.slice(-3), { id, message, tone }]);
    setTimeout(() => setToasts(items => items.filter(item => item.id !== id)), 5000);
  }, []);
  const refresh = useCallback(async () => {
    if (fetching.current) return;
    fetching.current = true;
    try {
      const next = await api('/state');
      const completedIds = new Set(next.downloads.filter(t => t.status === 'completed').map(t => t.id));
      if (knownCompleted.current && next.settings.notifications) {
        for (const task of next.downloads) {
          if (task.status === 'completed' && !knownCompleted.current.has(task.id)) toast(`${task.name} is ready.`);
        }
      }
      knownCompleted.current = completedIds;
      setState(next);
      setConnectionError('');
    } catch (error) { setConnectionError(error.message); }
    finally { fetching.current = false; }
  }, [toast]);
  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 1000);
    const media = matchMedia('(prefers-color-scheme: dark)');
    const change = e => setSystemDark(e.matches);
    media.addEventListener('change', change);
    return () => { clearInterval(timer); media.removeEventListener('change', change); };
  }, [refresh]);
  useEffect(() => {
    const key = e => {
      const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); searchRef.current?.focus(); }
      if (!typing && !modal && !detailsId && e.key.toLowerCase() === 'n') { e.preventDefault(); setModal({ type: 'add' }); }
      if (e.key === 'Escape') { setNotifications(false); setSidebarOpen(false); }
    };
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  }, [modal, detailsId]);
  useEffect(() => {
    if (!state) return;
    const settings = state.settings;
    const dark = settings.theme === 'dark' || (settings.theme === 'system' && systemDark);
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    document.documentElement.style.setProperty('--accent', settings.accent);
    document.documentElement.style.setProperty('--accent-soft', settings.accent + '13');
    document.documentElement.style.setProperty('--accent-border', settings.accent + '35');
    const rgb = settings.accent.match(/\w\w/g).map(x => parseInt(x, 16));
    document.documentElement.style.setProperty('--accent-foreground', rgb[0] * .299 + rgb[1] * .587 + rgb[2] * .114 > 165 ? '#172033' : '#ffffff');
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', settings.accent);
  }, [state?.settings.theme, state?.settings.accent, systemDark, !!state]);

  const navigate = useCallback((nextPage, nextCategory = 'all') => {
    setPage(nextPage); setCategory(nextCategory); setTab('all'); setQuery('');
    setSelected(new Set()); setSidebarOpen(false); setNotifications(false);
  }, []);
  const updateSettings = useCallback(async patch => {
    setState(current => ({ ...current, settings: { ...current.settings, ...patch } }));
    try { await api('/settings', { method: 'PATCH', body: patch }); await refresh(); return true; }
    catch (error) { toast(error.message, 'error'); refresh(); return false; }
  }, [refresh, toast]);
  const act = useCallback(async (id, action) => {
    try {
      await api(`/downloads/${id}/${action}`, { method: 'POST' });
      await refresh();
      if (action === 'pause') toast('Download paused. Pick up right where you left off.');
      if (action === 'resume') toast('Back in the queue. NDM has it from here.');
      if (action === 'retry') toast('Retrying your download.');
      if (action === 'delete') { setDetailsId(null); toast('Removed from downloads. Completed files are kept.'); }
    } catch (error) { toast(error.message, 'error'); }
  }, [refresh, toast]);
  const bulk = async (action, ids) => {
    if (action === 'delete') { setModal({ type: 'delete', ids }); return; }
    try {
      await api('/bulk', { method: 'POST', body: { ids, action } });
      setSelected(new Set()); await refresh(); toast(`${ids.length} downloads ${action === 'pause' ? 'paused' : 'added to the queue'}.`);
    } catch (error) { toast(error.message, 'error'); }
  };
  const confirmDelete = async () => {
    const ids = modal.ids;
    try {
      await api('/bulk', { method: 'POST', body: { ids, action: 'delete' } });
      setModal(null); setSelected(new Set()); setDetailsId(null); await refresh();
      toast('Removed from your workspace. Completed files are kept.');
    } catch (error) { toast(error.message, 'error'); }
  };
  const addDownload = async payload => {
    await api('/downloads', { method: 'POST', body: payload });
    setModal(null); navigate('downloads'); await refresh(); toast('Download added. Let the waiting be our job.');
  };
  const copy = async (text, message = 'Copied to clipboard.') => {
    try { await navigator.clipboard.writeText(text); toast(message); }
    catch { toast('Clipboard access is unavailable. Select and copy the text instead.', 'error'); }
  };

  const filtered = useMemo(() => {
    if (!state) return [];
    const statusOrder = { downloading: 0, queued: 1, paused: 2, error: 3, completed: 4 };
    return state.downloads.filter(task =>
      (category === 'all' || task.category === category) &&
      (tab === 'all' || task.status === tab || (tab === 'error' && task.status === 'error')) &&
      (!query || `${task.name} ${task.host}`.toLowerCase().includes(query.toLowerCase()))
    ).sort((a, b) => sort === 'name' ? a.name.localeCompare(b.name) : sort === 'size' ? b.size - a.size : sort === 'oldest' ? a.created_at - b.created_at : (statusOrder[a.status] - statusOrder[b.status]) || b.created_at - a.created_at);
  }, [state, category, tab, query, sort]);

  if (!state) return <><Loading />{connectionError && <div className="connection-error"><CircleAlert size={18} />{connectionError}<button onClick={refresh}>Try again</button></div>}</>;
  const { settings, stats, daemon } = state;
  const details = state.downloads.find(task => task.id === detailsId);
  const unread = state.events.filter(event => event.time > lastRead).length;
  const title = page === 'media' ? 'Media grabber' : page === 'music' ? 'Offline music' : page === 'settings' ? 'Settings' : category !== 'all' ? NAV.find(n => n.id === category)?.label || 'Downloads' : 'Downloads';
  const toggleTheme = () => updateSettings({ theme: document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark' });

  return <div className="app-shell">
    {sidebarOpen && <div className="sidebar-scrim" onClick={() => setSidebarOpen(false)} />}
    <aside className={`sidebar ${sidebarOpen ? 'open' : ''}`}>
      <button className="brand-button" aria-label="NDM home" onClick={() => navigate('downloads')}><Logo /></button>
      <button className="workspace-picker" onClick={() => navigate('settings')}><span className="workspace-avatar">P</span><span><strong>Personal workspace</strong><small>Your space. Your pace.</small></span><ChevronDown size={14} /></button>
      <div className="nav-label">WORKSPACE</div>
      <nav className="primary-nav" aria-label="Workspace">
        <button className={page === 'downloads' ? 'active' : ''} onClick={() => navigate('downloads')}><ArrowDownToLine size={18} /><span>Downloads</span><span className="nav-count">{stats.total}</span></button>
        <button className={page === 'media' ? 'active' : ''} onClick={() => navigate('media')}><ScanLine size={18} /><span>Media grabber</span><span className="new-dot" /></button>
        <button className={page === 'music' ? 'active' : ''} onClick={() => navigate('music')}><Music2 size={18} /><span>Offline music</span></button>
      </nav>
      <div className="nav-divider" />
      <div className="nav-label library-label">LIBRARY <span>{stats.completed}</span></div>
      <nav className="library-nav" aria-label="File categories">{NAV.map(({ id, label, icon: Icon }) => <button key={id} className={page === 'downloads' && category === id ? 'category-active' : ''} onClick={() => navigate('downloads', id)}><Icon size={17} strokeWidth={1.65} /><span>{label}</span><small>{id === 'all' ? stats.completed : state.downloads.filter(t => t.status === 'completed' && t.category === id).length || ''}</small></button>)}</nav>
      <div className="sidebar-bottom">
        <button className={`daemon-mini ${settings.engine_running ? '' : 'is-paused'}`} onClick={() => navigate('settings', 'background')}><span className="daemon-mini-icon"><Radio size={18} /></span><strong>{settings.engine_running ? 'Quietly working for you.' : 'Taking a little breather.'}</strong><p>{settings.engine_running ? 'Your daemon keeps downloads moving, even when you step away.' : 'Resume the engine whenever you’re ready.'}</p><span className="daemon-mini-status"><i />{settings.engine_running ? 'Background engine online' : 'Background engine paused'}</span></button>
        <button className={`settings-nav ${page === 'settings' ? 'active' : ''}`} onClick={() => navigate('settings')}><Settings2 size={18} /><span>Settings</span></button>
        <div className="sidebar-footer"><span className="local-avatar"><Monitor size={16} /></span><span><strong>{daemon.hosted ? 'Preview workspace' : 'Local workspace'}</strong><small>NDM version {daemon.version}</small></span><button className="icon-button" aria-label="Toggle light and dark theme" onClick={toggleTheme}>{document.documentElement.dataset.theme === 'dark' ? <Sun size={17} /> : <Moon size={17} />}</button></div>
      </div>
    </aside>

    <div className="workspace-main">
      <header className="topbar">
        <button className="icon-button mobile-menu" aria-label="Open navigation" onClick={() => setSidebarOpen(true)}><Menu size={21} /></button>
        <div className="breadcrumbs"><span>Workspace</span><ChevronRight size={13} /><strong>{title}</strong></div>
        <div className="topbar-right"><div className="global-search"><Search size={16} /><input ref={searchRef} aria-label="Search downloads" placeholder="Search anything..." value={query} onChange={e => { setQuery(e.target.value); if (page !== 'downloads') { setPage('downloads'); setCategory('all'); setTab('all'); } }} />{query ? <button aria-label="Clear search" onClick={() => setQuery('')}><X size={13} /></button> : <kbd>⌘ K</kbd>}</div>
          {daemon.hosted && <span className="preview-badge" title="Local sample files, transferred by the real NDM engine"><span />Live preview</span>}
          <div className="notification-wrap"><button className={`icon-button notification-button ${notifications ? 'pressed' : ''}`} aria-label="Notifications" aria-expanded={notifications} onClick={() => { setNotifications(!notifications); if (!notifications) { const now = Date.now() / 1000; setLastRead(now); localStorage.setItem('ndm-last-read', now); } }}><Bell size={19} />{unread > 0 && <i />}</button>
            {notifications && <div className="notification-panel"><div className="popover-heading"><h3>Activity</h3><span>All caught up <CheckCheck size={14} /></span></div>{state.events.length ? state.events.slice(0, 12).map(event => <div className="notification-item" key={event.id}><span className={`event-dot ${event.tone}`} /><div><strong>{event.title}</strong><p>{event.detail}</p><small>{relativeTime(event.time)}</small></div></div>) : <p className="muted padded">Your download activity will appear here.</p>}</div>}
          </div><span className="topbar-divider" /><button className="profile-avatar" aria-label="Workspace settings" onClick={() => navigate('settings')}>P</button>
        </div>
      </header>
      {connectionError && <div className="offline-banner"><CircleAlert size={16} />Reconnecting to the download engine…<button onClick={refresh}>Retry now</button></div>}
      <main className={`main-content page-${page}`}>
        <div className="page-heading"><div><div className="eyebrow">{page === 'downloads' ? 'A LITTLE LESS WAITING' : page === 'media' ? 'FROM LINK TO LIBRARY' : page === 'music' ? 'YOURS, EVEN OFFLINE' : 'MAKE YOURSELF AT HOME'}</div><h1>{title}<span className="heading-dot">.</span></h1><p>{page === 'downloads' ? 'Everything you need. Nothing in the way.' : page === 'media' ? 'Find the media. Keep what’s yours. Let NDM do the rest.' : page === 'music' ? 'A little collection of sounds that goes wherever you do.' : 'A workspace that works the way you do.'}</p></div>
          {page === 'downloads' && <button className="button primary new-download" onClick={() => setModal({ type: 'add' })}><Plus size={18} />New download<kbd>N</kbd></button>}
          {page === 'music' && <button className="button primary" onClick={() => setModal({ type: 'add' })}><Plus size={17} />Add audio</button>}
          {page === 'media' && <span className="heading-badge"><ShieldCheck size={15} />Permission-first capture</span>}
        </div>

        {page === 'downloads' && <>
          {!bannerDismissed && <section className="welcome-banner"><div className="banner-art" aria-hidden="true"><span className="orbit orbit-one" /><span className="orbit orbit-two" /><span className="banner-download"><ArrowDownToLine size={27} strokeWidth={1.8} /></span><span className="orbit-dot" /></div><div className="banner-copy"><h2>Big files. Small effort.</h2><p>Smart segments, seamless resumes, and a quiet engine that keeps things moving.</p></div><div className="banner-features"><span><Zap size={13} />Multi-connection</span><span><RefreshCw size={13} />Auto-resume</span></div><button className="banner-close" aria-label="Dismiss introduction" onClick={() => { setBannerDismissed(true); localStorage.setItem('ndm-banner', 'hidden'); }}><X size={15} /></button></section>}
          <section className="stats-grid" aria-label="Download statistics">
            <button className="stat-card" onClick={() => { setTab('downloading'); setCategory('all'); }}><div className="stat-top"><span>Active downloads</span><span className="stat-icon violet"><ArrowDownToLine size={16} /></span></div><div className="stat-value">{stats.active}<span className="stat-live"><i />{stats.active ? 'In motion' : 'All quiet'}</span></div><div className="stat-foot">{stats.queued} waiting in queue<span className="tiny-bars"><i /><i /><i /><i /><i /></span></div></button>
            <div className="stat-card"><div className="stat-top"><span>Transfer speed</span><span className="stat-icon blue"><Activity size={16} /></span></div><div className="stat-value speed-value">{bytes(stats.speed).split(' ')[0]}<small>{bytes(stats.speed).split(' ')[1]}/s</small></div><div className="stat-foot">{stats.connections} active connections</div><MiniSpark values={state.history.slice(-25)} /></div>
            <button className="stat-card" onClick={() => { setTab('completed'); setCategory('all'); }}><div className="stat-top"><span>Completed today</span><span className="stat-icon green"><CheckCircle2 size={16} /></span></div><div className="stat-value">{stats.completed_today}<span className="stat-unit">files</span></div><div className="stat-foot"><span className="green-text">{bytes(stats.bytes_today)}</span> safely downloaded</div></button>
            <div className="stat-card"><div className="stat-top"><span>Total downloaded</span><span className="stat-icon amber"><HardDrive size={16} /></span></div><div className="stat-value">{bytes(stats.bytes_total).split(' ')[0]}<small>{bytes(stats.bytes_total).split(' ')[1]}</small></div><div className="stat-foot">{bytes(stats.disk_free)} free on disk<span className="disk-mini"><i style={{ width: `${Math.max(5, (1 - stats.disk_free / stats.disk_total) * 100)}%` }} /></span></div></div>
          </section>
          <div className="download-layout"><section className="transfer-panel">
            <div className="panel-heading"><h2>Your downloads <span className="count-badge">{stats.total}</span></h2><button className="text-button subtle" onClick={() => { const ids = state.downloads.filter(t => t.status === 'completed').map(t => t.id); if (ids.length) bulk('delete', ids); else toast('Your completed list is already clear.'); }}><CheckCheck size={15} />Clear completed</button></div>
            <div className="transfer-toolbar"><div className="tabs" role="tablist" aria-label="Download status">{[{ id: 'all', label: 'All downloads', count: stats.total }, { id: 'downloading', label: 'Active', count: stats.active }, { id: 'queued', label: 'Queued', count: stats.queued }, { id: 'completed', label: 'Completed', count: stats.completed }].map(item => <button key={item.id} role="tab" aria-selected={tab === item.id} className={tab === item.id ? 'active' : ''} onClick={() => { setTab(item.id); setSelected(new Set()); }}>{item.label}{item.id === 'all' && <span>{item.count}</span>}{item.id === 'downloading' && item.count > 0 && <i />}</button>)}</div><div className="table-tools"><div className="select-tool"><SlidersHorizontal size={14} /><select aria-label="Filter by file type" value={category} onChange={e => setCategory(e.target.value)}><option value="all">Filter</option>{NAV.slice(1).map(n => <option key={n.id} value={n.id}>{n.label}</option>)}<option value="other">Other</option></select></div><div className="select-tool sort-tool"><select aria-label="Sort downloads" value={sort} onChange={e => setSort(e.target.value)}><option value="newest">Newest first</option><option value="oldest">Oldest first</option><option value="name">Name A–Z</option><option value="size">Largest first</option></select><ChevronDown size={13} /></div></div></div>
            {selected.size > 0 && <div className="bulk-toolbar"><span>{selected.size} selected</span><button onClick={() => bulk('pause', [...selected])}><Pause size={13} />Pause</button><button onClick={() => bulk('resume', [...selected])}><Play size={13} />Resume</button><button className="danger-text" onClick={() => bulk('delete', [...selected])}><Trash2 size={13} />Remove</button><button className="bulk-clear" onClick={() => setSelected(new Set())}><X size={14} /></button></div>}
            {(tab !== 'all' || category !== 'all' || query) && <div className="active-filters"><span>{filtered.length} results{query && ` for “${query}”`}{category !== 'all' && ` in ${category}`}{tab !== 'all' && ` · ${tab === 'downloading' ? 'active' : tab}`}</span><button onClick={() => { setTab('all'); setCategory('all'); setQuery(''); }}>Clear filters <X size={12} /></button></div>}
            <div className="transfer-table"><div className="transfer-table-head"><label className="checkbox-wrap"><input type="checkbox" aria-label="Select all visible downloads" checked={filtered.length > 0 && filtered.every(t => selected.has(t.id))} onChange={e => setSelected(e.target.checked ? new Set(filtered.map(t => t.id)) : new Set())} /></label><span>File name</span><span>Progress</span><span>Status</span><span /></div>
              {filtered.length ? filtered.map(task => <DownloadRow key={task.id} task={task} selected={selected.has(task.id)} select={() => setSelected(current => { const next = new Set(current); next.has(task.id) ? next.delete(task.id) : next.add(task.id); return next; })} act={act} details={() => setDetailsId(task.id)} />) : <EmptyState icon={query ? Search : ArrowDownToLine} title={query || tab !== 'all' || category !== 'all' ? 'Nothing here just yet' : 'Make room for something good'} text={query || tab !== 'all' || category !== 'all' ? 'Try another search or clear your filters.' : 'Paste a link, pick a file, and get on with your day.'} action={<button className="button primary" onClick={() => setModal({ type: 'add' })}><Plus size={16} />Add a download</button>} />}
            </div><footer className="table-footer"><span>Showing {filtered.length} of {stats.total} downloads</span><span><i className={settings.engine_running ? 'online-dot' : 'paused-dot'} />{settings.engine_running ? 'Your queue runs automatically' : 'Engine is paused'}</span></footer>
          </section><aside className="right-rail">
            <section className="rail-card engine-card"><div className="rail-heading"><h3><Radio size={16} />Background engine</h3><span className={`running-label ${settings.engine_running ? '' : 'paused'}`}><i />{settings.engine_running ? 'Running' : 'Paused'}</span></div><div className="engine-toggle"><div><strong>Always in motion</strong><p>Downloads keep going.<br />You don’t have to.</p></div><Toggle checked={settings.engine_running} onChange={value => updateSettings({ engine_running: value })} label="Run background download engine" /></div><div className="engine-metrics"><div><span>Concurrent files</span><strong>{stats.active}<small> / {settings.max_concurrent}</small></strong></div><div><span>Connections</span><strong>{stats.connections}<small> active</small></strong></div></div><button className="rail-footer-link" onClick={() => navigate('settings', 'background')}><span><ShieldCheck size={14} />{daemon.hosted ? 'Sandbox daemon online' : 'Independent of this window'}</span><ArrowUpRight size={15} /></button></section>
            <section className="rail-card activity-card"><div className="rail-heading"><h3>Transfer activity</h3><span className="live-chart-label"><i />Live</span></div><div className="chart-summary"><strong>{bytes(stats.speed)}<small>/s</small></strong><span>Across all downloads</span></div><SpeedChart values={state.history} /><div className="chart-legend"><span><i />Download speed</span><small>Last 60 seconds</small></div></section>
            <section className="appearance-teaser"><div className="teaser-top"><span className="palette-art"><i /><i /><i /></span><Sparkles size={17} /></div><h3>A little more you.</h3><p>Your workspace, in your colors.</p><div className="teaser-swatches">{ACCENTS.map(accent => <button key={accent.color} title={accent.name} aria-label={`Use ${accent.name} accent`} className={settings.accent === accent.color ? 'chosen' : ''} style={{ '--swatch': accent.color }} onClick={() => updateSettings({ accent: accent.color })}>{settings.accent === accent.color && <Check size={12} />}</button>)}</div><button onClick={() => navigate('settings', 'appearance')}>Make it yours <ArrowRight size={14} /></button></section>
          </aside></div>
          {daemon.hosted && <div className="preview-note"><ShieldCheck size={13} /><span>Preview files are generated locally and transferred through the real engine. Your desktop downloads stay on your machine.</span></div>}
        </>}
        {page === 'media' && <MediaPage state={state} updateSettings={updateSettings} toast={toast} refresh={refresh} copy={copy} navigate={navigate} />}
        {page === 'music' && <MusicPage toast={toast} navigate={navigate} settings={settings} />}
        {page === 'settings' && <SettingsPage key={category} state={state} initialTab={category} updateSettings={updateSettings} toast={toast} copy={copy} navigate={navigate} />}
        <footer className="workspace-footer"><span><Logo small />Your downloads, in flow.</span><button onClick={() => setModal({ type: 'about' })}>Made with Nexa UI <ArrowUpRight size={12} /></button></footer>
      </main>
    </div>
    {modal?.type === 'add' && <AddDownload settings={settings} onClose={() => setModal(null)} onSubmit={addDownload} toast={toast} />}
    {modal?.type === 'delete' && <Modal title="Remove these downloads?" subtitle="A little housekeeping. Your completed files stay safe." onClose={() => setModal(null)}><div className="modal-body"><div className="delete-notice"><Trash2 size={23} /><p>Remove {modal.ids.length} {modal.ids.length === 1 ? 'entry' : 'entries'} from your workspace? Unfinished partial files will be deleted. Completed files are never removed from disk.</p></div></div><footer className="modal-footer"><button className="button" onClick={() => setModal(null)}>Keep them</button><button className="button danger" onClick={confirmDelete}><Trash2 size={15} />Remove downloads</button></footer></Modal>}
    {modal?.type === 'about' && <Modal title="A quieter kind of download manager." subtitle="NDM 0.1.0 · Built with Python and Nexa UI" onClose={() => setModal(null)}><div className="modal-body about-body"><Logo /><p>The desktop app is rendered with the real <strong>nexa-ui</strong> native library. This browser companion shares its Python download engine, queue, and preferences.</p><div className="about-facts"><span><Zap size={16} />Segmented, resumable transfers</span><span><Radio size={16} />Independent background daemon</span><span><Music2 size={16} />Your own offline music library</span><span><ShieldCheck size={16} />Opt-in, permission-first media capture</span></div><p className="muted">Spotify links open in Spotify. Protected streams are not ripped or decrypted. See README.md for desktop setup and service installation.</p></div><footer className="modal-footer"><button className="button primary" onClick={() => setModal(null)}>Back to the flow <ArrowRight size={15} /></button></footer></Modal>}
    {details && <Details task={details} onClose={() => setDetailsId(null)} act={act} copy={copy} remove={() => setModal({ type: 'delete', ids: [details.id] })} hosted={daemon.hosted} toast={toast} />}
    <Toasts items={toasts} dismiss={id => setToasts(items => items.filter(item => item.id !== id))} />
  </div>;
}

function DownloadRow({ task, selected, select, act, details }) {
  const progress = percent(task);
  const active = task.status === 'downloading';
  const remaining = active && task.speed ? (task.size - task.downloaded) / task.speed : null;
  return <div className={`download-row ${selected ? 'selected' : ''} ${task.status}`}>
    <label className="checkbox-wrap"><input type="checkbox" aria-label={`Select ${task.name}`} checked={selected} onChange={select} /></label>
    <button className="file-cell" onClick={details}><FileIcon category={task.category} /><span><strong title={task.name}>{task.name}</strong><small>{task.source === 'demo' ? <><span className="sample-label">SAMPLE</span>Local demo file</> : <>{task.host}<span className="file-host-dot">·</span>{task.source === 'ytdlp' ? 'Media link' : task.category}</>}</small></span></button>
    <div className="progress-cell"><div className={`progress-track ${task.status}`} role="progressbar" aria-label={`${task.name} progress`} aria-valuenow={Math.round(progress)} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${progress}%` }} /></div><div className="progress-meta"><span>{task.status === 'completed' ? bytes(task.size) : `${bytes(task.downloaded)}${task.size ? ` / ${bytes(task.size)}` : ''}`}</span><strong>{task.status === 'completed' ? <Check size={12} /> : `${Math.floor(progress)}%`}</strong></div></div>
    <div className="status-cell"><Status task={task} /><small>{active ? `${bytes(task.speed)}/s${remaining !== null ? ` · ${duration(remaining)}` : ''}` : task.status === 'completed' ? relativeTime(task.completed_at) : task.status === 'error' ? 'Click for details' : task.status === 'queued' ? 'Up next' : 'Ready when you are'}</small></div>
    <div className="row-actions">{task.status !== 'completed' && <button className="icon-button" aria-label={`${active || task.status === 'queued' ? 'Pause' : 'Resume'} ${task.name}`} title={active || task.status === 'queued' ? 'Pause' : 'Resume'} onClick={() => act(task.id, active || task.status === 'queued' ? 'pause' : task.status === 'error' ? 'retry' : 'resume')}>{active || task.status === 'queued' ? <Pause size={15} /> : task.status === 'error' ? <RefreshCw size={15} /> : <Play size={15} />}</button>}<button className="icon-button" aria-label={`Details for ${task.name}`} title="Download details" onClick={details}><MoreHorizontal size={18} /></button></div>
  </div>;
}

function AddDownload({ settings, onClose, onSubmit, toast }) {
  const [url, setUrl] = useState('');
  const [filename, setFilename] = useState('');
  const [category, setCategory] = useState('auto');
  const [connections, setConnections] = useState(settings.connections);
  const [paused, setPaused] = useState(false);
  const [authorized, setAuthorized] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const isSpotify = /(^|\.)spotify\.(com|link)$/i.test((() => { try { return new URL(url).hostname; } catch { return ''; } })());
  const submit = async event => {
    event.preventDefault(); setBusy(true); setError('');
    try { await onSubmit({ url: url.trim(), filename: filename.trim() || null, category: category === 'auto' ? null : category, connections, start_paused: paused, authorized }); }
    catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };
  const paste = async () => { try { setUrl(await navigator.clipboard.readText()); } catch { toast('Use Ctrl/V or ⌘V to paste your link here.', 'error'); } };
  return <Modal title="Good things, incoming." subtitle="Add a link. We’ll take care of the wait." onClose={onClose}><form onSubmit={submit}><div className="modal-body form-stack"><label className="field-label">Download link <span>Required</span><div className="input-with-icon"><Link2 size={17} /><input autoFocus required type="url" placeholder="https://example.com/something-good.zip" value={url} onChange={e => setUrl(e.target.value)} /><button type="button" className="paste-button" onClick={paste}><Clipboard size={14} />Paste</button></div></label>{isSpotify && <div className="inline-notice"><Music2 size={18} /><p>Spotify’s protected tracks stay in Spotify. Use Premium offline mode, or add an audio file you own.</p><a className="button small" href={url} target="_blank" rel="noreferrer">Open Spotify <ExternalLink size={13} /></a></div>}<label className="field-label">File name <span>Optional</span><input placeholder="Let the server name it, or make it your own" value={filename} maxLength={180} onChange={e => setFilename(e.target.value)} /></label><div className="form-grid"><label className="field-label">Category<select value={category} onChange={e => setCategory(e.target.value)}><option value="auto">Detect automatically</option>{NAV.slice(1).map(n => <option key={n.id} value={n.id}>{n.label}</option>)}<option value="other">Other</option></select></label><label className="field-label">Connections<select value={connections} onChange={e => setConnections(Number(e.target.value))}>{[1, 2, 4, 8].map(n => <option key={n} value={n}>{n} {n === 1 ? 'connection' : 'connections'}</option>)}</select></label></div><div className="destination-note"><Folder size={17} /><div><small>Saving to</small><span>{settings.download_dir}</span></div><ShieldCheck size={16} /></div><label className="check-label"><input type="checkbox" checked={paused} onChange={e => setPaused(e.target.checked)} /><span>Add to queue without starting<small>Start it whenever the time is right.</small></span></label><label className="check-label permission-check"><input type="checkbox" checked={authorized} onChange={e => setAuthorized(e.target.checked)} /><span>I own this content or have permission to download it.<small>Required for audio and video. No DRM bypassing.</small></span></label>{error && <div className="form-error" role="alert"><CircleAlert size={16} />{error}</div>}</div><footer className="modal-footer"><button type="button" className="button" onClick={onClose}>Cancel</button><button className="button primary" disabled={busy || isSpotify}><ArrowDownToLine size={16} />{busy ? 'Adding…' : paused ? 'Add to queue' : 'Start download'}</button></footer></form></Modal>;
}

function Details({ task, onClose, act, copy, remove, hosted, toast }) {
  const progress = percent(task);
  const openFolder = async () => { try { await api(`/open/${task.id}`, { method: 'POST' }); toast('Opened on the machine running NDM.'); } catch (error) { toast(error.message, 'error'); } };
  return <div className="drawer-backdrop" onMouseDown={e => e.target === e.currentTarget && onClose()}><aside className="details-drawer" role="dialog" aria-modal="true" aria-label="Download details"><header><span>Download details</span><button className="icon-button" aria-label="Close details" onClick={onClose}><X size={19} /></button></header><div className="drawer-body"><div className="details-file"><FileIcon category={task.category} size={29} /><h2>{task.name}</h2><Status task={task} /></div>{task.status === 'error' && <div className="form-error"><CircleAlert size={18} />{task.error}</div>}<div className="details-progress"><div><strong>{Math.floor(progress)}%</strong><span>{bytes(task.downloaded)} of {bytes(task.size)}</span></div><div className={`progress-track ${task.status}`}><span style={{ width: `${progress}%` }} /></div><small>{task.status === 'downloading' ? `${bytes(task.speed)}/s · ${task.connections} connections` : task.status === 'completed' ? 'Safely saved and ready to use.' : 'Your progress is saved.'}</small></div><div className="detail-actions">{task.status !== 'completed' && <button className="button primary" onClick={() => act(task.id, ['downloading', 'queued'].includes(task.status) ? 'pause' : task.status === 'error' ? 'retry' : 'resume')}>{['downloading', 'queued'].includes(task.status) ? <Pause size={16} /> : <Play size={16} />}{['downloading', 'queued'].includes(task.status) ? 'Pause' : task.status === 'error' ? 'Retry' : 'Resume'}</button>}{task.status === 'completed' && <a className="button primary" href={`/api/downloads/${task.id}/file`} download><Download size={16} />Save file</a>}<button className="button" onClick={() => copy(task.url)}><Copy size={15} />Copy link</button></div><dl className="details-list"><div><dt>Source</dt><dd>{task.source === 'demo' ? 'Local sample file' : task.host}</dd></div><div><dt>Category</dt><dd className="capitalize">{task.category}</dd></div><div><dt>Added</dt><dd>{new Date(task.created_at * 1000).toLocaleString()}</dd></div><div><dt>Connections</dt><dd>{task.segments.length || task.connections}</dd></div><div><dt>Resume support</dt><dd>{task.resumable ? 'Server supports byte ranges' : task.status === 'completed' ? 'Download finished' : 'Detected when transfer starts'}</dd></div><div><dt>Saved to</dt><dd className="path-value">{task.path}</dd></div><div><dt>Original URL</dt><dd className="path-value">{task.url}<button className="icon-button" aria-label="Copy original URL" onClick={() => copy(task.url)}><Copy size={13} /></button></dd></div></dl>{task.segments.length > 0 && <section className="segments-section"><h3>Under the hood <Layers3 size={15} /></h3><p>{task.segments.length} smart segments, one complete file.</p><div className="segment-bars">{task.segments.map((segment, index) => <div key={index} title={`Segment ${index + 1}: ${bytes(segment.done)}`}><span style={{ width: `${segment.end >= segment.start ? Math.min(100, segment.done / (segment.end - segment.start + 1) * 100) : 0}%` }} /></div>)}</div></section>}{task.status === 'completed' && !hosted && <button className="button full-width" onClick={openFolder}><FolderOpen size={16} />Show in folder</button>}</div><footer><button className="text-button danger-text" onClick={remove}><Trash2 size={15} />Remove from downloads</button><small>Completed files are always kept on disk.</small></footer></aside></div>;
}
