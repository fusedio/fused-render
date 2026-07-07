// Entry point bundled into fused_render/templates/vendor/codemirror.bundle.js.
// esbuild wraps these exports in an IIFE assigned to the global `CM`, so the
// template can do `CM.EditorView`, `CM.python()`, etc. with no module loader.
// Everything code/template.html needs must be re-exported here — anything not
// referenced would be tree-shaken out of the bundle.
import { EditorView, basicSetup } from "codemirror";
// AN-23: selection-anchor decorations for annotate mode (SPEC §17.5). Decoration
// lives in @codemirror/view; StateField/StateEffect/RangeSet in @codemirror/state.
import { Decoration } from "@codemirror/view";
import { EditorState, StateField, StateEffect, RangeSet } from "@codemirror/state";
import { python } from "@codemirror/lang-python";
import { javascript } from "@codemirror/lang-javascript";
import { json } from "@codemirror/lang-json";
import { yaml } from "@codemirror/lang-yaml";
import { html } from "@codemirror/lang-html";
import { css } from "@codemirror/lang-css";
import { StreamLanguage } from "@codemirror/language";
import { shell } from "@codemirror/legacy-modes/mode/shell";
import { toml } from "@codemirror/legacy-modes/mode/toml";
import { oneDark } from "@codemirror/theme-one-dark";

// Legacy (CodeMirror-5-style) grammars wrapped as CM6 languages. Kept as ready
// EditorState extensions so the template treats them like the first-class
// language functions above.
const shellLang = StreamLanguage.define(shell);
const tomlLang = StreamLanguage.define(toml);

export {
  EditorView,
  EditorState,
  basicSetup,
  // AN-23: annotate-mode selection decorations.
  Decoration,
  StateField,
  StateEffect,
  RangeSet,
  python,
  javascript,
  json,
  yaml,
  html,
  css,
  StreamLanguage,
  shellLang,
  tomlLang,
  oneDark,
};
