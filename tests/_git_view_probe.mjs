/* Run the git template's REAL script against a DOM stub, and report whether the
 * view actually painted.
 *
 * Why this exists: the full-page git view shipped BLANK on a branch whose whole
 * pytest suite was green, and it was invisible to every check we had.
 * `node --check` passes (it is not a parse error), the source-contract tests pass
 * (the source is fine to read), and `window.onerror` never fires — because
 * `draw()` is async, so a throw inside `render()` becomes an unhandled REJECTION,
 * which the page-error hook does not observe. The page calls Python, gets good
 * data, records no error, and paints nothing.
 *
 * So the only thing that catches it is running the render for real and asserting
 * the DOM is not empty. Same idea as the `_DOM_STUB` harnesses in
 * test_annotate_revert.py, sized up for this template: a stub just large enough
 * to run the shipping code verbatim, with no copy of the logic under test.
 *
 * Usage: node _git_view_probe.mjs <template.html> <fixture.json>
 * Prints one JSON object: { painted, viewChildren, error, unhandled, calls }.
 */
import { readFileSync } from "node:fs";

const [templatePath, fixturePath] = process.argv.slice(2);
const html = readFileSync(templatePath, "utf8");
const fixture = JSON.parse(readFileSync(fixturePath, "utf8"));

// ---------------------------------------------------------------- DOM stub
class ClassList {
  constructor(node) { this.node = node; this.set = new Set(); }
  add(...c) { c.forEach((x) => x && this.set.add(x)); }
  remove(...c) { c.forEach((x) => this.set.delete(x)); }
  toggle(c, on) { (on === undefined ? !this.set.has(c) : on) ? this.set.add(c) : this.set.delete(c); }
  contains(c) { return this.set.has(c); }
}

class El {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.attrs = {};
    this.classList = new ClassList(this);
    this._text = "";
    this.style = {};
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.parentNode = null;
  }
  get className() { return [...this.classList.set].join(" "); }
  set className(v) {
    this.classList.set = new Set(String(v || "").split(/\s+/).filter(Boolean));
  }
  get textContent() {
    return this._text || this.children.map((c) => (c.textContent ?? String(c))).join("");
  }
  set textContent(v) { this._text = v === null || v === undefined ? "" : String(v); this.children = []; }
  append(...kids) {
    for (const k of kids.flat()) {
      if (k === null || k === undefined || k === false) continue;
      const node = typeof k === "object" ? k : Object.assign(new El("span"), { _text: String(k) });
      if (node instanceof Frag) { this.append(...node.children); continue; }
      node.parentNode = this;
      this.children.push(node);
    }
  }
  appendChild(k) { this.append(k); return k; }
  prepend(...kids) { const old = this.children; this.children = []; this.append(...kids); this.children.push(...old); }
  replaceChildren(...kids) { this.children = []; this._text = ""; this.append(...kids); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  // Real enough to drive the view's own click/keydown handlers (see `click()`
  // below) — the fixture-driven `actions` list needs this to arm a
  // confirmation on one row and then select a different one, which no amount
  // of asserting on rendered markup alone can exercise.
  addEventListener(type, fn) {
    (this._listeners ||= {});
    (this._listeners[type] ||= []).push(fn);
  }
  removeEventListener(type, fn) {
    this._listeners?.[type]?.splice(0).forEach((f) => {
      if (f !== fn) (this._listeners[type] ||= []).push(f);
    });
  }
  click() {
    // A real DOM dispatches no click at all on a disabled element — the
    // browser eats the event before any listener sees it. `action()`
    // (template.html) guards its own handler only on `busy !== null`, not on
    // `settings.disabled`, so without this check the probe could "verify" a
    // gesture — clicking a button disabled for consent-key or gating
    // reasons — that no browser would ever deliver (finding 6, second
    // review round).
    if (this.disabled) return;
    const event = { type: "click", preventDefault() {}, stopPropagation() {} };
    for (const fn of (this._listeners?.click ?? []).slice()) fn(event);
  }
  focus() {}
  scrollIntoView() {}
  closest() { return null; }
  contains() { return false; }
  // Depth-first descendant search over class/tag selectors, enough for the
  // template's three querySelector calls.
  querySelector(sel) {
    for (const kid of this.children) {
      if (matches(kid, sel)) return kid;
      const deep = kid.querySelector ? kid.querySelector(sel) : null;
      if (deep) return deep;
    }
    return null;
  }
  querySelectorAll(sel) {
    const out = [];
    for (const kid of this.children) {
      if (matches(kid, sel)) out.push(kid);
      if (kid.querySelectorAll) out.push(...kid.querySelectorAll(sel));
    }
    return out;
  }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  get childElementCount() { return this.children.length; }
}

class Frag extends El {}

function matches(node, sel) {
  return String(sel).split(/\s+/).every((part) => {
    if (part.startsWith(".")) return node.classList && node.classList.contains(part.slice(1));
    if (part.startsWith("#")) return node.attrs && node.attrs.id === part.slice(1);
    return node.tagName === part.toUpperCase();
  });
}

const byId = {};
for (const id of ["skeleton", "view", "live", "msg"]) {
  byId[id] = new El("div");
  byId[id].attrs.id = id;
}

const document = {
  getElementById: (id) => byId[id] || null,
  createElement: (t) => new El(t),
  createElementNS: (_ns, t) => new El(t),
  createDocumentFragment: () => new Frag("fragment"),
  createTextNode: (t) => Object.assign(new El("span"), { _text: String(t) }),
  addEventListener: () => {},
  removeEventListener: () => {},
  activeElement: null,
  body: new El("body"),
  documentElement: new El("html"),
  querySelector: (sel) => byId.view.querySelector(sel) || byId.skeleton.querySelector(sel),
  querySelectorAll: (sel) => byId.view.querySelectorAll(sel),
  title: "",
};

// ------------------------------------------------------------- fused stub
const calls = [];
const params = new Map(Object.entries(fixture.params || {}));
let onChange = () => {};

const fused = {
  env: "local",
  params: {
    get: (k) => (params.has(k) ? params.get(k) : undefined),
    getAll: () => Object.fromEntries(params),
    set: (k, v) => {
      if (typeof v !== "string" && v !== null) throw new TypeError("params must be strings");
      if (v === null) params.delete(k); else params.set(k, v);
      onChange(Object.fromEntries(params));
    },
    onChange: (cb) => { onChange = cb; return () => {}; },
  },
  runPython: (py, p) => {
    calls.push({ py, op: p && p.op });
    const op = (p && p.op) || "overview";
    if (!(op in fixture.payloads)) {
      return Promise.reject(Object.assign(new Error("no fixture for op " + op), { type: "probe" }));
    }
    return Promise.resolve(fixture.payloads[op]);
  },
  ai: Object.assign(() => Promise.reject(Object.assign(new Error("no AI in the probe"),
                                                      { type: "ai_unavailable" })),
                    { cancel: () => Promise.resolve(false) }),
  readFile: () => Promise.resolve(""),
  writeFile: () => Promise.resolve({ mtime: 1 }),
  stat: () => Promise.resolve({ writable: true, mtime: 1 }),
  rawUrl: (p) => "raw:" + p,
  trackJob: () => ({ update() {}, finish() {}, fail() {}, cancelled() {} }),
  autoReload: () => {},
};

// ------------------------------------------------------------- fetch stub
//
// The template's own `ghFetch` calls the real `fetch` global for every
// `/api/github/*` read the publish modal makes. Node has a real `fetch` in
// scope even inside `new Function(...)` (it is not a true sandbox — see the
// comment below on why compiling is still wrapped in a try), so without a
// stub the probe would either hang on a real network call or the modal's
// prerequisite states would never render (the "Publish to GitHub" scenarios
// this file's tests need). `fixture.github`, keyed by the exact request
// path, overrides one entry at a time — a test wanting "gh missing" sets
// `{"/api/github/status": {"found": false, ...}}` and leaves the rest at
// their default "everything is fine" shape, the same one-field-at-a-time
// convention `fixture.payloads` already uses for `runPython`.
const GH_DEFAULTS = {
  "/api/github/status": { found: true, path: "/usr/bin/gh", source: "path",
                          version: "2.50.0", signed_in: true, account: "octocat",
                          checked_at: Date.now() / 1000 },
  "/api/github/install": { state: "idle", detail: "", error: null,
                           started_at: null, finished_at: null },
  "/api/github/login": { in_flight: false, started_at: null, error: null },
  "/api/github/publish": { state: "idle", detail: "", error: null,
                           started_at: null, finished_at: null },
};
const ghTable = Object.assign({}, GH_DEFAULTS, fixture.github || {});

// `previewCapable` opts a fixture into BOTH halves of the preview gate
// (D701/B4): a marked ancestor frame (`revMarkedFrame()`) and a confirmed
// app folder (`probeAppFolder()`'s `/api/git/app-folder` read). Off by
// default — `window.parent` stays `null` and the app-folder probe reads
// `false` — so every pre-existing fixture keeps rendering exactly the
// capability-off DOM it always has; a test that needs the eye/Checkout/
// Revert controls on screen opts in explicitly rather than every fixture
// gaining them for free. `fixture.gitAppFolder` overrides the app-folder
// half alone, for a test that wants a marked pane but NO app folder (the
// gate's other, independent failure mode).
const PREVIEW_CAPABLE = fixture.previewCapable === true;
function fetchStub(path, init) {
  calls.push({ fetch: path, method: (init && init.method) || "GET" });
  const url = String(path).split("?")[0];
  if (url === "/api/git/app-folder") {
    const body = fixture.gitAppFolder !== undefined
      ? fixture.gitAppFolder : { ok: PREVIEW_CAPABLE };
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
  }
  const body = Object.prototype.hasOwnProperty.call(ghTable, path)
    ? ghTable[path] : { error: "no fixture for " + path };
  return Promise.resolve({ ok: !body.error, status: body.error ? 400 : 200,
                           json: () => Promise.resolve(body) });
}

// --------------------------------------------------------------- window stub
let fatal = null;
const unhandled = [];
process.on("unhandledRejection", (err) => {
  unhandled.push(err && err.stack ? String(err.stack).split("\n").slice(0, 4).join(" | ")
                                  : String(err));
});

const location = { href: "http://127.0.0.1/probe", search: "", pathname: "/probe" };
const history = { replaceState() {}, pushState() {} };
// The host's mark (`revMarkedFrame()`, Preview.tsx's real `data-fused-rev-target`
// attribute) — an ancestor `window.parent` whose document has ONE element the
// query can find. Only built when `previewCapable` asks for it; otherwise
// `window.parent` stays `null`, exactly as every pre-existing fixture already
// exercises (a page opened outside the shell has no such ancestor at all).
const markedFrame = PREVIEW_CAPABLE ? new El("iframe") : null;
const previewHost = PREVIEW_CAPABLE ? {
  document: { querySelector: (sel) => (sel === "[data-fused-rev-target]" ? markedFrame : null) },
} : null;
const window = {
  fused, document, location, history,
  addEventListener: () => {}, removeEventListener: () => {},
  parent: previewHost, frameElement: null, top: null,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  setInterval: () => 0, setTimeout, clearInterval: () => {}, clearTimeout,
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  postMessage: () => {},
  scrollTo: () => {},
};
window.self = window;
window.window = window;

// ----------------------------------------------------------------- run it
const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
const source = scripts.join("\n");

try {
  // COMPILING is inside the try, not just calling: a duplicate top-level `let`
  // — the exact failure that shipped — is a SyntaxError raised when the function
  // is CONSTRUCTED. With this outside the try it escaped, node died with empty
  // stdout, and the probe reported nothing at all rather than the error. Which
  // is the same "unverifiable reads as fine" trap this whole harness is for.
  const runner = new Function(
    "window", "document", "fused", "location", "history", "navigator",
    "requestAnimationFrame", "setInterval", "clearInterval", "matchMedia",
    "getComputedStyle", "localStorage", "sessionStorage", "self", "fetch",
    '"use strict";\n' + source);
  // `fetch` is passed explicitly, same as every other ambient global here —
  // `new Function` closes over the REAL global scope, not this module's, so
  // a bare `fetch(...)` in the template would otherwise resolve to Node's own
  // `fetch` and either hang the probe on a live network call or reach a host
  // that was never asked for.
  runner(window, document, fused, location, history, { platform: "probe", clipboard: {} },
         window.requestAnimationFrame, window.setInterval, window.clearInterval,
         window.matchMedia, window.getComputedStyle, window.localStorage,
         window.sessionStorage, window, fetchStub);
} catch (err) {
  fatal = err && err.stack ? String(err.stack).split("\n").slice(0, 5).join(" | ") : String(err);
}

// Let the async draw() settle.
await new Promise((r) => setTimeout(r, 60));
await new Promise((r) => setTimeout(r, 60));

// `fixture.actions`: an ordered list of clicks the harness performs against
// the ALREADY-RENDERED view, each `{ titleIncludes }` or `{ ariaLabel }`
// selecting the first matching element by DFS. This is what lets a test drive
// the view through a real sequence of gestures (select commit A, arm Revert,
// select commit B) rather than hand-assigning `selection`/`previewed` —
// neither of which is reachable through a URL param (see the state header's
// own "not a param at all any more"), so a fixture that could only set params
// could never reach the state findings 4/5 are about.
function findWhere(node, pred) {
  if (!node || node.tagName === undefined) return null;
  if (pred(node)) return node;
  for (const kid of node.children || []) {
    const hit = findWhere(kid, pred);
    if (hit) return hit;
  }
  return null;
}
for (const step of fixture.actions || []) {
  // `title` on these nodes is a plain DOM PROPERTY (`el()` sets it via
  // `Object.assign`, never `setAttribute`), so it lives on the node itself —
  // NOT in `.attrs`, which only holds what an explicit `setAttribute` call
  // put there (that is where `aria-label` lives, e.g. the row's own click
  // target has no `aria-label` at all).
  const pred = step.ariaLabel !== undefined
    ? (n) => n.attrs && n.attrs["aria-label"] === step.ariaLabel
    : (n) => typeof n.title === "string" && n.title.includes(step.titleIncludes);
  const target = findWhere(byId.view, pred);
  if (!target) {
    unhandled.push("probe action found no element for " + JSON.stringify(step));
    continue;
  }
  // FINDING 5 (round 3): a real DOM dispatches no click at all on a disabled
  // element — `El.click()` above already mirrors that by no-op'ing rather
  // than firing any listener — but that guard used to be silent here too,
  // so a fixture step whose target happened to be disabled looked IDENTICAL
  // to one that fired and produced no visible change: neither left any
  // trace in `unhandled`, and a test asserting the absence of some later
  // effect would pass whether or not the click could ever have reached a
  // listener. Recorded here, alongside the "no element" case just above,
  // so the Python side can tell "this gesture could never fire" apart from
  // "it fired and did nothing".
  if (target.disabled) {
    unhandled.push("probe action target is disabled, click blocked: " + JSON.stringify(step));
    continue;
  }
  target.click();
  // Every handler here either repaints synchronously (`select`, `preview`)
  // or kicks off an async `draw()` — settle the same way the initial paint
  // does before the next action reads the DOM it left behind.
  await new Promise((r) => setTimeout(r, 60));
  await new Promise((r) => setTimeout(r, 60));
}

// A `title`/`aria-label` never shows up in `textContent` — a button's
// TOOLTIP and its confirm-dialog question live there, not in the visible
// label — so a test that needs to pin what those actually SAY (not just
// that the button exists) needs the attributes too. This is a minimal
// attribute-aware serialization of the view, bounded like `viewText` is.
function serialize(node) {
  if (!node || node.tagName === undefined) return "";
  const attrs = Object.entries(node.attrs || {})
    .map(([k, v]) => ` ${k}="${v}"`).join("");
  // `disabled` and `checked` live as plain DOM PROPERTIES on the stub (`el()`
  // assigns them straight onto the node, same as a real `<button disabled>`
  // reflects a property rather than living in `.attrs`), so a test that needs
  // to tell "rendered, but disabled" apart from "rendered, and usable" — the
  // whole point of turning the old dead-end button into a live one — needs
  // them surfaced here too, the same way a real `outerHTML` would show them.
  const extra = (node.disabled ? ' disabled=""' : "")
    + (node.checked ? ' checked=""' : "");
  const kids = (node.children || []).map(serialize).join("");
  return `<${node.tagName.toLowerCase()}${attrs}${extra}>${node._text || ""}${kids}</${node.tagName.toLowerCase()}>`;
}

const view = byId.view;
process.stdout.write(JSON.stringify({
  painted: view.children.length > 0,
  viewChildren: view.children.length,
  viewText: view.textContent.slice(0, 400),
  viewHTML: serialize(view).slice(0, 8000),
  skeletonHidden: byId.skeleton.hidden,
  error: fatal,
  unhandled,
  calls,
  finalParams: Object.fromEntries(params),
}, null, 1));
