// Runs the code editor template's pure helpers (SPEC §51) against the SHIPPED
// bundle and prints what they return, as JSON.
//
// Unlike the markdown probes this needs no node_modules: it evaluates
// fused_render/templates/vendor/codemirror.bundle.js itself (an IIFE that
// assigns `CM`), so it exercises exactly the CodeMirror build users get —
// including whether every language the template lists is actually in it.
//
// Usage: node code-editor-probe.mjs <template.html> <bundle.js> <casesJson>
//
// casesJson is a list of [op, ...args]; see OPS below. Each result is either
// {ok: value} or {error: message}, in the same order.
import fs from "node:fs";

const [templatePath, bundlePath, casesArg] = process.argv.slice(2);
const cases = JSON.parse(casesArg || "[]");

globalThis.CM = new Function(fs.readFileSync(bundlePath, "utf8") + "\n;return CM;")();

// Enough of a page for the template's top-level code to run to the end. With
// `_file` empty, its load() bails before touching the editor, so nothing
// below needs to behave — only to exist.
const element = () => new Proxy({}, {
  get(_target, key) {
    if (key === "classList") return { toggle() {}, add() {}, remove() {} };
    if (key === "style" || key === "dataset") return {};
    if (key === "textContent" || key === "innerHTML" || key === "value") return "";
    return () => {};
  },
  set() { return true; },
});
globalThis.document = {
  getElementById: element,
  documentElement: { getAttribute: () => "dark" },
};
globalThis.window = { addEventListener() {} };
globalThis.Option = class { constructor(text, value) { this.text = text; this.value = value; } };
globalThis.MutationObserver = class { observe() {} disconnect() {} };
globalThis.fusedRoBadge = { update() {} };
globalThis.fused = {
  autoReload() {},
  params: { get: () => "" },
  rawUrl: (p) => p,
};

const html = fs.readFileSync(templatePath, "utf8");
const inline = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (inline.length !== 1) throw new Error("expected exactly one inline <script>, got " + inline.length);
const T = new Function(inline[0] + `
  return { LANGUAGES, detectLanguage, detectIndent, detectEol, textForEol,
           eolExtension, reformatJson, jsonLinesLinter, languageExtension };`)();

const fakeView = (doc, extensions = []) => ({ state: CM.EditorState.create({ doc, extensions }) });

const OPS = {
  language: (path) => T.detectLanguage(path).id,
  indent: (text) => T.detectIndent(text),
  eol: (text) => T.detectEol(text),
  // What a load-then-save of `raw` writes back, untouched: the CE-5 contract.
  roundTrip: (raw) => {
    const eol = T.detectEol(raw);
    const state = CM.EditorState.create({ doc: T.textForEol(raw, eol), extensions: [T.eolExtension(eol)] });
    return state.sliceDoc();
  },
  reformat: (text, unit, nl) => T.reformatJson(text, unit, nl),
  lintJsonl: (doc) => T.jsonLinesLinter(fakeView(doc))
    .map((d) => ({ from: d.from, to: d.to, message: d.message })),
  // Every picker entry must build a working state from THIS bundle — a typo'd
  // legacyModes key or a grammar dropped from entry.js fails here.
  everyLanguageBuilds: () => T.LANGUAGES.map((l) => {
    try {
      CM.EditorState.create({ doc: "x", extensions: [T.languageExtension(l)] });
      return [l.id, "ok"];
    } catch (err) {
      return [l.id, String(err && err.message)];
    }
  }),
};

const results = cases.map(([op, ...args]) => {
  try {
    return { ok: OPS[op](...args) };
  } catch (err) {
    return { error: String(err && err.message) };
  }
});
process.stdout.write(JSON.stringify(results));
