import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../static/js/hostExecution.js', import.meta.url), 'utf8');
function fixture() {
  const children = [], listeners = {}, responses = [], requests = [];
  let sid = 'chat-a';
  const context = vm.createContext({
    document: {
      createElement: () => ({style: {}, attributes: {}, listeners: {}, setAttribute(k,v) {this.attributes[k]=v;}, addEventListener(k,v) {this.listeners[k]=v;}}),
      getElementById: () => ({parentElement: {appendChild: node => children.push(node)}}),
    },
    window: {addEventListener: (k,v) => {listeners[k]=v;}},
  });
  vm.runInContext(source.replace('export function', 'function') + '\nglobalThis.create = createHostExecutionIndicator;', context);
  const indicator = context.create({getSessionId: () => sid, fetchImpl: async (url, options) => {
    requests.push({url, options});
    const value = responses.shift();
    if (value instanceof Error) throw value;
    return typeof value === 'function' ? value() : {ok: true, json: async () => value};
  }});
  return {indicator, button: children[0], responses, requests, listeners, setSession: value => {sid=value;}};
}

test('active chat consent is visible and the button revokes it on the server', async () => {
  const f = fixture();
  f.responses.push({enabled: true});
  await f.indicator.refresh();
  assert.equal(f.button.style.display, 'inline-flex');
  assert.match(f.button.textContent, /Without isolation/);
  assert.match(f.button.title, /outside the workspace/);
  f.responses.push({enabled: false}, {enabled: false});
  await f.button.listeners.click();
  assert.equal(f.requests[1].options.method, 'DELETE');
  assert.match(f.requests[1].url, /chat-a\/host-execution$/);
  assert.equal(f.button.style.display, 'none');
});

test('a late response from another chat cannot light the current indicator', async () => {
  const f = fixture();
  let resolve;
  f.responses.push(() => new Promise(r => {resolve=r;}));
  const previous = f.indicator.refresh();
  f.setSession('chat-b');
  f.responses.push({enabled: false});
  await f.indicator.refresh();
  resolve({ok: true, json: async () => ({enabled: true})});
  await previous;
  assert.equal(f.button.style.display, 'none');
});

test('workspace changes query the scoped permission, including clearing a workspace', async () => {
  const f = fixture();
  f.responses.push({enabled: false});
  await f.listeners['odysseus:workspace-changed']({detail: {path: ''}});
  assert.match(f.requests[0].url, /host-execution\?workspace=$/);
});

test('status and revocation errors remain visible and recover on retry', async () => {
  const f = fixture();
  f.responses.push(new Error('offline'));
  await f.indicator.refresh();
  assert.equal(f.button.style.display, 'inline-flex');
  assert.match(f.button.textContent, /unavailable/);
  f.responses.push(new Error('offline'));
  await f.button.listeners.click();
  assert.match(f.button.textContent, /Revocation failed/);
  f.responses.push({enabled: true});
  await f.indicator.refresh();
  assert.equal(f.button.textContent, 'Without isolation · Revoke');
});
