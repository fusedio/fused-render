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
  addEventListener() {}
  removeEventListener() {}
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

// --------------------------------------------------------------- window stub
let fatal = null;
const unhandled = [];
process.on("unhandledRejection", (err) => {
  unhandled.push(err && err.stack ? String(err.stack).split("\n").slice(0, 4).join(" | ")
                                  : String(err));
});

const location = { href: "http://127.0.0.1/probe", search: "", pathname: "/probe" };
const history = { replaceState() {}, pushState() {} };
const window = {
  fused, document, location, history,
  addEventListener: () => {}, removeEventListener: () => {},
  parent: null, frameElement: null, top: null,
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
    "getComputedStyle", "localStorage", "sessionStorage", "self",
    '"use strict";\n' + source);
  runner(window, document, fused, location, history, { platform: "probe", clipboard: {} },
         window.requestAnimationFrame, window.setInterval, window.clearInterval,
         window.matchMedia, window.getComputedStyle, window.localStorage,
         window.sessionStorage, window);
} catch (err) {
  fatal = err && err.stack ? String(err.stack).split("\n").slice(0, 5).join(" | ") : String(err);
}

// Let the async draw() settle.
await new Promise((r) => setTimeout(r, 60));
await new Promise((r) => setTimeout(r, 60));

const view = byId.view;
process.stdout.write(JSON.stringify({
  painted: view.children.length > 0,
  viewChildren: view.children.length,
  viewText: view.textContent.slice(0, 400),
  skeletonHidden: byId.skeleton.hidden,
  error: fatal,
  unhandled,
  calls,
}, null, 1));
