// Runs the markdown template's Live Preview decoration builder (MD-18a) for
// real, headlessly, and prints the resulting decoration set as JSON.
//
// Why this exists here rather than as an assertion about the template source:
// buildDecorations is the one piece of this template whose correctness is not
// visible in a diff. It depends on what the vendored markdown grammar calls
// each range, and the grammar has already been wrong twice in ways no source
// assertion would have caught — `---\ntitle: x\n---` parses as a
// HorizontalRule plus a SetextHeading2 (not frontmatter), and the inner
// brackets of `[[Wiki]]` parse as a Link node, which an over-broad "is this
// code?" guard silently treated as un-linkable. Both were found by running it.
//
// CM also *throws* on a decoration set with overlapping replacements, so simply
// building the set is itself the check that the reveal rule, the tree walk and
// the two regex passes cannot collide.
//
// Usage: node live-preview-probe.mjs <template.html> <doc.md> [caretPos] [optsJson]
//
// optsJson (all optional):
//   {"scanned": true}       graph.py answered with a real scan, so every link
//                           target's resolution is KNOWN. Without it the stub
//                           answers `{error: "no_scan"}` — what a mount-backed
//                           root, a refused scan or a failed one produces — and
//                           resolution is UNKNOWN, which must not render as
//                           "missing" (MD-11).
//   {"params": {"edit":"0"}} what fused.params.get returns, so a mode held in a
//                           param (MD-20) can be driven from here.
//
// Each widget's DOM is built and reported too (class, title, dataset, text):
// what a wikilink renders AS is the behaviour, and it is not visible in either
// the decoration range or the widget key.
//
// Must run from this directory — module resolution needs ./node_modules.
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

// The template runs top-level DOM and runtime code before it defines anything.
// These stubs exist only to let it finish loading; nothing under test touches
// them, and `_file` comes back empty so load() bails immediately.
const stub = () => new Proxy({}, {
  get(_target, key) {
    if (key === "classList") return { toggle() {}, add() {}, remove() {} };
    if (key === "dataset" || key === "style") return {};
    if (key === "textContent" || key === "innerHTML" || key === "value") return "";
    return () => {};
  },
  set() { return true; },
});

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
// Enough of an element for a widget builder to fill in and for us to read back.
// Not a DOM: no layout, no events, no tree semantics beyond appendChild.
function fakeElement(tag) {
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

globalThis.document = {
  getElementById: stub, createElement: fakeElement, addEventListener() {},
  querySelectorAll: () => [], documentElement: { getAttribute: () => "dark" },
  visibilityState: "visible",
};
globalThis.window = { addEventListener() {}, top: { location: { pathname: "/view/x" } } };
globalThis.MutationObserver = class { observe() {} disconnect() {} };
globalThis.sessionStorage = { getItem: () => null, setItem() {} };
globalThis.fusedRoBadge = { update() {} };
globalThis.fusedGraph = { create: () => ({ setData() {}, nudge() {} }) };
const [templatePath, docPath, caretArg, optsArg] = process.argv.slice(2);
const opts = JSON.parse(optsArg || "{}");
const doc = fs.readFileSync(docPath, "utf8");

// A stand-in for graph.py's `note` answer, so the SCANNED state can be probed
// too — the unscanned one is the default because that is what a mount gives.
// The rule is deliberately crude and local to this harness: every `[[target]]`
// in the document resolves, except one whose name says it should not. Real
// resolution is graph.py's and is tested against graph.py (MD-6).
const UNRESOLVED_HERE = /ghost|missing|gone/i;
function fakeNoteScan() {
  const links = [];
  for (const match of doc.matchAll(/!?\[\[([^\[\]\n]+?)\]\]/g)) {
    const target = match[1].split("|")[0].split("#")[0].trim();
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

globalThis.fused = {
  autoReload() {},
  rawUrl: (p) => "/api/fs/raw?path=" + p,
  params: {
    get: (name) => (opts.params || {})[name] || "",
    getAll: () => opts.params || {},
    set() {}, onChange() {},
  },
  async stat() { return { mtime: 1, size: 1, writable: true }; },
  async readFile() { return ""; },
  async writeFile() { return { mtime: 2 }; },
  async runPython(_py, args) {
    if (!opts.scanned) return { error: "no_scan", message: "not scanned here" };
    return args.action === "note" ? fakeNoteScan() : { error: "no_scan" };
  },
};

const html = fs.readFileSync(templatePath, "utf8");
const script = html.split("<script>\n")[1].split("</script>")[0];
const { buildDecorations, refreshLinks } = new Function(
  script + "\nreturn { buildDecorations, refreshLinks };")();

// The template's own load() bailed (no `_file`), so the link facts are asked for
// explicitly here — through the real refreshLinks, so the page's own
// error-vs-payload branch is what decides between known and unknown.
if (opts.scanned) await refreshLinks();

const caret = Math.min(Number(caretArg || 0), doc.length);
const state = EditorState.create({
  doc,
  extensions: [markdown({ base: markdownLanguage })],
  selection: EditorSelection.single(caret),
});
ensureSyntaxTree(state, doc.length, 10000);

// Building the set at all is the overlap/sort check: Decoration.set rejects
// replacements that overlap, whatever order they were added in.
//
// A STATE, not a view: the builder is registered as a StateField, because a
// ViewPlugin may not provide a replacement spanning a line break and the table
// widget does exactly that (see the template). Passing the state here is what
// keeps this harness honest about the shape the template actually uses.
const set = buildDecorations(state);

// A widget's toDOM only needs a view for taskWidget's click handler, which is
// never fired here.
const fakeView = { state, posAtDOM: () => 0, dispatch() {} };

function widgetDom(widget) {
  const node = widget.toDOM(fakeView);
  return {
    tag: (node.tagName || "").toLowerCase(),
    cls: node.className || "",
    title: node.title || "",
    data: node.dataset || {},
    text: node.textContent || "",
    src: node.src || "",
    href: node.href || "",
  };
}

const decorations = [];
for (const iter = set.iter(); iter.value; iter.next()) {
  const spec = iter.value.spec;
  // A line decoration also carries `class`, and is the only zero-length one.
  const kind = spec.widget ? "widget"
    : iter.from === iter.to ? "line"
    : spec.class ? "mark" : "hide";
  decorations.push({
    from: iter.from,
    to: iter.to,
    kind,
    cls: spec.class || (spec.widget ? spec.widget.key : null),
    text: doc.slice(iter.from, iter.to),
    dom: spec.widget ? widgetDom(spec.widget) : null,
  });
}
process.stdout.write(JSON.stringify(
  { caret, scanned: opts.scanned === true, decorations }, null, 1));
