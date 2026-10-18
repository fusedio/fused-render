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
// MD-13: the markdown template's Live Preview is a decoration set over the CM6
// document — the same mechanism Obsidian's is — so it needs the widget/plugin
// primitives (WidgetType, ViewPlugin, MatchDecorator), a keymap to hang the
// editing behaviours off, selection/range builders, syntaxTree to ask the
// markdown grammar what a range IS, and the completion API for the `[[` popup.
import { WidgetType, ViewPlugin, MatchDecorator, keymap } from "@codemirror/view";
import { EditorSelection, RangeSetBuilder, Prec } from "@codemirror/state";
import { syntaxTree, HighlightStyle, syntaxHighlighting } from "@codemirror/language";
// MD-18a/6b: `tags` is what a HighlightStyle's rules are keyed on — there is
// no way to define one without it, even though only HighlightStyle and
// syntaxHighlighting were the named gap. Needed for the light-mode fence
// HighlightStyle built from this view's own tokens (oneDark ships its own).
import { tags } from "@lezer/highlight";
// `acceptCompletion` because Tab has to take the open completion before it
// indents (MD-18): CM's own completionKeymap binds only Enter to it.
import { autocompletion, acceptCompletion } from "@codemirror/autocomplete";
import { indentMore, indentLess } from "@codemirror/commands";
// GFM is what `markdownLanguage` adds over the CommonMark base (tables, task
// lists, strikethrough, autolinks), so the template passes it as the base.
// `markdownKeymap` is Obsidian's smart-list behaviour already written: Enter
// continues a list marker and renumbers, Backspace deletes back through one.
import { markdown, markdownLanguage, markdownKeymap } from "@codemirror/lang-markdown";
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
// CE-*: the code editor's full feature set (SPEC §51). More first-class Lezer
// grammars, the lint/search APIs it drives from its own status bar, the
// Compartment its live toggles (language, wrap, indent, theme) reconfigure
// through, and legacy modes for the rest. C/C++/Java/Rust/Go deliberately ride
// the legacy modes rather than their Lezer packages: those grammars are ~300 KB
// minified together, and this one bundle is also loaded by the markdown and
// notebook views, which never use them.
import { Compartment } from "@codemirror/state";
import { indentWithTab } from "@codemirror/commands";
import { indentUnit } from "@codemirror/language";
import { completeAnyWord } from "@codemirror/autocomplete";
import { gotoLine } from "@codemirror/search";
import { linter, lintGutter, openLintPanel, forEachDiagnostic } from "@codemirror/lint";
import { jsonParseLinter } from "@codemirror/lang-json";
import { xml } from "@codemirror/lang-xml";
import { sql } from "@codemirror/lang-sql";
import { sass } from "@codemirror/lang-sass";
import { less } from "@codemirror/lang-less";
import { indentationMarkers } from "@replit/codemirror-indentation-markers";
import { properties } from "@codemirror/legacy-modes/mode/properties";
import { powerShell } from "@codemirror/legacy-modes/mode/powershell";
import { dockerFile } from "@codemirror/legacy-modes/mode/dockerfile";
import { diff } from "@codemirror/legacy-modes/mode/diff";
import { lua } from "@codemirror/legacy-modes/mode/lua";
import { ruby } from "@codemirror/legacy-modes/mode/ruby";
import { r } from "@codemirror/legacy-modes/mode/r";
import { swift } from "@codemirror/legacy-modes/mode/swift";
import { c, cpp, java, kotlin, csharp, scala, dart, objectiveC } from "@codemirror/legacy-modes/mode/clike";
import { rust } from "@codemirror/legacy-modes/mode/rust";
import { go } from "@codemirror/legacy-modes/mode/go";
import { groovy } from "@codemirror/legacy-modes/mode/groovy";
import { stex } from "@codemirror/legacy-modes/mode/stex";
import { perl } from "@codemirror/legacy-modes/mode/perl";
import { nginx } from "@codemirror/legacy-modes/mode/nginx";
import { cmake } from "@codemirror/legacy-modes/mode/cmake";
import { protobuf } from "@codemirror/legacy-modes/mode/protobuf";
import { julia } from "@codemirror/legacy-modes/mode/julia";
import { haskell } from "@codemirror/legacy-modes/mode/haskell";

// Legacy (CodeMirror-5-style) grammars wrapped as CM6 languages. Kept as ready
// EditorState extensions so the template treats them like the first-class
// language functions above.
const shellLang = StreamLanguage.define(shell);
const tomlLang = StreamLanguage.define(toml);

// CE-*: raw legacy mode specs, NOT pre-wrapped — the code template wraps the
// one a file needs with StreamLanguage.define on demand, so a bundle load does
// not build a language object for every grammar nobody opened.
const legacyModes = {
  shell, toml, properties, powerShell, dockerFile, diff, lua, ruby, r, swift,
  c, cpp, java, kotlin, csharp, scala, dart, objectiveC, rust, go, groovy,
  stex, perl, nginx, cmake, protobuf, julia, haskell,
};

export {
  EditorView,
  EditorState,
  basicSetup,
  // AN-23: annotate-mode selection decorations.
  Decoration,
  StateField,
  StateEffect,
  RangeSet,
  // MD-13: markdown Live Preview / editor behaviours.
  WidgetType,
  ViewPlugin,
  MatchDecorator,
  keymap,
  EditorSelection,
  RangeSetBuilder,
  Prec,
  syntaxTree,
  HighlightStyle,
  syntaxHighlighting,
  tags,
  autocompletion,
  acceptCompletion,
  indentMore,
  indentLess,
  markdown,
  markdownLanguage,
  markdownKeymap,
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
  // CE-*: code editor.
  Compartment,
  indentWithTab,
  indentUnit,
  completeAnyWord,
  gotoLine,
  linter,
  lintGutter,
  openLintPanel,
  forEachDiagnostic,
  jsonParseLinter,
  xml,
  sql,
  sass,
  less,
  indentationMarkers,
  legacyModes,
};
