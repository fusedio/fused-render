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
// Usage: node live-preview-probe.mjs <template.html> <doc.md> [caretPos]
// Must run from this directory — module resolution needs ./node_modules.
import fs from "node:fs";
import {
  EditorState, RangeSetBuilder, EditorSelection, StateEffect, Prec,
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
  keymap, EditorSelection, RangeSetBuilder, Prec, StateEffect, syntaxTree,
  autocompletion, indentMore, indentLess, markdown, markdownLanguage,
  markdownKeymap, basicSetup: [], oneDark: [],
};
globalThis.document = {
  getElementById: stub, createElement: stub, addEventListener() {},
  querySelectorAll: () => [], documentElement: { getAttribute: () => "dark" },
  visibilityState: "visible",
};
globalThis.window = { addEventListener() {}, top: { location: { pathname: "/view/x" } } };
globalThis.MutationObserver = class { observe() {} disconnect() {} };
globalThis.sessionStorage = { getItem: () => null, setItem() {} };
globalThis.fusedRoBadge = { update() {} };
globalThis.fusedGraph = { create: () => ({ setData() {}, nudge() {} }) };
globalThis.fused = {
  autoReload() {},
  rawUrl: (p) => "/api/fs/raw?path=" + p,
  params: { get: () => "", getAll: () => ({}), set() {}, onChange() {} },
  async stat() { return { mtime: 1, size: 1, writable: true }; },
  async readFile() { return ""; },
  async writeFile() { return { mtime: 2 }; },
  async runPython() { return { error: "no_scan" }; },
};

const [templatePath, docPath, caretArg] = process.argv.slice(2);
const html = fs.readFileSync(templatePath, "utf8");
const script = html.split("<script>\n")[1].split("</script>")[0];
const { buildDecorations } = new Function(
  script + "\nreturn { buildDecorations };")();

const doc = fs.readFileSync(docPath, "utf8");
const caret = Math.min(Number(caretArg || 0), doc.length);
const state = EditorState.create({
  doc,
  extensions: [markdown({ base: markdownLanguage })],
  selection: EditorSelection.single(caret),
});
ensureSyntaxTree(state, doc.length, 10000);

// Building the set at all is the overlap/sort check: Decoration.set rejects
// replacements that overlap, whatever order they were added in.
const set = buildDecorations({ state, visibleRanges: [{ from: 0, to: doc.length }] });

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
  });
}
process.stdout.write(JSON.stringify({ caret, decorations }, null, 1));
