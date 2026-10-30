/* Run runtime.js's scrollbar block FOR REAL against a fake document and report
 * where the sheet landed and what canvas colour the viewport track was given.
 * Same approach as `_theme_tokens_probe.mjs`: the shipping source is sliced
 * out verbatim and run; only the DOM around it is faked.
 *
 * Usage: node _scrollbar_probe.mjs <runtime.js>
 * Prints one JSON object keyed by scenario.
 */
import { readFileSync } from "node:fs";

const src = readFileSync(process.argv[2], "utf8");
const start = src.indexOf("var SCROLLBAR_CSS");
const end = src.indexOf("startScrollbars();", start);
const block = src.slice(start, end);

function makeEl(tag, attrs = {}, bg = "rgba(0, 0, 0, 0)") {
  const props = {};
  return {
    tag,
    bg,
    children: [],
    get firstChild() {
      return this.children[0] || null;
    },
    getAttribute: (n) => (n in attrs ? attrs[n] : null),
    setAttribute: (n, v) => {
      attrs[n] = String(v);
    },
    insertBefore(node, ref) {
      const i = ref ? this.children.indexOf(ref) : -1;
      if (i < 0) this.children.push(node);
      else this.children.splice(i, 0, node);
    },
    style: {
      getPropertyValue: (n) => props[n] || "",
      setProperty: (n, v) => {
        props[n] = v;
      },
      removeProperty: (n) => {
        delete props[n];
      },
    },
  };
}

// Returns { doc, fire } — `fire()` runs every listener and observer callback,
// then every queued animation frame, the way a page load would.
function boot({ rootAttrs = {}, rootBg, bodyBg } = {}) {
  const root = makeEl("html", rootAttrs, rootBg);
  const head = makeEl("head");
  head.children.push(makeEl("script")); // runtime.js itself
  const body = makeEl("body", {}, bodyBg);
  const callbacks = [];
  const frames = [];
  const doc = {
    documentElement: root,
    head,
    body,
    createElement: (tag) => makeEl(tag),
    addEventListener: (_, cb) => callbacks.push(cb),
  };
  const win = { addEventListener: (_, cb) => callbacks.push(cb) };
  class MutationObserver {
    constructor(cb) {
      callbacks.push(cb);
    }
    observe() {}
  }
  const run = new Function(
    "document",
    "window",
    "getComputedStyle",
    "requestAnimationFrame",
    "MutationObserver",
    "DARK_QUERY",
    block + "\nstartScrollbars();",
  );
  run(
    doc,
    win,
    (el) => ({ backgroundColor: el.bg }),
    (cb) => frames.push(cb),
    MutationObserver,
    "(prefers-color-scheme: dark)",
  );
  const fire = () => {
    callbacks.forEach((cb) => cb());
    frames.splice(0).forEach((cb) => cb());
  };
  return { doc, fire };
}

const sheetOf = (doc) => doc.head.children.find((c) => c.tag === "style") || null;
const out = {};

{
  const { doc, fire } = boot({ bodyBg: "rgb(13, 17, 23)" });
  fire();
  const sheet = sheetOf(doc);
  out.bodyCanvas = {
    first: doc.head.firstChild === sheet,
    css: sheet && sheet.textContent,
    canvas: doc.documentElement.style.getPropertyValue("--fused-sb-canvas"),
  };
}

{
  const { doc, fire } = boot({ rootBg: "rgb(255, 255, 255)", bodyBg: "rgb(1, 2, 3)" });
  fire();
  out.rootCanvas = { canvas: doc.documentElement.style.getPropertyValue("--fused-sb-canvas") };
}

{
  const { doc, fire } = boot({ bodyBg: "rgb(255, 255, 255)" });
  fire();
  doc.body.bg = "rgba(0, 0, 0, 0)";
  fire();
  out.cleared = { canvas: doc.documentElement.style.getPropertyValue("--fused-sb-canvas") };
}

{
  const { doc } = boot({ rootAttrs: { "data-fused-scrollbars": "native" } });
  out.native = { sheet: !!sheetOf(doc) };
}

process.stdout.write(JSON.stringify(out));
