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
import { EditorState, EditorSelection } from "@codemirror/state";
import { ensureSyntaxTree } from "@codemirror/language";
import { markdown, markdownLanguage } from "@codemirror/lang-markdown";
// Browser and `fused` stubs, and the trick that reaches a function the template
// never exported. Shared with renumber-probe.mjs.
import { loadTemplateScript } from "./template-harness.mjs";

const [templatePath, docPath, caretArg, optsArg] = process.argv.slice(2);
const opts = JSON.parse(optsArg || "{}");
const doc = fs.readFileSync(docPath, "utf8");

const { buildDecorations, refreshLinks } = loadTemplateScript(templatePath, {
  doc, scanned: opts.scanned === true, params: opts.params || {},
});

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
    // A mark can carry its own element and attributes (`tagName`/`attributes`),
    // which is how a range becomes an actual anchor without replacing the text
    // under it. Reported for the same reason a widget's DOM is: what a mark
    // renders AS is the behaviour, and the range alone does not say.
    tag: spec.tagName || null,
    attrs: spec.attributes || null,
  });
}
process.stdout.write(JSON.stringify(
  { caret, scanned: opts.scanned === true, decorations }, null, 1));
