# SPEC: omnibox search affordance (E+F)

Branch: `worktree-omnibox-search-affordance`. `worktree-omnibox-chip-padding`
(PR #1091) has since merged to main — this is now a regular PR against main,
not a stacked one. (It was stacked when this file was first written; that line
is stale, not this one.)

## The problem this solves

The explorer omnibox is both a path bar and a search box. It always arrives
pre-filled with the current folder's path (a **hard requirement** from the user:
the placeholder is only ever visible after you delete the text). So it looks
exactly like a plain path bar, and there is no obvious way to learn that you can
search in it. The words "Search ⌘L" were the only signal, and they were
unclickable text.

Four things sit on that one line today, each drawn in a different style: `Path`
(a label with no outline), `Search ⌘L` (a tip wearing an outline), `★` (a button
on a filled tile), and the path itself (content). They read as equals when they
are nothing alike.

## The rule

**A container means an action.** Only pressable things get an outline or a
filled tile. Labels and content wear nothing.

## Scope

### 1. The mode chip loses its word

`Path` / `Search` text goes. The 12px folder / magnifier glyph stays and carries
the mode alone. A folder icon in front of a path already says "path".

Accessibility is a hard requirement here, not a nice-to-have (see Risks):
- the mode must have a **spoken label** for screen readers (visually-hidden text
  or an `aria-label`), because the visible word is gone;
- the mode must be distinguishable by **more than colour** (the glyph shape
  already does this - keep both glyphs clearly different, do not use the same
  glyph tinted two ways).

### 2. `--chip-inset` collapses

PR #1091 introduced `--chip-inset` set per mode on `.listing-search-box`,
because "Path" and "Search" are different widths. An icon is the same width
either way, so the per-mode split is no longer needed. Collapse it to one value.

Keep the custom property itself rather than re-typing the number in three
places: `.listing-search-crumbs`, `.listing-search-input` and
`.listing-completion-row` all read it, and that is exactly why they cannot drift
apart. Just stop varying it by mode, and drop the now-dead `.search` modifier
if nothing else uses it.

Recompute the value for the icon-only chip. Do not leave the old text-derived
number in place - that is the whitespace bug #1091 exists to fix, reintroduced.

### 3. Search becomes a button (variant F) - REVISED, words stay

**Revised on a running-screen review**: the user compared an icon-only
magnifier button against the original "Search ⌘L" text and preferred the
words. Do not rebuild the icon-only version from this line - the reasoning
below is the FINAL shape.

The `.listing-search-shortcut-hint` idle text ("Search ⌘L") becomes a real
pressable button, on the same `bar-ctl` chassis family the neighbouring `⋮`
trigger rides, with the words **as its content** - not a glyph, and not a
bespoke outlined pill (that reproduces the original complaint this whole
branch exists to fix: four things on one line in four disagreeing styles).
The defect was never the words; it was that they wore no chassis and caught
no click.

- The visible label is "Search" plus the platform-conditional shortcut
  (⌘L / Ctrl L), exactly as the original hint rendered it. The shortcut does
  **not** move into the tooltip. Tooltip copy is `Search this folder`.
- At narrow widths, text costs space a glyph doesn't. Reuse
  `SearchField.tsx`'s existing `HINT_WIDE_PX` measurement (already driving the
  `HINT_LONG`/`HINT_SHORT` placeholder switch) rather than a second
  breakpoint: at or above the threshold, render the words; below it, collapse
  to the bare magnifier glyph (the one prior icon-only work already built -
  keep it, demoted to this narrow-width fallback rather than discarded).
- The accessible name (`aria-label`) must include **both** "Search" and the
  shortcut in **both** forms, wide and collapsed - in the collapsed form that
  is the only place the shortcut appears at all, so it is load-bearing there.
- Pressing it must do what `⌘L` does today. Find that existing handler and reuse
  it - do not write a second path to the same behaviour.
- Keep the platform-conditional shortcut label that the existing hint renders
  (⌘ vs Ctrl); do not hardcode one.
- Item 1 (the icon-only left chip) is unchanged, and that pairing is now the
  reason this design works: an icon on the left carries the mode, words on the
  right carry the action, and the "two magnifiers" risk this spec originally
  flagged as its riskiest unverifiable detail (Cannot Be Verified Headlessly,
  below) simply stops existing - there is only ever one magnifier glyph
  rendered at a time.

### 4. The dropdown does the telling (variant E)

This is the one genuinely new piece of machinery, not a restyle: the completion
list has only ever offered folder names to complete. It gains **action rows**.

- Typing a name offers search **first, in words**: `Search this folder for
  "<query>"` with a `↵` affordance, above the folder completions.
- A path-shaped query that does not resolve gets a warning row, `No folder at
  <path>`, above an offer to search for it instead. The message text already
  exists as `pathNotFoundMessage()` in `listing/enter-prompt.ts` - reuse it.
- The guidance currently in the placeholder (`HINT_LONG` / `HINT_SHORT` in
  `SearchField.tsx`, "Search, or type a path or pattern like ~/work/*/*.csv")
  moves into the dropdown, where it can be read **without emptying the box**.
  Keep the placeholder attribute too - it costs nothing and still serves the
  emptied-box case.

### 5. Hint consolidation: what moves and what stays

Five places explain a keypress today. Four move into the dropdown; one stays.

| Today | Where it goes |
|---|---|
| Banner above the file list, "press Enter to open that folder and search" (`Listing.tsx` ~1605-1619, `enterPrompt()`) | Into the dropdown as a pressable row. **Remove the banner** - it pushes the file list down. |
| `Search ⌘L` grey words in the box | Becomes the magnifier button, shortcut in its tooltip |
| The placeholder guidance | Into the dropdown |
| "No such file or folder" (same banner, `pathNotFoundMessage()`) | Into the dropdown, above a search offer |
| `↵ to open · esc to clear` and the per-row `↵` badges on the **home screen** (`FilesHome.tsx` `OpenRow` ~205-238, `AiActionRow` ~260-303, `.fh-result-note` ~1043-1157) | **STAYS. Do not touch `FilesHome.tsx`.** |

**Why the home screen stays** - and this corrects the original request, which
asked to remove "the hints shown in the file explorer page/entries": in the
folder view the file rows have **no** Enter hints at all. The only Enter message
there is the banner *above* the rows. The per-row `↵` badges are on the home
screen, and those already sit in a list of results, which is precisely where
everything else in this spec is moving *to*. Removing them would move them away
from the pattern, not toward it. The orchestrator surfaced this to the user
twice and recommended keeping them; proceed on that recommendation.

## Hard behavioural constraint - read this before touching selection

PR #1091 fixed a HIGH-severity bug where Enter opened an **arbitrary first row**
for a relative path-shaped query. The fix keys `rowsAnswerQuery` on `!searching`.
Adding a first row to the dropdown walks straight back into that blast radius.

Requirements:
- For a **path-shaped query**, plain Enter must still resolve/open the path
  exactly as it does today. The search action row must be **reachable** by arrow
  keys but must **not** be the default selection, and must not change what a
  bare Enter does.
- Arrow-key navigation must include the action rows in its index maths. Check
  every place that counts rows or maps an index to a row - an action row that
  the keyboard skips, or that shifts the folder completions by one silently, is
  the defect to avoid.
- `isPathQuery` (from `listing/path-shaped-query.ts`) already decides mode and
  whether a search runs at all. Read from it; do not add a second, parallel
  notion of "is this a path".

## Files

Everything is under `frontend/src/apps/explorer/` and `frontend/src/styles/`.

- `SearchField.tsx` - chip markup (~293-310), `HINT_LONG`/`HINT_SHORT`
  (~156-157, 336), `.listing-search-shortcut-hint` (~489-493), examples panel
  (~403-423), completion rows (~424-456)
- `Listing.tsx` - the banner row (~1605-1619)
- `listing/enter-prompt.ts` - `enterPrompt()`, `pathNotFoundMessage()`
- `styles/explorer.css` - `--chip-inset` and the chip/hint/row rules
- **Do not touch** `FilesHome.tsx`, and **do not touch** the tracked
  `DECISIONS.md` (1598 lines, shared). Append your notes to
  `DECISIONS-omnibox-search-affordance.md` instead.

Line numbers are from the base commit and are pointers, not gospel - confirm
before editing.

## Repo conventions that will bite

- **No colour literals.** `tests/test_theme.py` bans them outside the token
  file. Dark is `:root` (the repo default); light is `:root[data-theme="light"]`.
  Add to **every** theme block. `--tint` is a bare `r, g, b` triple, used as
  `rgba(var(--tint), .06)`.
- **Reuse the vocabulary.** Before writing CSS, grep for the class that already
  does the job. The `⋮` button already has a chassis - the magnifier goes on
  that same one. An invented class loses the specificity fight and renders as
  something you did not design.
- **Some Python tests assert on literal frontend source lines.** Grep `tests/`
  for every symbol, class name and string you remove or rename, before you
  remove it. `bun test` staying green does not cover this.
- `bun` / `bunx`, never npm/npx.
- `mock.module` in bun is **process-wide**: a stub in one test file leaks into
  unrelated files in a full `bun test`. If you stub a module, stub it complete -
  a missing export throws `SyntaxError: Export named 'x' not found` and silently
  skips the block you thought you were testing. That exact failure already
  happened once on the base branch.

## Verification

- TDD: test first, watch it fail, implement, watch it pass.
- Scoped tests only in the inner loop (`bun test <path>`, `-t <name>`). The
  orchestrator runs the full suite once at the end.
- Existing tests to update rather than duplicate: `search-mode-chip.test.ts`
  (asserts on the per-mode `--chip-inset` value and the chip word - both change
  here), `search-enter-banner.test.ts` (the banner it covers is being removed;
  the coverage should move to the dropdown row, not be deleted).

New coverage worth having:
- the chip renders no word, and still exposes a spoken mode label;
- `--chip-inset` no longer varies by mode;
- the magnifier button is pressable and triggers the same handler as ⌘L;
- the dropdown offers a search action row for a bare word;
- the dropdown shows the not-found row plus a search offer for an unresolvable
  path;
- **a path-shaped query's bare Enter still resolves the path** and does not
  trigger the action row (the regression guard for the HIGH bug above);
- arrow-key navigation reaches the action row and still lands correctly on the
  folder completions after it.

## Cannot be verified headlessly - report these back, do not claim them

Layout, hover and both themes need a human on a running screen:
- ~~the two magnifiers problem~~ - RESOLVED by item 3's revision: the left
  chip stayed icon-only and the right button's words came back, so there is
  only ever one magnifier glyph on screen at a time (the right button's own
  narrow-width fallback). Still worth a glance at the exact width where the
  button collapses to that glyph, alongside the left chip, in case the pairing
  reads oddly for a moment right at the threshold.
- no horizontal jump when the mode flips, or on focus/blur;
- the search button (words form) sitting as a sibling of `⋮`/`★`, in light and
  dark, and the wrap/clip behaviour of "Search ⌘L" right around
  `HINT_WIDE_PX`;
- the dropdown's action row against a long query and a narrow window;
- the teaching panel's derived examples reading naturally for a real folder's
  actual extension mix, and the panel's precedence over completions on a
  pristine, pre-filled path not looking like a bug (it hides the folder's own
  children the completion dropdown would otherwise offer).
