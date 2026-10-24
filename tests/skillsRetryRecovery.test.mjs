import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../static/js/skills.js', import.meta.url), 'utf8');

function fixture() {
  class Element {
    constructor() { this.children = []; this._html = ''; this.textContent = ''; this.dataset = {}; }
    set innerHTML(value) { this._html = value; this.children = []; }
    get innerHTML() { return this._html; }
    appendChild(child) { this.children.push(child); }
    querySelector(selector) {
      if (selector === '[data-act="retry"]') return this.children.find(c => c.dataset.act === 'retry') || null;
      return this.children.find(c => c.className?.includes('skill-test-status-error')) || null;
    }
    addEventListener() {}
    remove() {}
  }
  const log = new Element(), verdict = new Element(), preview = new Element();
  preview.querySelector = selector => selector === '.skill-test-log' ? log : verdict;
  const card = { classList: { contains: () => true }, querySelector: () => preview };
  const requests = [], responses = [];
  let poll = null;
  const context = vm.createContext({
    API: '', window: {}, document: { createElement: () => new Element(), body: { contains: () => true } },
    uiModule: { styledPrompt: async () => 'Create and verify a harmless fixture.' },
    _setCardRunning() {}, _renderTestVerdict: (element, v) => { element.textContent = v.verdict; },
    setInterval: fn => { poll = fn; return 1; }, clearInterval: () => { poll = null; },
    fetch: async (url, options) => { requests.push({ url, options }); return responses.shift(); },
  });
  vm.runInContext(source.slice(source.indexOf('async function _fetchTestStatus('),
    source.indexOf('// Show/hide the app-wide whirlpool spinner')) + '\nglobalThis.run = _testSkill;', context);
  return { context, card, log, verdict, requests, responses, poll: () => poll };
}
const ok = value => ({ ok: true, json: async () => value });

test('Retry sends a concrete task; transient polling errors retain evidence and recover', async () => {
  const f = fixture();
  f.responses.push(ok({}), ok({ status: 'running', log: [{ type: 'skill_test_start', task: 'fixture', model: 'model' }] }));
  await f.context.run(f.card, 'demo', true);
  assert.equal(JSON.parse(f.requests[0].options.body).task, 'Create and verify a harmless fixture.');
  f.responses.push({ ok: false, status: 503 });
  await f.poll()();
  assert(f.log.children.some(c => c.textContent === 'Task: fixture'));
  assert(f.log.children.some(c => c.textContent.includes('HTTP 503')));
  assert(f.poll());
  f.responses.push(ok({ status: 'done', log: [{ type: 'say', text: 'verified' }], verdict: { verdict: 'pass' } }));
  await f.poll()();
  assert.equal(f.log.children[0].textContent, 'verified');
  assert.equal(f.verdict.textContent, 'pass');
  assert.equal(f.poll(), null);
});

test('server restart produces an explanation and Retry, preserving the last log', async () => {
  const f = fixture();
  f.responses.push(ok({}), ok({ status: 'running', log: [{ type: 'say', text: 'evidence' }] }));
  await f.context.run(f.card, 'demo', true);
  f.responses.push(ok({ status: 'none' }));
  await f.poll()();
  assert(f.log.children.some(c => c.textContent === 'evidence'));
  assert(f.log.children.some(c => c.textContent.includes('restart')));
  assert(f.verdict.children.some(c => c.textContent === 'Retry'));
  assert.equal(f.poll(), null);
});

test('an unavailable status must not automatically start a duplicate test', async () => {
  const f = fixture();
  f.responses.push({ ok: false, status: 503 });
  await f.context.run(f.card, 'demo');
  assert.equal(f.requests.length, 1);
  assert(!f.requests.some(r => r.options?.method === 'POST'));
  assert(f.poll());
});

test('cancelling the task prompt starts no model work', async () => {
  const f = fixture();
  f.context.uiModule.styledPrompt = async () => null;
  await f.context.run(f.card, 'demo', true);
  assert.equal(f.requests.length, 0);
  assert(f.log.textContent.includes('not started'));
});
