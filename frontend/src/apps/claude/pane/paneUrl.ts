// WHAT THE LEFT PANE FRAMES, as a pure decision (T:5219-5470 `paneURL`).
//
// The template did the two fetches (`/api/fs/stat`, `./app.py`) and the decision
// in one async function. Here they are split: the caller fetches (AppPane's
// `usePaneState`), this module decides — so the whole decision tree, including
// the three shapes and the NO-PANE case, is testable with no network and no DOM.
//
// Three shapes and a no-pane case (D235, D239):
//
//   * an APP FOLDER — the app's entry html (its first top-level page carrying
//     `<meta name="fused-app">`, D301, resolved by `./app.py`), noun "project";
//   * a FILE — the file in its OWN default view, exactly what the explorer's
//     preview pane would show it in, with the view SWITCHABLE via `leftmode`;
//   * an ORDINARY FOLDER — NO PANE (D239). `kind: "none"`, not a throw: a
//     folder that is not an app is the ordinary case, and the original throw
//     left a permanent error panel beside a working chat.
//   * CHAT_ONLY — the host owns the pane, so every kind answers `kind: "none"`
//     while still reporting the noun (the placeholders and the system prompt
//     need it either way).
//
// Framed via `/render`, never `/embed`: /embed serves the React shell, which
// nests the target one iframe deeper and puts its document out of the annotation
// layer's reach (T:5248-5258).
import { modeTitle } from "@platform/lib/mode-name";
import { withNoFocus } from "@platform/lib/frame-focus";
import { withPreviewFlag } from "@platform/lib/router";
import type { StatResult, TemplateEntry } from "@platform/lib/api";
import type { AppEntryResponse } from "../protocol/types";

/** The chat mode, which the pane must never frame — framing it would nest the
 *  split view inside its own left pane, recursively (T:5219-5224). */
export const PANE_SKIP_MODES = new Set(["claude"]);

/** What kind of thing the pane is showing. `"none"` is a designed absence. */
export type PaneKind = "project" | "folder" | "file" | "none";

/** The page's ONE answer to "what kind of thing is this chat about" (T:5350).
 *  `""` until the decision lands; the markup's kind-free copy stands until then. */
export type TargetNoun = "" | "project" | "folder" | "file";

/** What to CALL the pane's document, which is a DIFFERENT question from what to
 *  call the target (T:5352-5360): an app folder's pane is the user's own running
 *  app, a file's pane is fused-render's preview OF their file, where "the app"
 *  is a plain lie. "preview" is the kind-free word and is true of both. */
export type PaneNoun = "app" | "preview";

export interface PaneDecisionInput {
  /** `_file` — the chat's target. */
  file: string;
  /** `chat_only=1`: the host has a pane, this document does not (T:5425, 5463). */
  chatOnly: boolean;
  /** `/api/fs/stat?path=<file>` (T:5412). */
  stat: StatResult;
  /** `./app.py {dir: file}` — only consulted for `stat.is_dir` (T:5417). */
  appEntry?: AppEntryResponse | null;
  /** `leftmode` — which offerable view a FILE target opens in (T:5294). */
  leftMode?: string;
  /** Shell-mounted framing flags; see `paneSrcFor`. */
  flags?: PaneSrcFlags;
}

export interface PaneDecision {
  kind: PaneKind;
  /** The iframe src, `null` for `kind: "none"`. */
  src: string | null;
  /** `targetNoun` — one writer for the placeholder, the footnote and the prompt. */
  noun: TargetNoun;
  /** `paneNoun` — what the pane's document is called. */
  paneNoun: PaneNoun;
  /** The switchable views for a FILE target, in stat's own order; the first is
   *  the default. Empty for every other kind: an app folder has exactly one
   *  thing to frame (the entry, which is not a stat entry). */
  leftModes: TemplateEntry[];
  /** The entry html the pane is rendering, for `app_state`'s `entry` field.
   *  `""` for no-pane and CHAT_ONLY: nothing of ours is rendering it (T:5420). */
  entry: string;
  /** stat's remote flag, forwarded to the framed page as `_remote=1`. */
  remote: boolean;
  /** The mode the iframe is showing right now — the idempotence record that
   *  `applyLeftMode` compares against (T:5284). `null` when no pane. */
  framedMode: string | null;
}

/** stat's entries this pane may offer, in stat's own order. Two exclusions: the
 *  chat modes, and `conditional` entries whose gate verdict lives behind
 *  /api/fs/conditions and is deliberately NOT fetched here (CT-12 — an
 *  unresolved gate reads as "not offered"). The first element is therefore the
 *  DEFAULT, which is the shell's own `defaultTemplate` rule (T:5262-5271). */
export function paneOfferable(entries: readonly TemplateEntry[] | undefined): TemplateEntry[] {
  return (entries ?? []).filter((e) => !e.conditional && !PANE_SKIP_MODES.has(e.mode));
}

/** The chosen entry: `leftmode` if it names one that is present and offerable,
 *  else the default. Unknown / no-longer-offered falls back SILENTLY, the same
 *  forgiving rule the shell applies to an unknown `_mode` (SPEC PT-9) — a param
 *  left over from a renamed template, or carried across to a file of another
 *  type, must not turn the pane into an error page (T:5290-5296). */
export function curLeftEntry(
  entries: readonly TemplateEntry[],
  leftMode: string | undefined,
): TemplateEntry | null {
  const want = leftMode || "";
  return entries.find((e) => e.mode === want) ?? entries[0] ?? null;
}

/** Shell-mounted framing flags. The template never needed these — it WAS the
 *  embedded page, so runtime.js had already read them off its own URL. The
 *  native pane is an iframe the SHELL mounts, so it marks the URL itself:
 *  `_nofocus=1` (the embedded-frame focus contract, platform/lib/frame-focus)
 *  and `_preview=1` (skip the server's open recording, platform/lib/router). */
export interface PaneSrcFlags {
  /** `_nofocus=1` — the pane must not pull the keyboard out of the chat. */
  noFocus?: boolean;
  /** `_preview=1` — a shell-mounted view is not the user opening the app. */
  preview?: boolean;
  /** `_noopen=1` — do not record an app OPEN, while staying fully interactive
   *  (D622, the listing pane). Deliberately not `_preview=1`, which would also
   *  disable `fused.daemon.*` for the app this pane frames. */
  noOpen?: boolean;
}

function withFlags(src: string, flags: PaneSrcFlags | undefined): string {
  let out = src;
  // `_preview` BEFORE `_nofocus`, which is the order T spells at its one site
  // that emits both: `paneSrcFor(t, path, remote) + "&_preview=1&_nofocus=1"`
  // (T:10717). Nothing reads either flag positionally, so this is literal
  // parity and not behaviour — but the inventory pins these URLs as an EXACT
  // shape, and a snapshot test or a log grep written to T's spelling misses on
  // a src that reads `&_nofocus=1&_preview=1`.
  if (flags?.preview) out = withPreviewFlag(out);
  if (flags?.noFocus) out = withNoFocus(out);
  if (flags?.noOpen && !/[?&]_noopen=1(&|$)/.test(out)) {
    out += (out.includes("?") ? "&" : "?") + "_noopen=1";
  }
  return out;
}

/**
 * ONE stat entry → the iframe src (T:5298-5321). Split out of the decision so a
 * picker switch re-derives the URL from the entry list it already has, with no
 * re-stat — and so the shot viewer (D616) can frame a template for a file that
 * is NOT the chat's target with the URL shape verbatim, down to the `_remote`
 * hint. One builder, so a preview that works in the pane cannot be subtly
 * different in the viewer.
 *
 * NOTE on the param name: the target rides as `_file`, not `_mode`. `_mode` is
 * the SHELL's URL vocabulary for which view to open a path in; `/render` takes
 * the template's own path plus the file it is rendering.
 *
 * Throws for an offerable non-sentinel entry with no `path`, which stat does not
 * produce today — see `applyLeftMode`'s ordering discipline in useLeftMode.
 */
export function paneSrcFor(
  entry: Pick<TemplateEntry, "mode" | "path">,
  file: string,
  remote = false,
  flags?: PaneSrcFlags,
): string {
  // `_render` is a shell sentinel (PT-12), not a template folder: it means "the
  // file renders itself", which is a bare /render on the file.
  if (entry.mode === "_render") {
    return withFlags("/render?path=" + encodeURIComponent(file), flags);
  }
  if (!entry.path) {
    throw new Error("the default view for this file has no template (" + entry.mode + ")");
  }
  // `_remote=1` is forwarded exactly as the shell's own iframe does, so a
  // template that prefers ranged HTTP reads over local file I/O gets the hint.
  return withFlags(
    "/render?path=" +
      encodeURIComponent(entry.path) +
      "&_file=" +
      encodeURIComponent(file) +
      (remote ? "&_remote=1" : ""),
    flags,
  );
}

/** An app folder's pane: the entry html rendered as itself (T:5427). */
export function appEntrySrc(entry: string, flags?: PaneSrcFlags): string {
  return withFlags("/render?path=" + encodeURIComponent(entry), flags);
}

const NO_PANE: Pick<PaneDecision, "kind" | "src" | "leftModes" | "entry" | "framedMode"> = {
  kind: "none",
  src: null,
  leftModes: [],
  entry: "",
  framedMode: null,
};

/**
 * The whole tree, pure (T:5406-5470). Throws where the template does — "no
 * preview view for this file" (T:5468) — because the caller's catch is
 * `paneReady`'s error panel and the two must stay one behaviour. T's other
 * throw, `if (st.error)` (T:5414), has no equivalent here: `statPath` already
 * raises an HttpError carrying the same message, so the caller never gets a
 * StatResult with an error in it. A folder with no app entry is NOT one of
 * those: it answers `kind: "none"`.
 */
export function decidePane(input: PaneDecisionInput): PaneDecision {
  const { file, chatOnly, stat, appEntry, leftMode, flags } = input;

  if (stat.is_dir) {
    const entry = appEntry?.entry || "";
    if (entry) {
      // An app folder — the system prompt says "project" too (agent.py
      // `_split_system_prompt`, keyed on the same predicate).
      // CHAT_ONLY still reports the NOUN and still withholds `entry`: that field
      // names "the entry html the pane is rendering", and from here nothing of
      // ours is (T:5420-5425).
      if (chatOnly) return { ...NO_PANE, ...nounsFor("project"), remote: false };
      return {
        kind: "project",
        src: appEntrySrc(entry, flags),
        ...nounsFor("project"),
        leftModes: [],
        entry,
        remote: !!stat.remote,
        framedMode: null,
      };
    }
    // An ordinary folder, not a project: NO PANE (D239). A distinct answer
    // rather than a thrown error.
    return { ...NO_PANE, ...nounsFor("folder"), remote: false };
  }

  // CHAT_ONLY is checked BEFORE the entry lookup below, on purpose: the "no
  // preview view for this file" throw is about a pane WE have to fill, and in
  // this layout there is no pane to fail at filling (T:5457-5463).
  if (chatOnly) return { ...NO_PANE, ...nounsFor("file"), remote: false };

  const leftModes = paneOfferable(stat.templates);
  const remote = !!stat.remote;
  const t = curLeftEntry(leftModes, leftMode);
  if (!t) throw new Error("no preview view for this file");
  return {
    kind: "file",
    src: paneSrcFor(t, file, remote, flags),
    ...nounsFor("file"),
    leftModes,
    // The target IS the document being annotated, so app_state reports the file
    // itself where a folder target reports the app's entry page (T:5466).
    entry: file,
    remote,
    framedMode: t.mode,
  };
}

// ── the nouns: ONE writer for every piece of chrome that names the target ──
//
// T routes the placeholder, the footnote and the pane chrome through
// `setTargetNoun` / `applyPaneNoun` (T:5323-5411) precisely because a second,
// independent kind lookup is a second thing to forget — which is how a chat
// about `notes.md` came to read "files in this project". Here the same
// single-writer rule is kept by making every string a function OF the noun, so
// no component re-derives the kind.

function nounsFor(noun: TargetNoun): { noun: TargetNoun; paneNoun: PaneNoun } {
  return { noun, paneNoun: paneNounFor(noun) };
}

/** T:5406. "folder" never reaches the pane chrome (that target has no pane), but
 *  it resolves to the kind-free word rather than to nothing. */
export function paneNounFor(noun: TargetNoun): PaneNoun {
  return noun === "project" ? "app" : "preview";
}

/** The home composer's placeholder (T:5392). The markup's own kind-free "Ask
 *  Claude…" stands while the noun is still `""`. */
export function homePlaceholderFor(noun: TargetNoun): string {
  return noun ? "Ask Claude about this " + noun + "…" : "Ask Claude…";
}

/** The footnote's kind-dependent first sentence (T:5399-5403). A file is the
 *  subject itself, so "files in this file" is not a sentence — and "and the
 *  assets it references" is exactly the reach `_system_prompt` gives the agent
 *  for a file target. The second sentence is its own element (fitFootnote). */
export function footnoteFor(noun: TargetNoun): string {
  if (noun === "file") return "Claude can read and edit this file and the assets it references.";
  if (!noun) return "Claude can read and edit files here.";
  return "Claude can read and edit files in this " + noun + ".";
}

/** The annotate switch's idle spoken name (T:5377). PR3 owns the armed names —
 *  `applyPaneNoun` is gated on `!annOn` so a noun resolving mid-mode never
 *  overwrites the Done face (Bugbot PR #665). */
export function annotateLabelFor(paneNoun: PaneNoun): string {
  return "Comment on the " + paneNoun;
}

/** The IDLE Comment seat's TOOLTIP (T:7505-7508 `annIdleTitle`), beside its
 *  spoken name above because the two are the same fact about the same noun.
 *
 *  T extracted this into a function *because* the literal had two writers —
 *  `applyPaneNoun`, when the target's kind resolves, and `annSetMode`, on every
 *  disarm — and the first toggle-off threw away the kind-correct noun the
 *  former had just written. React re-derives it from `paneNoun` on every
 *  render, so that live bug cannot come back here; the helper exists so a
 *  second writer cannot re-open the drift T closed by hand, and so the armed
 *  sentence (`ANN_ARMED_TITLE`, ann/types) and the idle one sit at one
 *  altitude instead of one being an export and the other a literal in JSX. */
export function annIdleTitleFor(paneNoun: PaneNoun): string {
  return "Comment on the " + paneNoun + ", then send the notes to Claude";
}

/** The screenshot button's one sentence, leading with the VERB (T:5382-5386). */
export function shotLabelFor(paneNoun: PaneNoun): string {
  return "Screenshot the " + paneNoun + " and attach it to this message";
}

// ── the picker's labels and icons ──

/**
 * The label the picker shows for a mode (T:5478-5500). `git`/`app`/`_app` are
 * named for what the shell calls the SURFACE rather than for the registry key:
 * by the time this label is on screen the user is already inside the app, so
 * "App" says nothing — it is the app's own preview, as against its history.
 *
 * DEVIATION from T, deliberate: the fall-through is `modeTitle`
 * (platform/lib/mode-name) rather than T's four-line bare capitalize. T's own
 * comment says the labels "must read the same as the explorer's mode switcher
 * does for the same file", and the switcher calls `modeTitle` — which the
 * template could not import, being a standalone document. So this gets "DuckDB"
 * and "Log studio" where T got "Duckdb" and "Log-studio".
 */
export function paneModeLabel(mode: string): string {
  const named: Record<string, string> = { git: "Source Control", app: "Preview", _app: "Preview" };
  return named[mode] ?? modeTitle(mode);
}

/** The template's own icon.svg, which stat already resolved to an absolute path
 *  on the entry (`_icon_for`, PT-11) — served by /api/fs/raw and drawn as a MASK
 *  filled with currentColor, exactly what the shell does with the same file
 *  (`templateModeIcon`). No icon (a template folder that ships none, and the
 *  `_render` sentinel, which is not a folder) → the shell's own fallback, a
 *  lettered box, rather than a hole in the column (T:5502-5526). */
export function paneModeIconUrl(icon: string | null | undefined): string | null {
  if (!icon) return null;
  return 'url("/api/fs/raw?path=' + encodeURIComponent(icon) + '")';
}

/** The lettered-box fallback's letter. */
export function paneModeLetter(mode: string): string {
  return paneModeLabel(mode).charAt(0).toUpperCase();
}

/** Why there is nothing to say about the app. ONE const because two callers say
 *  it — the snapshot itself and the pull channel's fallback answer — and a
 *  divergence would tell the model two different stories about one condition
 *  (T:4803-4822). It enumerates CAUSES, so it may only name conditions that can
 *  actually produce it: "a project may have no app entry" is NOT one, because
 *  D239 gives that target no pane and therefore no `app_state` tool at all. */
export const APP_STATE_UNREADABLE =
  "the left pane's document could not be read (a file may have no preview " +
  "view, or the pane has not finished loading), so nothing is known about its " +
  "title, url or DOM right now";
