// Real browser module; intercept only external HTTP boundaries. No live server or job.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '@playwright/test');
const root = path.resolve(__dirname, '..');
const run = (overrides = {}) => ({
  id: 'run-1', task_id: 'journal-ia-daily-local', task_name: 'Journal-IA daily note',
  task_type: 'action', action: 'journal_ia', status: 'running',
  started_at: '2026-10-24T11:54:23Z', finished_at: null, result: 'Collecting sources',
  error: null, model: '', endpoint_url: '', session_id: '', research_id: '',
  output_target: 'session', ...overrides,
});
const response = (...runs) => ({ runs, has_more: false });
const deferred = () => { let resolve; const promise = new Promise(r => resolve = r); return {promise, resolve}; };

async function harness(t, api) {
  const browser = await chromium.launch({headless: true,
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ? {executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH} : {})});
  t.after(() => browser.close());
  const page = await browser.newPage({viewport: {width: 1280, height: 900}});
  const errors = [], requests = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/harness') return route.fulfill({contentType: 'text/html', body:
      `<html><body><script type="module">import tasks from '/static/js/tasks.js'; window.tasks=tasks;</script></body></html>`});
    if (url.pathname.startsWith('/api/')) {
      requests.push(url.pathname + url.search);
      let result = await api?.(url, route.request());
      if (!result) result = url.pathname === '/api/tasks' ? {json: {tasks: []}}
        : url.pathname === '/api/tasks/onboarding' ? {json: {opened: true}}
        : url.pathname === '/api/tasks/runs/recent' ? {json: response()}
        : {json: {notifications: []}};
      return route.fulfill(result);
    }
    const file = path.resolve(root, '.' + decodeURIComponent(url.pathname));
    if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) return route.fulfill({status: 404, body: 'missing'});
    return route.fulfill({path: file, contentType: file.endsWith('.js') ? 'application/javascript' : undefined});
  });
  await page.goto('http://activity.test/harness');
  await page.waitForFunction(() => window.tasks);
  // Functional tests do not depend on entrance animations/styles.
  await page.addStyleTag({content: '.modal-overlay,.modal-body{display:block!important}.tasks-tab{display:inline-block!important}'});
  const open = async (defaultTab = false) => page.evaluate(def => window.tasks.openTasks(def ? undefined : 'journal-ia-daily-local'), defaultTab);
  const activity = async () => page.locator('[data-tab="activity"]').click();
  const text = () => page.locator('#tasks-activity-list').innerText();
  const recentCount = () => requests.filter(p => p.startsWith('/api/tasks/runs/recent')).length;
  t.after(() => assert.deepEqual(errors, [], 'no unhandled frontend exception'));
  return {page, open, activity, text, recentCount};
}

test('a late Completed response cannot erase Activity rows when searching', async t => {
  const late = deferred();
  const h = await harness(t, async url => url.pathname.endsWith('/runs/recent')
    ? url.searchParams.get('max_result_chars') === '10000'
      ? (await late.promise, {json: response()}) : {json: response(run())} : null);
  await h.open();
  await h.page.locator('[data-tab="completed"]').click();
  await h.activity();
  await h.page.waitForFunction(() => document.querySelector('.task-log-row'));
  late.resolve();
  await h.page.waitForResponse(r => r.url().includes('max_result_chars=10000'));
  await h.page.locator('#tasks-activity-search').fill('Journal-IA');
  assert.match(await h.text(), /Journal-IA daily note/);
});

test('a late empty default Completed response cannot navigate away from Activity', async t => {
  const late = deferred();
  const h = await harness(t, async url => url.pathname.endsWith('/runs/recent')
    ? url.searchParams.get('max_result_chars') === '10000'
      ? (await late.promise, {json: response()}) : {json: response(run())} : null);
  await h.open(true);
  await h.activity();
  await h.page.waitForFunction(() => document.querySelector('.task-log-row'));
  const arrived = h.page.waitForResponse(r => r.url().includes('max_result_chars=10000'));
  late.resolve();
  await arrived;
  assert.equal(await h.page.locator('[data-tab="activity"]').getAttribute('aria-selected'), 'true');
});

test('empty Activity refreshes into running then partial without losing its search', async t => {
  let calls = 0;
  const h = await harness(t, url => url.pathname.endsWith('/runs/recent')
    ? {json: ++calls === 1 ? response() : response(run(calls === 2 ? {} : {
      status: 'partial', finished_at: '2026-10-24T12:00:00Z', result: 'One segment completed, next segment failed'}))} : null);
  await h.page.clock.install({time: new Date('2026-10-24T11:54:24Z')});
  await h.open(); await h.activity();
  await h.page.waitForFunction(() => document.querySelector('#tasks-activity-list').textContent.includes('No activity'));
  await h.page.locator('#tasks-activity-search').fill('Journal-IA');
  await h.page.clock.fastForward(6000);
  await h.page.waitForFunction(() => document.querySelector('.task-log-row'));
  assert.match(await h.text(), /Journal-IA daily note/);
  assert.match(await h.text(), /Running/);
  await h.page.locator('.task-log-row').click();
  assert.match(await h.page.locator('.task-log-row').getAttribute('class'), /expanded/);
  await h.page.clock.fastForward(6000);
  await h.page.waitForFunction(() => document.querySelector('.task-log-partial-tag'));
  assert.match(await h.text(), /Partially completed/);
  assert.match(await h.page.locator('.task-log-row').getAttribute('class'), /expanded/);
  assert.equal(await h.page.locator('#tasks-activity-search').inputValue(), 'Journal-IA');
});

test('a stale Activity request cannot overwrite a newer manual refresh', async t => {
  const old = deferred(); let calls = 0;
  const h = await harness(t, async url => url.pathname.endsWith('/runs/recent')
    ? ++calls === 1 ? (await old.promise, {json: response()}) : {json: response(run())} : null);
  await h.open(); await h.activity();
  await h.page.locator('#tasks-activity-refresh').click();
  await h.page.waitForFunction(() => document.querySelector('.task-log-row'));
  const arrived = h.page.waitForResponse(r => r.url().includes('/runs/recent'));
  old.resolve(); await arrived;
  assert.match(await h.text(), /Journal-IA daily note/);
});

test('an empty filtered page still offers pagination', async t => {
  const h = await harness(t, url => url.pathname.endsWith('/runs/recent')
    ? {json: Number(url.searchParams.get('limit')) > 40 ? response(run())
      : {runs: [run({output_target: 'notification'})], has_more: true}} : null);
  await h.open(); await h.activity();
  await h.page.waitForFunction(() => document.querySelector('#tasks-activity-list').textContent.includes('No matching'));
  assert.equal(await h.page.locator('#tasks-activity-load-more').count(), 1);
  await h.page.locator('#tasks-activity-load-more').click();
  await h.page.waitForFunction(() => document.querySelector('.task-log-row'));
  assert.match(await h.text(), /Journal-IA daily note/);
});

test('refresh stops on tab exit and modal close', async t => {
  const h = await harness(t, url => url.pathname.endsWith('/runs/recent') ? {json: response(run())} : null);
  await h.page.clock.install();
  await h.open(); await h.activity();
  await h.page.waitForFunction(() => document.querySelector('.task-log-row'));
  await h.page.locator('[data-tab="tasks"]').click();
  let before = h.recentCount(); await h.page.clock.fastForward(16000);
  assert.equal(h.recentCount(), before);
  await h.activity(); await h.page.waitForFunction(() => document.querySelector('.task-log-row'));
  await h.page.evaluate(() => window.tasks.closeTasks());
  before = h.recentCount(); await h.page.clock.fastForward(16000);
  assert.equal(h.recentCount(), before);
});

test('HTTP auth failure is displayed as an error instead of an empty history', async t => {
  const h = await harness(t, url => url.pathname.endsWith('/runs/recent') ? {status: 401, json: {detail: 'Not authenticated'}} : null);
  await h.open(); await h.activity();
  await h.page.waitForFunction(() => document.querySelector('#tasks-activity-list').textContent.includes('HTTP 401'));
  assert.match(await h.text(), /Failed to load activity.*HTTP 401/);
});

test('notification filter and search survive automatic refresh', async t => {
  let calls = 0;
  const h = await harness(t, url => url.pathname.endsWith('/runs/recent') ? {json: response(
    run(), run({id: 'notification', task_name: 'Notification task', output_target: 'notification',
      result: ++calls > 1 ? 'New progress' : 'Initial progress'}))} : null);
  await h.page.clock.install();
  await h.open(); await h.activity();
  await h.page.waitForFunction(() => document.querySelector('.task-log-row'));
  assert.doesNotMatch(await h.text(), /Notification task/);
  await h.page.locator('[data-key="notifications"]').click();
  await h.page.locator('#tasks-activity-search').fill('Notification');
  assert.match(await h.text(), /Notification task/);
  await h.page.clock.fastForward(6000);
  await h.page.waitForFunction(() => document.querySelector('#tasks-activity-list').textContent.includes('New progress'));
  assert.match(await h.page.locator('[data-key="notifications"]').getAttribute('class'), /active/);
  assert.equal(await h.page.locator('#tasks-activity-search').inputValue(), 'Notification');
});

for (const failure of [{status: 500, json: {detail: 'Database unavailable'}},
  {status: 200, contentType: 'application/json', body: 'invalid JSON'}]) {
  test(`Activity exposes HTTP/JSON errors (${failure.status}, ${failure.body || 'backend'})`, async t => {
    const h = await harness(t, url => url.pathname.endsWith('/runs/recent') ? failure : null);
    await h.open(); await h.activity();
    await h.page.waitForFunction(() => document.querySelector('#tasks-activity-list').textContent.includes('Failed to load activity'));
    assert.doesNotMatch(await h.text(), /No activity yet/);
  });
}

test('a hidden page pauses Activity reads and resumes when visible', async t => {
  let calls = 0;
  const h = await harness(t, url => url.pathname.endsWith('/runs/recent')
    ? {json: response(run({result: ++calls > 1 ? 'Updated after visibility' : 'Initial progress'}))} : null);
  await h.page.clock.install();
  await h.open(); await h.activity();
  await h.page.waitForFunction(() => document.querySelector('.task-log-row'));
  await h.page.evaluate(() => Object.defineProperty(document, 'hidden', {configurable: true, value: true}));
  const before = h.recentCount();
  await h.page.clock.fastForward(20000);
  assert.equal(h.recentCount(), before);
  await h.page.evaluate(() => Object.defineProperty(document, 'hidden', {configurable: true, value: false}));
  await h.page.clock.fastForward(6000);
  await h.page.waitForFunction(() => document.querySelector('#tasks-activity-list').textContent.includes('Updated after visibility'));
});
