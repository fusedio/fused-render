// Loads the markdown template's <script> block in node and hands back the
// functions inside it.
//
// The template is a page, not a module: it runs top-level DOM and runtime code
// before it defines anything, and it has no exports. So every probe here needs
// the same two things — enough of a browser and of the `fused` runtime for the
// script to finish loading, and a way to reach a function it never exported.
// That is all this file is. It lives apart from the probes because there are
// now two of them (decorations, and ordered-list renumbering) and a second copy
// of these stubs would drift from the first the moment the template grows a new
// global.
//
// Nothing under test touches the stubs: `_file` comes back empty, so the
// template's own load() bails immediately and every probe drives the piece it
// cares about explicitly.
import fs from "node:fs";
import {
  EditorState, RangeSetBuilder, EditorSelection, StateEffect, StateField, Prec,
} from "@codemirror/state";
import { syntaxTree, ensureSyntaxTree } from "@codemirror/language";
import {
  Decoration, WidgetType, ViewPlugin, MatchDecorator, keymap, EditorView,
} from "@codemirror/view";
import { markdown, markdownLanguage, markdownKeymap } from "@codemirror/lang-markdown";
import { autocompletion } from "@codemirror/autocomplete";
import { indentMore, indentLess } from "@codemirror/commands";

// Every top-level name a probe may want out of the template. Asking for one
// that does not exist is a ReferenceError from inside the generated function,
// which is the error you want — it names the thing that was renamed.
const EXPORTS = [
  "buildDecorations", "refreshLinks", "editorKeymap",
  "renumberFilter", "renumberOrderedLists",
];

const stub = () => new Proxy({}, {
  get(_target, key) {
    if (key === "classList") return { toggle() {}, add() {}, remove() {} };
    if (key === "dataset" || key === "style") return {};
    if (key === "textContent" || key === "innerHTML" || key === "value") return "";
    return () => {};
  },
  set() { return true; },
});

// Enough of an element for a widget builder to fill in and for us to read back.
// Not a DOM: no layout, no events, no tree semantics beyond appendChild.
export function fakeElement(tag) {
  return {
    tagName: tag.toUpperCase(),
    className: "", title: "", textContent: "", href: "", src: "", alt: "",
    type: "", checked: false, disabled: false,
    dataset: {}, style: {}, children: [],
    classList: { add() {}, remove() {}, toggle() {} },
    appendChild(child) { this.children.push(child); return child; },
    addEventListener() {},
  };
}

// A stand-in for graph.py's `note` answer, so the SCANNED state can be probed
// too — the unscanned one is the default because that is what a mount gives.
// The rule is deliberately crude and local to this harness: every `[[target]]`
// in the document resolves, except one whose name says it should not. Real
// resolution is graph.py's and is tested against graph.py (MD-6).
const UNRESOLVED_HERE = /ghost|missing|gone/i;
function fakeNoteScan(doc) {
  const links = [];
  for (const match of doc.matchAll(/!?\[\[([^\[\]\n]+?)\]\]/g)) {
    const target = match[1].split("|")[0].split("#")[0].trim();
    // `[[#Heading]]` has no target: graph.py's `_resolved_links` skips it
    // outright (it is an anchor inside this same note, not an edge), so the
    // answer this stands in for carries no row for it either.
    if (!target) continue;
    const named = /\.[a-z0-9]+$/i.test(target);
    links.push({
      target,
      path: UNRESOLVED_HERE.test(target)
        ? null : "/vault/" + target + (named ? "" : ".md"),
      title: "Title of " + target,
      heading: "", label: "", embed: match[0].startsWith("!"), wiki: true,
    });
  }
  return {
    error: null, root: "/vault", rel: "note.md", title: "note",
    headings: [], tags: [], links, backlinks: [],
    notes: 1, truncated: false, skipped_large: [], parser_version: 0,
  };
}

/**
 * Install the browser and `fused` stubs, then run the template's script block
 * and return the top-level functions named in EXPORTS.
 *
 * @param templatePath path to the markdown template.html
 * @param options.doc      the document the probe will use, so the fake scan can
 *                         answer about the wikilinks actually in it
 * @param options.scanned  graph.py answered with a real scan, so every link
 *                         target's resolution is KNOWN. Without it the stub
 *                         answers `{error: "no_scan"}` — what a mount-backed
 *                         root, a refused scan or a failed one produces — and
 *                         resolution is UNKNOWN, which must not render as
 *                         "missing" (MD-11).
 * @param options.params   what fused.params.get returns, so a mode held in a
 *                         param (MD-20) can be driven from a probe.
 */
export function loadTemplateScript(templatePath, options = {}) {
  const { doc = "", scanned = false, params = {} } = options;

  globalThis.CM = {
    EditorState, EditorView, Decoration, WidgetType, ViewPlugin, MatchDecorator,
    keymap, EditorSelection, RangeSetBuilder, Prec, StateEffect, StateField,
    // A parse that has caught up with the document, which is the state the real
    // editor converges to: `syntaxTree(state)` alone returns only what the
    // language extension has parsed so far (a few KB on a fresh state, and it
    // advances in the background from a view it does not have here), so a table
    // halfway down a real note would simply not be in the tree.
    syntaxTree: (state) => ensureSyntaxTree(state, state.doc.length, 10000)
      || syntaxTree(state),
    autocompletion, indentMore, indentLess, markdown, markdownLanguage,
    markdownKeymap, basicSetup: [], oneDark: [],
  };
  globalThis.document = {
    getElementById: stub, createElement: fakeElement, addEventListener() {},
    querySelectorAll: () => [], documentElement: { getAttribute: () => "dark" },
    visibilityState: "visible",
  };
  globalThis.window = {
    addEventListener() {}, top: { location: { pathname: "/view/x" } },
  };
  globalThis.MutationObserver = class { observe() {} disconnect() {} };
  globalThis.sessionStorage = { getItem: () => null, setItem() {} };
  globalThis.fusedRoBadge = { update() {} };
  globalThis.fusedGraph = { create: () => ({ setData() {}, nudge() {} }) };
  globalThis.fused = {
    autoReload() {},
    rawUrl: (p) => "/api/fs/raw?path=" + p,
    params: {
      get: (name) => params[name] || "",
      getAll: () => params,
      set() {}, onChange() {},
    },
    async stat() { return { mtime: 1, size: 1, writable: true }; },
    async readFile() { return ""; },
    async writeFile() { return { mtime: 2 }; },
    async runPython(_py, args) {
      if (!scanned) return { error: "no_scan", message: "not scanned here" };
      return args.action === "note" ? fakeNoteScan(doc) : { error: "no_scan" };
    },
  };

  const html = fs.readFileSync(templatePath, "utf8");
  const script = html.split("<script>\n")[1].split("</script>")[0];
  return new Function(
    script + "\nreturn { " + EXPORTS.join(", ") + " };")();
}
