/* Run runtime.js's theme block FOR REAL against a tiny fake window tree and
 * report what each framed document ended up with. Same approach as
 * `_capture_bridge_probe.mjs`: the shipping source is sliced out verbatim and
 * run; only the DOM around it is faked.
 *
 * The fake MutationObserver honours `attributeFilter`, so a token the real
 * observer would not have been told about is also missed here.
 *
 * Usage: node _theme_tokens_probe.mjs <runtime.js>
 * Prints one JSON object keyed by scenario.
 */
import { readFileSync } from "node:fs";

const src = readFileSync(process.argv[2], "utf8");
const start = src.indexOf("var THEME_KEY");
const end = src.indexOf("startTheme();", start);
const block = src.slice(start, end);

function makeEl(attrs = {}, computed = {}) {
  const a = { ...attrs };
  const props = {};
  const observers = [];
  const notify = (name) => {
    for (const o of observers) {
      if (!o.filter || o.filter.includes(name)) o.cb();
    }
  };
  return {
    computed,
    _observers: observers,
    getAttribute: (n) => (n in a ? a[n] : null),
    hasAttribute: (n) => n in a,
    setAttribute(n, v) {
      const changed = a[n] !== String(v);
      a[n] = String(v);
      if (changed) notify(n);
    },
    removeAttribute(n) {
      if (n in a) { delete a[n]; notify(n); }
    },
    style: {
      colorScheme: "",
      getPropertyValue: (n) => props[n] || "",
      setProperty(n, v) {
        if (props[n] !== v) { props[n] = v; notify("style"); }
      },
      removeProperty(n) {
        if (n in props) { delete props[n]; notify("style"); }
      },
    },
  };
}

function makeWin(el, parent) {
  const win = {
    parent: parent || null,
    document: { documentElement: el },
    getComputedStyle: (e) => ({ getPropertyValue: (n) => e.computed[n] || "" }),
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    addEventListener() {},
  };
  if (!parent) win.parent = win;
  return win;
}

class FakeObserver {
  constructor(cb) { this.cb = cb; }
  observe(el, opts) {
    el._observers.push({ cb: this.cb, filter: opts && opts.attributeFilter });
  }
}

function boot(win) {
  const run = new Function(
    "window", "document", "localStorage", "MutationObserver",
    block + "; startTheme();",
  );
  run(win, win.document, { getItem: () => null }, FakeObserver);
  return win.document.documentElement;
}

const SHELL = {
  "--bg": "#000011", "--bg-alt": "#000022", "--bg-panel": "#000033",
  "--fg": "#eeeeee", "--fg-muted": "#cccccc", "--fg-faint": "#999999",
  "--border": "#223344", "--sel": "#112233", "--accent": "#ff00ff",
  "--on-accent": "#ffffff", "--error": "#ff0000", "--success": "#00ff00",
  "--warning": "#ffff00",
};
const shellEl = () =>
  makeEl({ "data-theme": "dark", "data-theme-name": "midnight" }, { ...SHELL });
const tokens = (el) =>
  Object.fromEntries(
    ["--fused-bg", "--fused-accent", "--fused-warning"].map((n) => [
      n, el.style.getPropertyValue(n),
    ]),
  );
const out = {};

// 1. opted-in child of the shell
{
  const shell = shellEl();
  const sw = makeWin(shell);
  const child = boot(makeWin(makeEl({ "data-fused-theme": "shell" }), sw));
  out.optedIn = {
    tokens: tokens(child),
    name: child.getAttribute("data-fused-theme-name"),
    theme: child.getAttribute("data-theme"),
    count: Object.keys(SHELL).filter(
      (n) => child.style.getPropertyValue("--fused-" + n.slice(2)),
    ).length,
  };
  // live: preset changes in the shell
  shell.computed["--bg"] = "#ffffff";
  shell.computed["--accent"] = "";
  shell.setAttribute("data-theme-name", "contrast");
  out.live = {
    tokens: tokens(child),
    name: child.getAttribute("data-fused-theme-name"),
  };
  shell.removeAttribute("data-theme-name");
  out.cleared = { name: child.getAttribute("data-fused-theme-name") };
}

// 2. non-opted-in child gets colour-scheme only
{
  const sw = makeWin(shellEl());
  const child = boot(makeWin(makeEl({}), sw));
  out.notOptedIn = {
    tokens: tokens(child),
    name: child.getAttribute("data-fused-theme-name"),
    theme: child.getAttribute("data-theme"),
    scheme: child.style.colorScheme,
  };
}

// 3. standalone: no ancestor at all
{
  const child = boot(makeWin(makeEl({ "data-fused-theme": "shell" })));
  out.standalone = { tokens: tokens(child), theme: child.getAttribute("data-theme") };
}

// 4. nested: shell -> opted-in framing view -> opted-in entry page
{
  const shell = shellEl();
  const sw = makeWin(shell);
  const mw = makeWin(makeEl({ "data-fused-theme": "shell" }), sw);
  boot(mw);
  const leaf = boot(makeWin(makeEl({ "data-fused-theme": "shell" }), mw));
  out.nested = { tokens: tokens(leaf), name: leaf.getAttribute("data-fused-theme-name") };
  shell.computed["--bg"] = "#abcdef";
  shell.setAttribute("data-theme", "light");
  out.nestedLive = { tokens: tokens(leaf), theme: leaf.getAttribute("data-theme") };
}

console.log(JSON.stringify(out));
