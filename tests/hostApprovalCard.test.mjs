import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../static/js/chatRenderer.js', import.meta.url), 'utf8');
const start = source.indexOf('export function renderAskUserCard(');
const end = source.indexOf('\n/**', start);

function fixture() {
  class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.style = {}; this.attributes = {}; this.listeners = {}; }
    appendChild(child) { this.children.push(child); }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    click() { this.listeners.click?.(); }
    classList = { contains: () => false, add: () => {} };
  }
  const root = new Element('root');
  const context = vm.createContext({
    document: { createElement: tag => new Element(tag) },
    removeAskUserCards() {}, _markAskUserAnswered() {},
    svgifyEmoji: value => value, uiModule: { esc: value => value },
  });
  vm.runInContext(source.slice(start, end).replace('export function', 'function') + '\nglobalThis.render = renderAskUserCard;', context);
  return { root, render: context.render };
}

test('host approval visibly explains absent isolation before the exact action and choices', () => {
  const f = fixture();
  const warning = 'This command can modify files outside the workspace and start child processes.';
  const command = 'printf hello\nprintf complete-input';
  let submission;
  const card = f.render({kind: 'tool_approval', approval_id: 'opaque-id',
    question: 'Run this exact command without isolation?', description: warning,
    action: {tool: 'bash', content: command, workspace: 'C:/fixture', execution_profile: 'host_once'},
    options: [{label: 'Allow this command once without isolation', value: 'approve_task'},
      {label: 'Allow commands in this chat without isolation', value: 'approve_host_chat'}, {label: 'Deny', value: 'deny'}],
  }, {root: f.root, scroll: false, focus: false, onSubmit: value => { submission = value; }});
  const note = card.children.find(c => c.attributes.role === 'note');
  const action = card.children.find(c => c.textContent?.includes(command));
  const choices = card.children.find(c => c.className === 'ask-user-options');
  assert.equal(note.textContent, warning);
  assert(card.children.indexOf(note) < card.children.indexOf(action));
  assert(card.children.indexOf(action) < card.children.indexOf(choices));
  assert.equal(choices.children.length, 3);
  choices.children[0].click();
  assert.equal(submission.approval_id, 'opaque-id');
  assert.equal(submission.decision, 'approve_task');
  choices.children[1].click();
  assert.equal(submission.decision, 'approve_host_chat');
  choices.children[2].click();
  assert.equal(submission.decision, 'deny');
});

test('a resolved host approval is never rendered as another actionable card', () => {
  const f = fixture();
  assert.equal(f.render({resolved: 'approve_task'}, {root: f.root}), null);
  assert.equal(f.root.children.length, 0);
});
