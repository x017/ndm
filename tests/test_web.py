"""The web frontend must remain an authenticated, CSRF-protected IPC adapter."""
import json
import threading
from http.client import HTTPConnection

import pytest

from ndm.models import Settings
from ndm.web import WebServer
from ndm.web.views import dialog, queue


@pytest.fixture
def web(tmp_path):
    server = WebServer(('127.0.0.1', 0), tmp_path, token='test-secret')
    calls = []
    state = {'downloads': [], 'offers': [], 'stats': {}, 'settings': Settings().model_dump()}

    def call(operation, payload=None):
        calls.append((operation, payload))
        if operation == 'spotify.search':
            return {'items': [{'song': {'name': 'Example'}}]}
        return state

    server.call = call
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, calls
    server.shutdown()
    server.server_close()
    thread.join()


def fetch(server, path='/', method='GET', body=None, auth=True, csrf=True, **extra):
    conn = HTTPConnection('127.0.0.1', server.server_port)
    headers = {'Cookie': 'ndm_session=test-secret'} if auth else {}
    if csrf:
        headers['X-NDM-CSRF'] = server.csrf
    headers.update(extra)
    conn.request(method, path, body, headers)
    response = conn.getresponse()
    result = response.status, dict(response.getheaders()), response.read().decode()
    conn.close()
    return result


def test_private_login(web):
    server, _ = web
    assert 'name="password"' in fetch(server, auth=False)[2]
    assert fetch(server, '/?token=test-secret', auth=False)[0] == 200
    assert fetch(server, '/login', 'POST', 'username=admin&password=wrong', auth=False)[0] == 401
    from urllib.parse import urlencode
    status, headers, _ = fetch(server, '/login', 'POST', urlencode({'username': server.username, 'password': server.password}), auth=False)
    assert status == 303
    assert headers['Location'] == '/'
    assert 'HttpOnly' in headers['Set-Cookie']
    assert 'SameSite=Strict' in headers['Set-Cookie']


def test_page_and_local_assets(web):
    server, calls = web
    status, _, html = fetch(server)
    assert status == 200
    assert 'htmx.min.js' in html and 'alpine.min.js' in html
    assert server.csrf in html
    for asset in ('htmx.min.js', 'alpine.min.js', 'app.css'):
        assert fetch(server, '/static/' + asset)[0] == 200
    assert fetch(server, '/static/../../ipc.py')[0] == 404
    assert fetch(server, '/queue')[0] == 200
    assert calls[-1][0] == 'state'


def test_writes_require_session_csrf_and_same_origin(web):
    server, calls = web
    assert fetch(server, '/add', 'POST', 'url=https://example.com', auth=False)[0] == 403
    assert fetch(server, '/add', 'POST', 'url=https://example.com', csrf=False)[0] == 403
    assert fetch(server, '/add', 'POST', 'url=https://example.com', Origin='https://evil.example')[0] == 403
    assert not calls


def test_add_action_and_settings(web):
    server, calls = web
    status, headers, _ = fetch(server, '/add', 'POST', 'url=https://example.com/a&paused=on')
    assert status == 200
    assert json.loads(headers['HX-Trigger'])['queueChanged']
    assert calls[-1] == ('add', {'url': 'https://example.com/a', 'filename': None, 'start_paused': True})
    fetch(server, '/action', 'POST', 'id=abc&action=pause')
    assert calls[-1] == ('action', {'id': 'abc', 'action': 'pause'})
    fetch(server, '/settings', 'POST', 'cookie_browser=chrome&connections=8&browser_integration=true')
    assert calls[-1] == ('settings', {'cookie_browser': 'chrome', 'connections': 8, 'browser_integration': True})


def test_spotify_uses_song_payloads(web):
    server, calls = web
    fetch(server, '/grab', 'POST', 'url=https://open.spotify.com/track/1234567890123456789012')
    assert calls[-1] == ('spotify.queue', {'songs': [{'name': 'Example'}]})


def test_rendering_escapes_names_and_filters():
    task = {'id': 'abc', 'name': '<script>alert(1)</script>', 'url': 'https://example.com', 'category': 'audio', 'status': 'paused', 'size': 100, 'downloaded': 50}
    state = {'downloads': [task]}
    html = queue(state, {})
    assert '<script>' not in html
    assert '&lt;script&gt;' in html
    assert '50.0%' in html
    assert '&lt;script&gt;' not in queue(state, {'filter': ['video']})
    assert '&lt;script&gt;' not in queue(state, {'q': ['missing']})


def test_settings_and_dialog_components():
    state = {'settings': Settings().model_dump()}
    html = dialog('settings', state)
    for label in ('Connection', 'Proxy', 'Save To', 'Capture', 'Spotify', 'cookie_browser'):
        assert label in html
    assert 'hx-post="/add"' in dialog('add', state)
    assert 'hx-post="/grab"' in dialog('grab', state)
