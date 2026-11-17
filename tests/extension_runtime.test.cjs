/* Execute the shipped extension scripts against browser-event doubles.
 * Run with: node --test tests/extension_runtime.test.cjs
 * No browser, network, npm package, or NDM daemon is needed.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..');
const VIDEO = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ';
const TRACK = 'https://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl';

function event() {
  const listeners = [];
  return {
    addListener(fn) { listeners.push(fn); },
    async fire(...args) { return Promise.all(listeners.map(fn => fn(...args))); },
    listeners,
  };
}

function harness(browser, stored = {}) {
  const storage = { ...stored };
  const calls = [], cancellations = [], erasures = [], notifications = [], menus = [];
  let nativeReply = { ok: true, status: 'pending', id: 'offer-1' };
  let nativeError = null;
  const tabs = [{ url: VIDEO }];
  const api = {
    storage: {
      local: {
        async get(defaults) { return { ...defaults, ...storage }; },
        async set(values) { Object.assign(storage, values); },
      },
      onChanged: event(),
    },
    downloads: {
      onCreated: event(),
      async cancel(id) { cancellations.push(id); calls.push('cancel'); },
      async erase(query) { erasures.push(query); calls.push('erase'); },
    },
    runtime: {
      sendNativeMessage: async (host, message) => {
        calls.push('native');
        assert.equal(host, 'ndm');
        if (nativeError) throw nativeError;
        return nativeReply;
      },
      getURL: name => `${browser}-extension://ndm/${name}`,
      onInstalled: event(),
      onMessage: event(),
    },
    notifications: {
      create: async options => { notifications.push(options); return 'notification'; },
    },
    tabs: { query: async () => tabs },
    contextMenus: {
      onClicked: event(),
      create: options => { menus.push(options); },
      removeAll: callback => { menus.length = 0; callback(); },
    },
  };
  api.menus = api.contextMenus;
  const globals = { URL, Date, Number, String, Object, Math, Promise, Error, JSON, Set, Map };
  // Firefox scripts use `browser`; Chromium scripts use `chrome`.
  globals.browser = api;
  globals.chrome = api;
  const context = vm.createContext({
    ...globals,
    importScripts: name => vm.runInContext(
      fs.readFileSync(path.join(ROOT, 'extension', browser, name), 'utf8'), context,
      { filename: name }),
  });
  const load = name => vm.runInContext(
    fs.readFileSync(path.join(ROOT, 'extension', browser, name), 'utf8'), context,
    { filename: name });
  if (browser === 'firefox') load('core.js');
  load('background.js');
  return {
    context, load, storage, calls, cancellations, erasures, notifications, menus, tabs,
    api,
    reply(value) { nativeReply = value; },
    fail(error) { nativeError = error; },
    evaluate(code) { return vm.runInContext(code, context); },
    async message(value) {
      let result;
      const keepOpen = api.runtime.onMessage.listeners[0](value, {}, value => { result = value; });
      if (keepOpen === true) {
        // Drain the worker's promise chain, including native messaging/storage.
        for (let i = 0; i < 12; i++) await Promise.resolve();
        return result;
      }
      return keepOpen;
    },
  };
}

for (const browser of ['firefox', 'chrome']) {
  test(`${browser}: accepted download is cancelled only after native acceptance`, async () => {
    const h = harness(browser);
    await h.api.downloads.onCreated.fire({
      id: 7, url: 'https://example.org/movie.mp4', filename: '/tmp/movie.mp4',
      mime: 'video/mp4', totalBytes: 4096,
    });
    assert.deepEqual(h.calls, ['native', 'cancel', 'erase']);
    assert.deepEqual(h.cancellations, [7]);
    assert.equal(h.erasures.length, 1);
    assert.equal(h.erasures[0].id, 7);
    assert.match(h.notifications.at(-1).title, /Waiting in NDM/);
  });

  test(`${browser}: closed host leaves the browser transfer untouched`, async () => {
    const h = harness(browser);
    h.fail(new Error('native host exited'));
    await h.api.downloads.onCreated.fire({
      id: 8, url: 'https://example.org/safe.zip', filename: 'safe.zip', totalBytes: 9000,
    });
    assert.deepEqual(h.cancellations, []);
    assert.deepEqual(h.erasures, []);
    assert.equal(h.notifications.at(-1).title, 'NDM is not reachable');
  });

  test(`${browser}: disabled integration is a refusal, not a cancellation`, async () => {
    const h = harness(browser);
    h.reply({ ok: false, error: 'Browser integration is off.' });
    await h.api.downloads.onCreated.fire({
      id: 9, url: 'https://example.org/keep.pdf', filename: 'keep.pdf', totalBytes: 9000,
    });
    assert.deepEqual(h.cancellations, []);
    assert.match(h.notifications.at(-1).message, /integration is off/);
  });

  test(`${browser}: popup grabs the active YouTube tab over browser.grab`, async () => {
    const h = harness(browser);
    const reply = await h.message({ type: 'grab' });
    assert.equal(reply.ok, true);
    const message = h.evaluate('ndmPageKind(' + JSON.stringify(VIDEO) + ')');
    assert.equal(message, 'youtube');
    assert.deepEqual(h.cancellations, []);
    assert.match(h.notifications.at(-1).title, /YouTube link sent/);
  });

  test(`${browser}: Spotify page grab reports the real daemon error`, async () => {
    const h = harness(browser);
    h.tabs[0].url = TRACK;
    h.reply({ ok: false, error: 'Too many downloads are waiting for approval.' });
    const reply = await h.message({ type: 'grab' });
    assert.equal(reply.ok, false);
    assert.match(reply.error, /Too many downloads/);
    assert.deepEqual(h.cancellations, []);
  });

  test(`${browser}: status uses an async native ping`, async () => {
    const h = harness(browser);
    h.reply({ ok: true, running: true, enabled: true, ask: true });
    const reply = await h.message({ type: 'status' });
    assert.equal(reply.running, true);
    assert.equal(reply.enabled, true);
  });

  test(`${browser}: unsupported active pages are not sent`, async () => {
    const h = harness(browser);
    h.tabs[0].url = 'https://example.org/watch?v=123';
    const reply = await h.message({ type: 'grab' });
    assert.equal(reply.ok, false);
    assert.deepEqual(h.calls, []);
  });
}

test('Chrome reads changed settings after a service-worker restart', async () => {
  let h = harness('chrome');
  await h.api.runtime.onInstalled.fire({ reason: 'install' });
  assert.deepEqual(h.menus.map(m => m.id), ['ndm-link', 'ndm-page']);
  h.storage.enabled = false;
  // A new context models MV3 teardown; only storage survives.
  h = harness('chrome', h.storage);
  await h.api.downloads.onCreated.fire({
    id: 10, url: 'https://example.org/a.mp4', filename: 'a.mp4', totalBytes: 9000,
  });
  assert.deepEqual(h.calls, []);
  h.storage.enabled = true;
  h.storage.minSizeKB = 100;
  await h.api.downloads.onCreated.fire({
    id: 11, url: 'https://example.org/small.mp4', filename: 'small.mp4', totalBytes: 9000,
  });
  assert.deepEqual(h.calls, []);
});

test('Chrome reinstall/update does not duplicate context menu ids', async () => {
  const h = harness('chrome');
  await h.api.runtime.onInstalled.fire({ reason: 'install' });
  await h.api.runtime.onInstalled.fire({ reason: 'update' });
  assert.deepEqual(h.menus.map(m => m.id), ['ndm-link', 'ndm-page']);
});

test('context-menu media link routes to grab without cancelling a synthetic id', async () => {
  const h = harness('chrome');
  await h.api.contextMenus.onClicked.fire({ menuItemId: 'ndm-link', linkUrl: TRACK }, { url: TRACK });
  assert.deepEqual(h.cancellations, []);
  assert.deepEqual(h.erasures, []);
  assert.match(h.notifications.at(-1).title, /Spotify link sent/);
});

test('page classifier agrees with the Python regression matrix', () => {
  const cases = JSON.parse(fs.readFileSync(path.join(__dirname, 'extension_page_cases.json'), 'utf8'));
  const h = harness('firefox');
  for (const [url, expected] of cases) {
    assert.equal(h.evaluate(`ndmPageKind(${JSON.stringify(url)})`), expected, url);
  }
});

test('filters keep blob/data, extension downloads and disabled types in the browser', () => {
  const h = harness('firefox');
  const settings = { enabled: true, types: 'video', minSizeKB: 0 };
  for (const item of [
    { url: 'blob:https://example.org/id', filename: 'a.mp4' },
    { url: 'data:video/mp4;base64,AA', filename: 'a.mp4' },
    { url: 'https://example.org/a.mp4', byExtensionID: 'other' },
    { url: 'https://example.org/a.mp3', filename: 'a.mp3' },
  ]) assert.equal(h.evaluate(`ndmWanted(${JSON.stringify(item)}, ${JSON.stringify(settings)})`), false);
});
