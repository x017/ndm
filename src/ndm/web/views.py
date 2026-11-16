"""HTML equivalents of the Nexa desktop's semantic controls and layout."""
from html import escape

from ..models import COOKIE_BROWSERS


def e(value):
    return escape(str(value), quote=True)


def size(value):
    value = float(value or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024:
            return f'{value:.1f} {unit}'
        value /= 1024
    return f'{value:.1f} PB'


def button(label, route, values, cls=''):
    import json
    return f'<button class="{cls}" hx-post="{route}" hx-vals="{e(json.dumps(values))}" hx-target="#notice">{e(label)}</button>'


def page(csrf):
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NDM · Downloads</title><link rel="stylesheet" href="/static/app.css">
<script src="/static/htmx.min.js"></script><script defer src="/static/alpine.min.js"></script></head>
<body x-data="{modal:false, selected:'', tab:'all', dark:localStorage.getItem('ndm-dark')==='true'}" :class="{'dark':dark}"
@keydown.escape.window="modal=false" @close-dialog.window="modal=false" hx-headers='{"X-NDM-CSRF":"''' + csrf + '''"}'>
<header><strong class="brand">↧ <span>NDM</span></strong><nav>File <span>Downloads</span> View <span>Tools</span> Help</nav>
<button @click="dark=!dark;localStorage.setItem('ndm-dark',dark)" title="Toggle theme">◐</button><span class="web-label">WEB</span></header>
<div class="toolbar"><button hx-get="/dialog?kind=add" hx-target="#dialog" @click="modal=true">＋ Add URL</button>
<button hx-get="/dialog?kind=spotify" hx-target="#dialog" @click="modal=true">♫ Spotify</button>
<button hx-get="/dialog?kind=grab" hx-target="#dialog" @click="modal=true">♧ Grabber</button><i></i>
<button hx-post="/action" :hx-vals="JSON.stringify({id:selected,action:'resume'})" hx-target="#notice" :disabled="!selected">▷ Resume</button>
<button hx-post="/action" :hx-vals="JSON.stringify({id:selected,action:'pause'})" hx-target="#notice" :disabled="!selected">Ⅱ Stop</button>
<button hx-post="/engine" hx-vals='{"running":"false"}' hx-target="#notice">□ Stop All</button>
<button hx-post="/action" :hx-vals="JSON.stringify({id:selected,action:'remove'})" hx-target="#notice" :disabled="!selected" class="danger">♲ Delete</button>
<button class="options" hx-get="/dialog?kind=settings" hx-target="#dialog" @click="modal=true">⚙ Options</button></div>
<div class="workspace"><aside><div class="side-heading">↧ &nbsp; All Downloads</div><p>⌄ &nbsp; Downloads</p>
''' + ''.join(f'<button :class="{{active:tab===\'{key}\'}}" @click="tab=\'{key}\';selected=\'\'" hx-get="/queue?filter={key}" hx-target="#queue" hx-swap="innerHTML">{icon} &nbsp; {label}</button>' for key, icon, label in [('all','↧','All Downloads'),('unfinished','◴','Unfinished'),('completed','✓','Finished')]) + '''<p>⌄ &nbsp; Categories</p>''' + ''.join(f'<button :class="{{active:tab===\'{key}\'}}" @click="tab=\'{key}\';selected=\'\'" hx-get="/queue?filter={key}" hx-target="#queue">{icon} &nbsp; {label}</button>' for key, icon, label in [('archive','▣','Compressed'),('document','▤','Documents'),('audio','♫','Music'),('video','▹','Video'),('image','▧','Images'),('other','▢','Other')]) + '''<div class="aside-foot"><span class="dot"></span> Native engine<br><small>Same downloads. Any window.</small></div></aside>
<main><div class="list-heading"><h1>Downloads</h1><form id="filters" hx-get="/queue" hx-target="#queue" hx-trigger="input delay:250ms"><input type="hidden" name="filter" :value="tab"><input type="search" name="q" placeholder="⌕  Search downloads…" aria-label="Search downloads"></form></div>
<div id="notice" role="status"></div><section id="queue" hx-get="/queue" hx-trigger="load, every 2s, queueChanged from:body" hx-include="#filters" hx-swap="innerHTML"><div class="empty">Connecting to the native engine…</div></section></main></div>
<div class="scrim" x-show="modal" x-cloak @click.self="modal=false"><section class="dialog" role="dialog" aria-modal="true" aria-label="NDM dialog"><button class="close" @click="modal=false" aria-label="Close">×</button><div id="dialog">Loading…</div></section></div>
</body></html>'''


def queue(state, params):
    tasks = state.get('downloads', [])
    category = params.get('filter', ['all'])[0]
    query = params.get('q', [''])[0].casefold()
    filtered = [t for t in tasks if (category == 'all' or category == t['category'] or category == t['status'] or (category == 'unfinished' and t['status'] != 'completed')) and query in (t['name'] + ' ' + t['url']).casefold()]
    rows = []
    for t in filtered:
        progress = min(100, max(0, t.get('downloaded', 0) * 100 / (t.get('size') or 1)))
        rows.append(f'''<tr @click="selected='{e(t['id'])}'" :class="{{selected:selected==='{e(t['id'])}'}}"><td><span class="file-icon">▤</span><strong>{e(t['name'])}</strong><small class="task-error">{e(t.get('error') or t.get('phase') or '')}</small></td><td>{size(t.get('size'))}</td><td><div class="progress"><span style="width:{progress:.1f}%"></span></div><small>{progress:.1f}%</small></td><td><span class="badge {e(t['status'])}">{e(t['status'])}</span></td><td>{size(t.get('speed'))}/s</td><td>{e(t['category'])}</td></tr>''')
    offers = ''.join(f'<div class="offer"><strong>Browser handoff</strong> {e(o.get("name") or o.get("url"))} ' + button('Download', '/resolve', {'id': o['id'], 'accept': 'true'}, 'primary') + button('Leave in browser', '/resolve', {'id': o['id'], 'accept': 'false'}) + '</div>' for o in state.get('offers', []))
    stats = state.get('stats', {})
    return offers + '<div class="table-wrap"><table><thead><tr><th>File Name</th><th>Size</th><th>Progress</th><th>Status</th><th>Speed</th><th>Category</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>' + ('' if rows else '<div class="empty"><div class="empty-icon">↧</div><h2>No downloads here</h2><p>Add a URL, grab a video, or bring in your Spotify music.</p><button hx-get="/dialog?kind=add" hx-target="#dialog" @click="modal=true" class="primary">＋ Add your first download</button></div>') + f'</div><footer><span>{len(tasks)} files &nbsp; · &nbsp; {stats.get("active", 0)} downloading &nbsp; · &nbsp; {stats.get("queued", 0)} queued</span><span>↓ {size(stats.get("speed"))}/s &nbsp; · &nbsp; {size(stats.get("disk_free"))} free &nbsp; <span class="dot"></span> Native daemon</span></footer>'


def field(label, name, value='', kind='text'):
    return f'<label>{e(label)}<input type="{kind}" name="{e(name)}" value="{e(value)}"></label>'


def select(label, name, values, current):
    return f'<label>{e(label)}<select name="{name}">' + ''.join(f'<option value="{e(v)}" {"selected" if str(v) == str(current) else ""}>{e(str(v).capitalize())}</option>' for v in values) + '</select></label>'


def dialog(kind, state):
    if kind == 'settings':
        s = state['settings']
        tabs = {'Connection': field('Simultaneous downloads', 'max_concurrent', s['max_concurrent'], 'number') + field('Connections per download', 'connections', s['connections'], 'number') + field('Speed limit (bytes/sec, 0 = unlimited)', 'speed_limit', s['speed_limit'], 'number') + select('Engine running', 'engine_running', ['true','false'], str(s['engine_running']).lower()),
                'Proxy': select('Enable proxy', 'proxy_enabled', ['true','false'], str(s['proxy_enabled']).lower()) + field('Proxy URL', 'proxy_url', s['proxy_url']) + field('Username', 'proxy_username', s['proxy_username']) + field('Password', 'proxy_password', s['proxy_password'], 'password'),
                'Save To': field('Download folder (on the server)', 'download_dir', s['download_dir']) + field('Music folder', 'music_dir', s['music_dir']),
                'Capture': select('Browser handoff', 'browser_integration', ['true','false'], str(s['browser_integration']).lower()) + select('Ask before file handoff', 'browser_ask', ['true','false'], str(s['browser_ask']).lower()) + select('Use cookies from (on the server)', 'cookie_browser', COOKIE_BROWSERS, s['cookie_browser']),
                'Spotify': select('Audio format', 'spotify_format', ['mp3','flac','ogg','opus','m4a','wav'], s['spotify_format']) + select('Bitrate', 'spotify_bitrate', ['auto','128k','192k','256k','320k','disable'], s['spotify_bitrate']) + select('Matcher', 'spotify_matcher', ['spotdl','ndm'], s['spotify_matcher'])}
        return '<h2>NDM Options</h2><div x-data="{pane:0}"><nav class="tabs">' + ''.join(f'<button @click="pane={i}" :class="{{active:pane==={i}}}">{label}</button>' for i,label in enumerate(tabs)) + '</nav>' + ''.join(f'<form x-show="pane==={i}" hx-post="/settings" hx-target="#dialog-notice">{body}<div class="form-actions"><button class="primary">Save changes</button></div></form>' for i,body in enumerate(tabs.values())) + '</div><div id="dialog-notice" role="status"></div>'
    title = {'add':'Add download', 'spotify':'Spotify', 'grab':'Media Grabber'}.get(kind, 'Add download')
    route = '/add' if kind == 'add' else '/grab'
    extra = field('Save as (optional)', 'filename') + '<label class="check"><input type="checkbox" name="paused"> Start paused</label>' if kind == 'add' else select('Quality', 'quality', ['best','1080','720','audio'], 'best') if kind == 'grab' else '<p class="muted">Queue a public Spotify track, album, or playlist using your configured spotDL settings.</p>'
    return f'<h2>{title}</h2><form hx-post="{route}" hx-target="#dialog-notice"><label>URL<input type="url" name="url" placeholder="https://…" required autofocus></label>{extra}<div id="dialog-notice" role="status"></div><div class="form-actions"><span class="htmx-indicator">Working… This may take a moment.</span><button class="primary">Start download</button></div></form>'
