// Which listing action a key event MEANS — the pure half of
// useListingShortcuts, so the chord table can be tested without a DOM or a
// React renderer (the frontend test setup has neither). The tests live in
// shortcut-chord.test.ts, which also keeps the source guard on the reload
// chord that used to be this table's only test.
//
// THE RULE THIS FILE EXISTS TO ENFORCE: a chord matches on its EXACT modifiers,
// never on "at least these". The hook used to test `mod && key === "c"` against
// a lowercased `e.key`, so Ctrl+SHIFT+C matched — and Ctrl+Shift+C is devtools'
// inspect-element on Linux/Windows and the terminal copy chord besides. The
// handler took it and called preventDefault, so the browser binding never ran
// and the user's inspector stopped opening. Every entry below therefore spells
// out the modifiers it does NOT want, and anything unmatched returns null so the
// caller leaves the event alone for whoever else wants it.
import { isMac, isMod } from "@platform/lib/platform";

export type ChordAction =
  | "copy"
  | "cut"
  | "paste"
  | "duplicate"
  | "open"
  | "parent"
  | "back"
  | "forward"
  | "new-folder"
  | "trash"
  | "rename";

// The parts of a KeyboardEvent this decision reads. An interface rather than
// KeyboardEvent so a test can hand it a plain object — and so the matcher
// cannot reach for anything stateful.
export interface ChordEvent {
  key: string;
  metaKey: boolean;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
}

export interface ChordContext {
  // Whether focus is in the listing's search input. Several chords mean
  // something else entirely while typing (Cmd+C is text copy, Cmd+Backspace is
  // "clear to start of line" on macOS), so they resolve to no action at all
  // rather than being matched and then declined.
  inSearch: boolean;
  // Whether the user has TEXT selected anywhere on the page. The same argument
  // as `inSearch`, for the case that has no input box: this handler is bound on
  // `document`, and clicking a plain <span> leaves activeElement as <body>, so
  // the "is the listing active" guard passes while the user is looking at text
  // they just highlighted somewhere else entirely. Cmd+C then matched
  // copy-the-file-path and called preventDefault, and the browser's own copy
  // never ran — which is why an error message in the download manager could be
  // selected and never copied. Rows are `user-select: none`, so a real text
  // selection is never the listing's own.
  hasSelection: boolean;
}

// Platform is NOT a parameter. `isMod`/`isMac` are the app's one detection and
// one exclusive primary-modifier test (see lib/platform), and re-deriving
// either from an argument would be a second copy of the rule that file exists
// to keep single. The cost is that the mac-only branches below cannot be driven
// from a test process that reports no navigator — stated in the test file.
export function matchChord(e: ChordEvent, ctx: ChordContext): ChordAction | null {
  const key = e.key.toLowerCase();
  // Exactly the primary modifier — no Shift, no Alt. This is the whole fix.
  const chord = isMod(e) && !e.shiftKey && !e.altKey;
  // Primary + Shift and nothing else (the new-folder chord).
  const shiftChord = isMod(e) && e.shiftKey && !e.altKey;
  // No modifier at all. Raw flags rather than `!isMod(e)`, because isMod is
  // EXCLUSIVE: on Linux it is false while Super is held, so `!isMod(e)` would
  // let Super+Delete through to a destructive branch.
  const bare = !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey;

  if (chord && (key === "c" || key === "x" || key === "v")) {
    // With focus in the search box these keep their native text-clipboard
    // meaning; only the non-text chords stay live there.
    if (ctx.inSearch) return null;
    // Selected text wins Cmd+C, wherever it is. Copying what you highlighted is
    // unambiguous and it is the browser's own meaning, so the listing does not
    // get to reinterpret it as "copy the selected file's path". Only COPY:
    // a non-editable selection cannot be cut, so Cmd+X keeps meaning the
    // listing's cut, and paste never had a text reading here at all.
    if (key === "c" && ctx.hasSelection) return null;
    return key === "c" ? "copy" : key === "x" ? "cut" : "paste";
  }
  if (chord && key === "d") return "duplicate";
  // Open the lead row — the same gesture as Enter (macOS Cmd+Down).
  if (chord && e.key === "ArrowDown") return "open";
  if (chord && e.key === "ArrowUp") return "parent";
  // Back / forward. Bare Mod only: Cmd+Shift+[ / ] are the browser's own
  // tab-switch chords on macOS, and this handler was stealing them.
  if (chord && e.key === "[") return "back";
  if (chord && e.key === "]") return "forward";
  if (shiftChord && key === "n") return "new-folder";
  // macOS trash chord. Windows/Linux use the Delete key instead. Never while
  // typing in the search box: Cmd+Delete is the standard macOS "clear to start
  // of line" chord, so trashing there would be a foot-gun.
  if (chord && e.key === "Backspace") {
    return isMac && !ctx.inSearch ? "trash" : null;
  }
  if (bare && e.key === "Backspace") {
    // Windows/Linux: bare Backspace goes up a folder. On macOS it must stay
    // inert — Cmd+Backspace is trash there, and a bare Backspace navigating
    // away would be a foot-gun. Never while typing in the search box.
    return !isMac && !ctx.inSearch ? "parent" : null;
  }
  if (bare && e.key === "Delete") {
    // Windows/Linux trash key. On macOS the Delete (⌦) key is not the trash
    // gesture — Cmd+Backspace above is. Shift+Delete is Windows Explorer's
    // "delete permanently", which this must not answer with an ordinary trash.
    return !isMac && !ctx.inSearch ? "trash" : null;
  }
  // Rename. Bare F2 only — Alt+F2 is a desktop-wide run dialog on Linux.
  if (bare && e.key === "F2") return "rename";
  return null;
}
