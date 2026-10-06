// Run the shipped inline scripts with a small DOM fixture; no npm dependencies.
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');

class Classes {
  constructor(stats) { this.values = new Set(); this.stats = stats; }
  add(value) { this.stats.classOps++; this.values.add(value); }
  remove(value) { this.stats.classOps++; this.values.delete(value); }
  contains(value) { return this.values.has(value); }
  toggle(value, force = !this.contains(value)) {
    this.stats.classOps++;
    if (force) this.add(value); else this.remove(value);
    return force;
  }
}

function fixture(page = 'player', suppliedData) {
  const html = readFileSync(join(__dirname, '../src/podcast_practice/assets', `${page}.html`), 'utf8');
  const listeners = new Map(), elements = new Map(), timers = new Map(), frames = new Map();
  const stats = {classOps: 0, textWrites: 0, attributeWrites: 0, geometryReads: 0, scrolls: 0};
  let timerId = 0, frameId = 0, plays = 0;
  class Element {
    constructor(tagName = 'div') {
      this.tagName = tagName.toUpperCase();
      this.classList = new Classes(stats);
      this.style = {}; this.dataset = {}; this.attributes = {}; this.children = [];
      this.events = new Map(); this.checked = false; this.disabled = false;
      this.hidden = false; this.open = false; this.isContentEditable = false;
      this.selectedIndex = 0; this._value = ''; this._text = '';
    }
    set className(value) { this.classList.values = new Set(value.split(' ')); }
    get className() { return [...this.classList.values].join(' '); }
    set textContent(value) { stats.textWrites++; this._text = String(value); this.children = []; }
    get textContent() { return this._text + this.children.map(c => c.textContent ?? c).join(''); }
    set value(value) {
      if (this.options) this.selectedIndex = this.options.findIndex(o => o.value === String(value));
      else this._value = String(value);
    }
    get value() { return this.options ? this.options[this.selectedIndex]?.value : this._value; }
    append(...children) { for (const child of children) { if (typeof child !== 'string') child.parent = this; this.children.push(child); } }
    setAttribute(key, value) { stats.attributeWrites++; this.attributes[key] = value; }
    addEventListener(key, fn) { const list = this.events.get(key) || []; list.push(fn); this.events.set(key, list); }
    dispatchEvent(event) { event.target ??= this; for (const fn of this.events.get(event.type) || []) fn(event); }
    click() {
      if (this.disabled) return;
      if (this.type === 'checkbox') this.checked = !this.checked;
      this.dispatchEvent({type: 'click'});
      if (this.type === 'checkbox') this.dispatchEvent({type: 'change'});
    }
    closest(selector) {
      for (let el = this; el; el = el.parent) {
        for (const part of selector.split(',')) {
          if (part === '#episodes a' && el.tagName === 'A' && el.parent?.id === 'episodes') return el;
          if (part === '[role="textbox"]' && el.attributes.role === 'textbox') return el;
          if (part === '[role="button"]:not(.sentence)' && el.attributes.role === 'button' && !el.classList.contains('sentence')) return el;
          if (part.toUpperCase() === el.tagName) return el;
        }
      }
      return null;
    }
    focus() { document.activeElement = this; }
    blur() { document.activeElement = document.body; }
    select() { this.selection = [0, this.value.length]; }
    showModal() { this.open = true; }
    close() { this.open = false; }
    scrollIntoView() { stats.scrolls++; }
    getBoundingClientRect() { stats.geometryReads++; return this.rect || {top: 300, bottom: 400}; }
  }
  for (const match of html.matchAll(/<(\w+)[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const el = new Element(match[1]); el.id = match[2];
    if (match[0].includes('type="checkbox"')) el.type = 'checkbox';
    el.hidden = /\bhidden\b/.test(match[0]); el.checked = /\bchecked\b/.test(match[0]);
    elements.set(el.id, el);
  }
  const document = {
    body: new Element('body'), documentElement: new Element('html'),
    getElementById: id => elements.get(id),
    createElement: tag => new Element(tag),
    addEventListener: (key, fn) => { const list = listeners.get(key) || []; list.push(fn); listeners.set(key, list); },
    querySelector: selector => selector === 'dialog[open]' ? [...elements.values()].find(el => el.tagName === 'DIALOG' && el.open) : null,
    querySelectorAll: selector => selector === '#episodes a' ? elements.get('episodes').children : [],
  };
  document.activeElement = document.body;
  const data = suppliedData || {
    title: 'Keyboard fixture', duration: 20, review: {warnings: []}, chapters: [{title: 'Start', sentence: 0}],
    sentences: [1, 6, 12].map((start, id) => ({id, start, end: start + 2, speaker: 'Host', text: `Sentence ${id}`, words: [{text: `word${id}`, start, end: start + 1}]})),
  };
  if (page === 'player') {
    elements.get('alignment-data').textContent = JSON.stringify(data);
    const audio = elements.get('audio'); audio.paused = true; audio.currentTime = 0;
    audio.play = async () => { plays++; audio.paused = false; audio.dispatchEvent({type: 'play'}); };
    audio.pause = () => { const playing = !audio.paused; audio.paused = true; if (playing) audio.dispatchEvent({type: 'pause'}); };
    elements.get('speed').options = ['0.6', '0.75', '0.85', '1', '1.1', '1.25'].map(value => ({value, textContent: `${value}×`}));
    elements.get('speed').value = '1'; elements.get('gap').value = '1';
  } else {
    elements.get('library-config').textContent = JSON.stringify({token: null});
    for (let i = 0; i < 3; i++) { const link = new Element('a'); link.id = `episode-${i}`; elements.get('episodes').append(link); }
  }
  const context = {
    document, window: {innerHeight: 1000}, location: {protocol: 'http:'},
    Event: class { constructor(type) { this.type = type; } },
    requestAnimationFrame: fn => { frames.set(++frameId, fn); return frameId; },
    cancelAnimationFrame: id => frames.delete(id),
    setTimeout: fn => { timers.set(++timerId, fn); return timerId; },
    clearTimeout: id => timers.delete(id),
  };
  vm.runInNewContext(html.match(/<script>\s*([\s\S]*?)<\/script>/)[1], context);
  function key(key, options = {}) {
    const event = {key, code: key === ' ' ? 'Space' : key, target: document.activeElement, preventDefault() { this.defaultPrevented = true; }, ...options};
    event.target.dispatchEvent({...event, type: 'keydown'});
    for (const fn of listeners.get('keydown') || []) fn(event);
    return event;
  }
  function runFrame() { const pending = [...frames.values()]; frames.clear(); pending.forEach(fn => fn()); }
  return {key, elements, document, data, timers, frames, stats, runFrame, get plays() { return plays; }, Element};
}

test('sentence navigation stops at the ends, replay works, and seeking stays in bounds', () => {
  const f = fixture(), audio = f.elements.get('audio');
  f.key('ArrowLeft'); assert.equal(f.plays, 0);
  f.key('ArrowRight'); assert.equal(audio.currentTime, 5.9);
  f.key('ArrowRight'); assert.equal(audio.currentTime, 11.9);
  f.key('ArrowRight'); assert.equal(f.plays, 2);
  audio.currentTime = 13; f.key('r'); assert.equal(audio.currentTime, 11.9);
  for (let i = 0; i < 5; i++) f.key('ArrowRight', {shiftKey: true});
  assert.equal(audio.currentTime, 20);
  for (let i = 0; i < 5; i++) f.key('ArrowLeft', {shiftKey: true});
  assert.equal(audio.currentTime, 0);
});

test('untranscribed audio clears active highlights and resumes on the next sentence', () => {
  const f = fixture(), audio = f.elements.get('audio');
  const rows = f.elements.get('transcript').children;
  const words = rows.map(row => row.children[1].children.at(-1).children[0]);
  audio.currentTime = 1.5; audio.dispatchEvent({type: 'timeupdate'});
  assert.equal(rows[0].classList.contains('active'), true);
  assert.equal(words[0].classList.contains('spoken'), true);
  audio.currentTime = 4; audio.dispatchEvent({type: 'timeupdate'});
  assert.equal(rows.some(row => row.classList.contains('active')), false);
  assert.equal(words.some(word => word.classList.contains('spoken')), false);
  assert.equal(audio.currentTime, 4);
  audio.currentTime = 6.5; audio.dispatchEvent({type: 'timeupdate'});
  assert.equal(rows[1].classList.contains('active'), true);
  assert.equal(words[1].classList.contains('spoken'), true);
  audio.currentTime = 17; audio.dispatchEvent({type: 'timeupdate'});
  assert.equal(rows.some(row => row.classList.contains('active')), false);
  assert.equal(words.some(word => word.classList.contains('spoken')), false);
});

test('long transcript updates touch only changed rows and leave idle frames unchanged', () => {
  const sentences = Array.from({length: 5000}, (_, id) => ({id, start: id * 6 + 1, end: id * 6 + 3,
    speaker: 'Host', text: `Sentence ${id}`, words: [{text: `word${id}`, start: id * 6 + 1, end: id * 6 + 2}]}));
  const f = fixture('player', {duration: 30000, sentences, chapters: [{title: 'Start', sentence: 0}], review: {warnings: []}});
  const audio = f.elements.get('audio'), rows = f.elements.get('transcript').children;
  audio.currentTime = sentences[1400].start + 0.2; audio.dispatchEvent({type: 'timeupdate'});
  const before = {...f.stats};
  for (let i = 0; i < 100; i++) {
    audio.currentTime += 0.0001; audio.dispatchEvent({type: 'timeupdate'});
  }
  assert.equal(f.stats.classOps, before.classOps);
  assert.equal(f.stats.textWrites, before.textWrites);
  assert.equal(f.stats.attributeWrites, before.attributeWrites);
  assert.equal(f.stats.geometryReads, before.geometryReads);
  audio.currentTime = sentences[1401].start + 0.2; audio.dispatchEvent({type: 'timeupdate'});
  assert.ok(f.stats.classOps - before.classOps <= 8);
  assert.equal(rows[1400].classList.contains('selected'), false);
  assert.equal(rows[1401].classList.contains('selected'), true);
  assert.equal(rows[1401].classList.contains('active'), true);
});

test('rapid playback restarts keep exactly one animation loop', () => {
  const f = fixture(), audio = f.elements.get('audio');
  for (let i = 0; i < 100; i++) {
    f.key(' '); assert.equal(f.frames.size, 1);
    f.key(' '); assert.equal(f.frames.size, 0);
  }
  f.key(' '); audio.dispatchEvent({type: 'play'});
  assert.equal(f.frames.size, 1);
  f.runFrame(); assert.equal(f.frames.size, 1);
  audio.pause(); assert.equal(f.frames.size, 0);
});

test('following can resume on the same sentence without waiting for its boundary', () => {
  const f = fixture(), audio = f.elements.get('audio');
  const row = f.elements.get('transcript').children[1];
  row.rect = {top: 2000, bottom: 2100};
  f.key('f'); audio.currentTime = 6.5; audio.dispatchEvent({type: 'timeupdate'});
  assert.equal(f.stats.scrolls, 0);
  f.elements.get('follow').click(); assert.equal(f.stats.scrolls, 1);
});

test('seeking back to the same sentence follows again after leaving it', () => {
  const f = fixture(), audio = f.elements.get('audio'), seek = f.elements.get('seek');
  const row = f.elements.get('transcript').children[1];
  row.rect = {top: 2000, bottom: 2100};
  audio.currentTime = 6.5; audio.dispatchEvent({type: 'timeupdate'});
  assert.equal(f.stats.scrolls, 1);
  audio.currentTime = 4; audio.dispatchEvent({type: 'timeupdate'});
  seek.value = 6.5; seek.dispatchEvent({type: 'input'});
  assert.equal(f.stats.scrolls, 2);
  assert.equal(row.classList.contains('active'), true);
});

test('speed changes use the existing choices and stop at their limits', () => {
  const f = fixture(), speed = f.elements.get('speed'), audio = f.elements.get('audio');
  f.key('['); assert.equal(speed.value, '0.85'); assert.equal(audio.playbackRate, 0.85);
  for (let i = 0; i < 8; i++) f.key('[', {repeat: true});
  assert.equal(speed.value, '0.6');
  for (let i = 0; i < 8; i++) f.key(']', {repeat: true});
  assert.equal(speed.value, '1.25'); assert.equal(audio.playbackRate, 1.25);
});

test('toggle keys run once when held and use mutually exclusive practice modes', () => {
  const f = fixture(), get = id => f.elements.get(id);
  f.key('l'); assert.equal(get('loop').checked, true);
  f.key('l', {repeat: true}); assert.equal(get('loop').checked, true);
  f.key('p'); assert.equal(get('pauseEnd').checked, true); assert.equal(get('loop').checked, false);
  f.key('l'); assert.equal(get('loop').checked, true); assert.equal(get('pauseEnd').checked, false);
  f.key('f'); assert.equal(get('follow').checked, false);
  f.key('f', {repeat: true}); assert.equal(get('follow').checked, false);
  f.key('H'); assert.equal(get('transcript').classList.contains('hidden-text'), true);
  f.key('h', {repeat: true}); assert.equal(get('hide').attributes['aria-pressed'], 'true');
  f.key('v'); assert.equal(get('reveal').attributes['aria-pressed'], 'true');
  f.key('v'); assert.equal(get('reveal').attributes['aria-pressed'], 'false');
});

test('play and replay cancel a pending loop without leaving a timer behind', () => {
  const f = fixture(), audio = f.elements.get('audio');
  f.key('l'); f.key(' '); audio.currentTime = 3.1; audio.dispatchEvent({type: 'timeupdate'});
  assert.equal(f.timers.size, 1); assert.equal(audio.paused, true);
  f.key(' '); assert.equal(f.timers.size, 0); assert.equal(audio.paused, true);
  f.key(' '); assert.equal(audio.paused, false);
  audio.currentTime = 3.1; audio.dispatchEvent({type: 'timeupdate'});
  assert.equal(f.timers.size, 1);
  f.key('r'); assert.equal(f.timers.size, 0); assert.equal(audio.currentTime, 0.9);
});

function adjacentSentences() {
  return {duration: 10, chapters: [{title: 'Start', sentence: 0}], review: {warnings: []},
    sentences: [
      {id: 0, start: 1, end: 3, speaker: 'Host', text: 'Finished sentence', words: [{text: 'Finished', start: 1, end: 3}]},
      {id: 1, start: 3.05, end: 5, speaker: 'Host', text: 'Next sentence', words: [{text: 'Next', start: 3.05, end: 5}]},
    ]};
}

test('sentence-end pause keeps the completed sentence active and V reveals it at adjacent boundaries', () => {
  for (const boundary of [3.05, 3.2]) {
    const f = fixture('player', adjacentSentences()), audio = f.elements.get('audio');
    const rows = f.elements.get('transcript').children;
    f.key('p'); f.key('h'); f.key(' ');
    audio.currentTime = boundary; f.runFrame();
    assert.equal(audio.paused, true);
    assert.match(f.elements.get('status').textContent, /第 1 句已播完/);
    audio.dispatchEvent({type: 'timeupdate'});
    assert.equal(rows[0].classList.contains('active'), true);
    assert.equal(rows[1].classList.contains('active'), false);
    assert.equal(rows[1].children[1].children.at(-1).children[0].classList.contains('spoken'), false);
    f.key('v'); assert.equal(rows[0].classList.contains('revealed'), true);
    assert.equal(rows[1].classList.contains('revealed'), false);
    f.key('v'); assert.equal(rows[0].classList.contains('revealed'), false);
    f.key(' '); assert.equal(audio.currentTime, 0.9);
    assert.equal(audio.paused, false);
    audio.currentTime = boundary; f.runFrame();
    f.key('ArrowRight'); assert.ok(Math.abs(audio.currentTime - 2.95) < 1e-9);
    assert.equal(rows[1].classList.contains('active'), true);
    f.key('v'); assert.equal(rows[1].classList.contains('revealed'), true);
  }
});

test('sentence-end pause retains its sentence when the next update lands in an untranscribed gap', () => {
  const f = fixture(), audio = f.elements.get('audio'), rows = f.elements.get('transcript').children;
  f.key('p'); f.key('h'); f.key(' ');
  audio.currentTime = 4; f.runFrame();
  assert.equal(audio.paused, true);
  assert.equal(rows[0].classList.contains('active'), true);
  f.key('v'); assert.equal(rows[0].classList.contains('revealed'), true);
});

test('loop waiting and sentence-end pause retain the target until leaving practice mode', () => {
  for (const mode of ['l', 'p']) {
    const f = fixture('player', adjacentSentences()), audio = f.elements.get('audio');
    const rows = f.elements.get('transcript').children;
    f.key(mode); f.key('h'); f.key(' ');
    audio.currentTime = 3.2; f.runFrame();
    assert.equal(f.timers.size, mode === 'l' ? 1 : 0);
    assert.equal(rows[0].classList.contains('active'), true);
    f.key('v'); assert.equal(rows[0].classList.contains('revealed'), true);
    assert.equal(rows[1].classList.contains('revealed'), false);
    f.key(mode); assert.equal(f.timers.size, 0);
    assert.equal(rows[1].classList.contains('active'), true);
    assert.equal(rows[1].classList.contains('selected'), true);
  }
});

test('slash focuses and selects search; Escape clears it and leaves search', () => {
  const f = fixture(), search = f.elements.get('search'); search.value = 'word';
  f.key('/'); assert.equal(f.document.activeElement, search); assert.deepEqual(search.selection, [0, 4]);
  f.key('l'); assert.equal(f.elements.get('loop').checked, false);
  f.key('ArrowRight'); assert.equal(f.plays, 0);
  f.key('Escape'); assert.equal(search.value, ''); assert.equal(f.document.activeElement, f.document.body);
});

test('typing, composition, browser shortcuts, and prevented events do not trigger actions', () => {
  for (const options of [{isComposing: true}, {keyCode: 229}, {ctrlKey: true}, {metaKey: true}, {altKey: true}, {defaultPrevented: true}]) {
    const f = fixture(); f.key('l', options); assert.equal(f.elements.get('loop').checked, false);
  }
  for (const tag of ['input', 'textarea', 'select']) {
    const f = fixture(); new f.Element(tag).focus(); f.key('l'); assert.equal(f.elements.get('loop').checked, false);
  }
  const f = fixture(), editable = new f.Element(); editable.isContentEditable = true; editable.focus();
  f.key('l'); assert.equal(f.elements.get('loop').checked, false);
});

test('native Space activation is preserved for buttons, links, and nested button contents', () => {
  const f = fixture(), button = f.elements.get('hide');
  button.focus(); assert.equal(f.key(' ').defaultPrevented, undefined); assert.equal(f.plays, 0);
  const child = new f.Element('kbd'); button.append(child); child.focus();
  assert.equal(f.key(' ').defaultPrevented, undefined);
  const link = new f.Element('a'); link.focus(); assert.equal(f.key(' ').defaultPrevented, undefined);
  f.document.body.focus(); f.key(' '); assert.equal(f.plays, 1);
  f.key(' ', {repeat: true}); assert.equal(f.elements.get('audio').paused, false);
});

test('help lists all shortcuts and keeps player commands out of an open dialog', () => {
  const f = fixture(); assert.equal(f.elements.get('shortcut-list').children.length, 32);
  f.key('?'); assert.equal(f.elements.get('shortcut-dialog').open, true);
  f.key('l'); assert.equal(f.elements.get('loop').checked, false);
  f.key('ArrowRight'); assert.equal(f.plays, 0);
  f.elements.get('shortcut-close').click();
  f.key('l'); assert.equal(f.elements.get('loop').checked, true);
});

test('library navigation focuses links and clamps to the first and last episodes', () => {
  const f = fixture('library'), links = f.elements.get('episodes').children;
  f.key('ArrowDown'); assert.equal(f.document.activeElement, links[0]);
  f.key('ArrowDown'); assert.equal(f.document.activeElement, links[1]);
  f.key('End'); assert.equal(f.document.activeElement, links[2]);
  f.key('ArrowDown'); assert.equal(f.document.activeElement, links[2]);
  f.key('Home'); assert.equal(f.document.activeElement, links[0]);
  f.key('ArrowUp'); assert.equal(f.document.activeElement, links[0]);
  f.document.body.focus(); f.key('ArrowUp'); assert.equal(f.document.activeElement, links[2]);
});

test('library keys leave form controls, deletion dialogs, and an empty library alone', () => {
  const f = fixture('library'), button = new f.Element('button'); button.focus();
  assert.equal(f.key('ArrowDown').defaultPrevented, undefined); assert.equal(f.document.activeElement, button);
  f.elements.get('purge-dialog').showModal(); f.key('?'); assert.equal(f.elements.get('shortcut-dialog').open, false);
  assert.equal(f.key('ArrowDown').defaultPrevented, undefined);
  f.elements.get('purge-dialog').close(); f.document.body.focus();
  f.key('?', {isComposing: true}); assert.equal(f.elements.get('shortcut-dialog').open, false);
  f.elements.get('episodes').children = []; assert.equal(f.key('ArrowDown').defaultPrevented, undefined);
});
