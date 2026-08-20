# Git template UX touch-up — design

Inherits `~/.claude/design-principles.md`. Reference app copied: **GitHub Desktop**
(changes checkboxes aside — staging model untouched this round; history selection,
single sync button, stash presentation). Driven by the 2026-08-20 four-agent audit.

## Out of scope (deliberately)

- Unified checkbox staging (kills Staged/Changes/Untracked). Pinned by
  `test_git_scope.py`; a structural feature, not a touch-up. Parked.
- ours/theirs conflict buttons (new ops.py ops). Parked; the AI-decline note now
  points at hand-editing instead.
- First-run tour contrast (shell-level, not this template).

## 1. Commit list + preview (the user's core complaint)

- **Selected commit row**: new `--sel-bg` token (accent-tinted) in BOTH theme
  blocks; selected row shows the short sha in front of the subject (identity at
  the row, not only in the pane). Hover ≠ selected any more.
- **Preview ("show file as of this commit")**: eye toggle stays, but the active
  commit also wears a `previewing` pill, and a banner renders above the Commits
  section: "Files shown as of <sha> — Back to now" with the exit button. The
  state is visible without hovering anything.
- **Diff pane**: raw plumbing lines (`diff --git`, `index …`, mode/similarity
  lines) are not rendered; each file boundary becomes a styled filename divider
  (parsed from `diff --git`). Hunk headers stay.

## 2. Toolbar — one sync control (GitHub Desktop's morphing button)

One button replaces Fetch/Pull/Push, labeled by what it will do:
- behind > 0 → "Get latest ↓N" (pull; GH Desktop rule: pull before push)
- ahead > 0, behind 0 → "Send ↑N" (push)
- both 0 → "Check for updates" (fetch)
- no remote → disabled "No remote"
- git verbs stay in the tooltip. The 372px icon-only breakpoint dies with the
  third and fourth buttons (audit: it measured the viewport, not `#root`).

## 3. Narrow pane (384px default) fixes

- `.row { overflow: hidden }` — no child ever paints over `.acts`.
- Stash rows rebuilt: message is the primary field (never width 0), time under
  it; `stash@{N}`/branch move to the tooltip; actions become **Restore** (pop)
  and **Delete** (drop, confirmed). "apply" (keep-a-copy) moves to Restore's
  tooltip note; three verbs was expert altitude.
- Confirm bar question takes its own full line below 560px.

## 4. Vocabulary

- Status letters → words: the row renders `change.label` ("Modified", "Deleted",
  "Untracked"…) as the colored field; the porcelain code stays in the tooltip.
- `detached at abc1234` → `not on a branch — viewing abc1234`.
- Confirm affirmative names the act ("Discard changes", "Drop stash", …), never
  "Yes, do it".
- Commit box renders whenever ANYTHING is uncommitted: with nothing staged it is
  disabled with "Stage (+) the changes you want to include first" — the invisible
  first step becomes a taught step. (Fully clean tree still shows no box.)

## 5. State + safety

- Success flash auto-dismisses after 4s; failures persist.
- The busy button shows an inline spinner (reduced-motion: static).
- Branch ✕ becomes two-click ("Delete?") — local state, not `ask` (DESTRUCTIVE
  mirror with ops.py is pinned by test and `branch -d` cannot lose commits).
- Commit disabled while any listed change is conflicted, with the reason.
- Error-flash AI helper becomes a labeled "Explain" button — an icon-only sparkle
  next to another sparkle that writes commit messages is how the novice got
  terminal instructions she never asked for.

## 6. Theme + motion

- Drop `color` from the `.btn` transition (WebKit freezes `var()` colors —
  the light-mode 2.6:1 WCAG failure).
- `ai-pulse` swing narrowed 0.65→1; caret keeps its flip, loses the dead
  transition; confirm bar gets a 150ms entrance (keyframe, reduced-motion
  guarded); section `h2` 12px `--ink-2`.

---

# v2 — user feedback round (2026-08-20, screenshot review)

NN heuristics as the bar: no layout shift, clear hierarchy, no overload.

1. **Sections are accordions** (Changes, Stashes, Commits): chevron on the
   header, whole header toggles. Stashes starts COLLAPSED (memory, not URL).
2. **One "Changes" section** replaces Staged/Changes/Untracked. Each row: a
   CHECKBOX (checked = staged, indeterminate = partially staged, disabled on
   conflicts), status word, path, discard icon. Master checkbox in the header
   stages/unstages everything. Commit box counts checked rows.
3. **Icon buttons with responsive labels**: header + stash-row actions render
   icon + word; below ~480px pane width the word hides (CSS, .btn-label), the
   tooltip and aria-label always carry it. Commit keeps its word always —
   the one primary action.
4. **Branches = popover dropdown** anchored to the top-left chip (which gains a
   branch glyph). No inline section, no layout shift; outside click / Escape
   closes. Same param (`panel=branch`).
5. **Status = bottom toast**, fixed position, never shifts layout. Success
   auto-dismisses (~4s); failure persists with labeled Explain.
6. **"Set aside" renamed "Stash"** (user's call — the section is already named
   Stashes; tooltip keeps the plain-language explanation). Clicking it opens a
   REAL dialog: message field, include-new-files toggle, the list of files it
   will stash, Confirm/Cancel.
7. **Stash rows**: Restore / Delete as icon buttons (responsive labels).
8. **Diff opens INLINE under the clicked row** — commit diffs expand inside the
   Commits list, change diffs inside Changes. The two-column/stacked pane dies;
   nothing above the click moves. diffPane keeps its signature (test contract),
   only placement changes.
9. **"End of history" = a quiet ── divider ──** under the list, not a fake row.

Deliberately kept against the letter of the feedback: text confirm bars for
destructive ops (icon-only destruction fails error-prevention), and the Commit
button's label.
