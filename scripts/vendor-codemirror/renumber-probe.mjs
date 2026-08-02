// Drives the markdown template's ordered-list renumbering (MD-25) for real,
// headlessly, and prints the resulting document as JSON.
//
// Why a probe and not a source assertion: renumbering is a behaviour of the
// whole editing pipeline, not of one function. It hangs off a
// transactionFilter, so what actually has to be true is "make this edit and the
// numbers come out right" — and the things most likely to be wrong (does the
// filter see a cut? does it run after markdownKeymap's Enter already renumbered?
// does an undo escape it?) are only visible by dispatching a real transaction
// into a real EditorState with the real extensions.
//
// Usage: node renumber-probe.mjs <template.html> <doc.md> <editJson>
//
// editJson describes one dispatch against the document:
//   {"changes": [{"from":n,"to":n,"insert":"…"}], "userEvent": "delete.selection"}
//   {"key": "Enter", "at": n}   press a key through the real keymap at pos n
//   {"undo": true, ...}         make the edit, then undo it
//
// Must run from this directory — module resolution needs ./node_modules.
import fs from "node:fs";
import { EditorState, EditorSelection, Text, Prec } from "@codemirror/state";
import { keymap, EditorView } from "@codemirror/view";
import { markdown, markdownLanguage, markdownKeymap } from "@codemirror/lang-markdown";
import { history, undo, redo, historyKeymap } from "@codemirror/commands";
import { loadTemplateScript } from "./template-harness.mjs";

const [templatePath, docPath, editArg] = process.argv.slice(2);
const edit = JSON.parse(editArg || "{}");
const doc = fs.readFileSync(docPath, "utf8");

const { renumberFilter, renumberOrderedLists, editorKeymap } =
  loadTemplateScript(templatePath, { doc });

const state = EditorState.create({
  doc,
  extensions: [
    markdown({ base: markdownLanguage }),
    history(),
    Prec.high(keymap.of(markdownKeymap.concat(editorKeymap))),
    keymap.of(historyKeymap),
    renumberFilter,
  ],
  selection: edit.selection
    ? EditorSelection.single(edit.selection[0], edit.selection[1])
    : EditorSelection.single(edit.at || 0),
});

// A real view is what delivers a keypress to a keymap, and CM has no headless
// one — so commands are run against a minimal object carrying the two things a
// keymap command uses: the state, and a dispatch that advances it.
let current = state;
const view = {
  get state() { return current; },
  dispatch(...specs) {
    for (const spec of specs) current = current.update(spec).state;
  },
};

if (edit.key) {
  const binding = markdownKeymap.concat(editorKeymap)
    .find((b) => b.key === edit.key);
  if (!binding) throw new Error("no binding for " + edit.key);
  binding.run(view);
} else if (edit.changes) {
  view.dispatch({
    changes: edit.changes,
    userEvent: edit.userEvent || "input.type",
  });
}
if (edit.undo) undo(view);
if (edit.redo) redo(view);

// Also reported: what the pure function alone says about the untouched
// document, so a test can separate "the rule is wrong" from "the filter never
// ran".
const pure = renumberOrderedLists(Text.of(doc.split("\n")), 1, doc.split("\n").length);

process.stdout.write(JSON.stringify({
  doc: current.doc.toString(),
  selection: [current.selection.main.anchor, current.selection.main.head],
  pureChanges: pure,
}, null, 1));
