import assert from 'node:assert/strict';
import test, {afterEach} from 'node:test';

const globalNames = ['document', 'window', 'navigator', 'fetch', 'customElements'];
const originalGlobals = new Map(globalNames.map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
let scenario = 0;
afterEach(() => {
  for (const [name, descriptor] of originalGlobals) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else delete globalThis[name];
  }
});

async function page({serial = true, loaded = true, metadataAvailable = true} = {}) {
  const elements = new Map();
  const create = () => ({
    value: '', hidden: true, disabled: true, children: [], listeners: {}, attributes: {},
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    addEventListener(event, fn) { this.listeners[event] = fn; },
    setAttribute(key, value) { this.attributes[key] = value; },
    removeAttribute(key) { delete this.attributes[key]; },
  });
  const get = selector => {
    if (!elements.has(selector)) elements.set(selector, create());
    return elements.get(selector);
  };
  let define, reject;
  const defined = loaded ? Promise.resolve() : new Promise((resolve, fail) => { define = resolve; reject = fail; });
  const timers = new Map();
  const catalog = {name: 'Sensor', version: '1.2.3-beta.1', channel: 'beta',
    project_url: 'https://example.com/project', profiles: [{
      id: 'board', label: '<script>not markup</script>', board: 'Exact board', chip_family: 'ESP32',
      provisioning: 'manual', manifest: 'manifests/board.json',
      parts: [{path: 'firmware/board/factory.bin', offset: 0}],
    }]};
  const globals = {
    document: {querySelector: get, createElement: create},
    window: {isSecureContext: true,
      setTimeout: fn => { const id = timers.size + 1; timers.set(id, fn); return id; },
      clearTimeout: id => timers.delete(id),
    },
    navigator: serial ? {serial: {}} : {},
    fetch: async url => { assert.equal(url, 'catalog.json'); return {ok: metadataAvailable, json: async () => catalog}; },
    customElements: {get: () => loaded ? {} : undefined, whenDefined: () => defined},
  };
  for (const [name, value] of Object.entries(globals)) {
    Object.defineProperty(globalThis, name, {configurable: true, writable: true, value});
  }
  await import(`../templates/esp-web-installer/app.mjs?scenario=${++scenario}`);
  const select = id => { get('#profile').value = id; get('#profile').listeners.change(); };
  return {get, select, define, reject, expire: () => [...timers.values()].forEach(fn => fn())};
}

test('exact selection is required; downloads and prerelease label remain explicit', async () => {
  const p = await page();
  assert.equal(p.get('#installer').hidden, true);
  assert.match(p.get('#release').textContent, /BETA — prerelease/);
  assert.equal(p.get('#profile').children[0].textContent, '<script>not markup</script>');
  p.select('board');
  assert.equal(p.get('#installer').hidden, false);
  assert.equal(p.get('#installer').attributes.manifest, 'manifests/board.json');
  assert.equal(p.get('#downloads').children[0].href, 'firmware/board/factory.bin');
  p.select('');
  assert.equal(p.get('#installer').hidden, true);
  assert.equal(p.get('#installer').attributes.manifest, undefined);
});

test('unsupported browsers retain downloads without an install button', async () => {
  const p = await page({serial: false});
  p.select('board');
  assert.equal(p.get('#installer').hidden, true);
  assert.match(p.get('#support').textContent, /desktop browser/);
  assert.ok(p.get('#downloads').children.length);
});

test('failed or delayed module load never shows a dead install button', async () => {
  const p = await page({loaded: false});
  p.select('board');
  assert.equal(p.get('#installer').hidden, true);
  p.expire();
  assert.match(p.get('#support').textContent, /could not load/);
  assert.ok(p.get('#downloads').children.length);
  p.define();
  await Promise.resolve();
  assert.equal(p.get('#installer').hidden, false);
});

test('metadata failure disables installation', async () => {
  const p = await page({metadataAvailable: false});
  assert.equal(p.get('#installer').hidden, true);
  assert.equal(p.get('#profile').disabled, true);
  assert.match(p.get('#support').textContent, /Installer unavailable/);
});

test('module initialization rejection stays visible as an error', async () => {
  const p = await page({loaded: false});
  p.select('board');
  p.reject(new Error('Registration failed'));
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(p.get('#installer').hidden, true);
  assert.match(p.get('#support').textContent, /installer is unavailable/);
});
