// Which of the landing page's three cross-session lists is on screen, and
// whether they share a tab bar — `LIST_PANELS` / `syncLists` / the arrow-key
// walk, T:18260-18338 (inventory 05 §B "Tab rules").
//
// The counts ARE the state, and `null` — "the read has not answered yet" — is
// not zero: a skeleton is drawn into Recent while the sessions read is in
// flight, and it has to be on screen to be a skeleton of anything.

export type ListName = "recent" | "artifacts" | "snaps";
export const LIST_NAMES: readonly ListName[] = ["recent", "artifacts", "snaps"];

export interface ListCounts {
  /** `null` = still reading. */
  recent: number | null;
  artifacts: number | null;
  snaps: number | null;
  /** A failed snapshots read keeps its tab: the retry lives in that panel, so
   *  it must stay reachable rather than vanishing with the failure (T:18258). */
  snapsFailed: boolean;
}

export interface ListVisibility {
  tabbed: boolean;
  selected: ListName;
  /** Is this panel rendered at all. */
  shown: Record<ListName, boolean>;
  /** Is this list's tab on the bar. */
  tabShown: Record<ListName, boolean>;
}

/** "Worth a tab": really has rows. `null > 0` is false, so a list still being
 *  read never counts — which keeps the bar from flashing in and out as the
 *  reads land (T:18264-18291). */
export function isFilled(counts: ListCounts, name: ListName): boolean {
  if (name === "recent") return (counts.recent ?? 0) > 0;
  if (name === "artifacts") return (counts.artifacts ?? 0) > 0;
  return (counts.snaps ?? 0) > 0 || counts.snapsFailed;
}

/** "Worth a heading of its own" — a DIFFERENT question: Recent stands alone
 *  while unread (it has a skeleton to stand in for the rows) and Snaps while
 *  reading or failed (it has a sentence, and a retry) (T:18272-18290). */
export function isAlone(counts: ListCounts, name: ListName): boolean {
  if (name === "recent") return counts.recent === null || counts.recent > 0;
  if (name === "artifacts") return (counts.artifacts ?? 0) > 0;
  return counts.snaps === null || counts.snaps > 0 || counts.snapsFailed;
}

/** The tabs actually ON the bar, in bar order. */
export function filledTabs(counts: ListCounts): ListName[] {
  return LIST_NAMES.filter((name) => isFilled(counts, name));
}

/** Tabs once TWO of the lists really have rows: one tab is a heading with a
 *  pointer cursor, and each section's plain heading is the honest version of
 *  it. A selected tab whose list has just emptied falls back to the first one
 *  that does have rows (T:18293-18317). */
export function computeLists(
  counts: ListCounts,
  selected: ListName,
): ListVisibility {
  const filled = filledTabs(counts);
  const tabbed = filled.length >= 2;
  const active = tabbed && !filled.includes(selected) ? filled[0] : selected;
  const shown = {} as Record<ListName, boolean>;
  const tabShown = {} as Record<ListName, boolean>;
  for (const name of LIST_NAMES) {
    shown[name] = tabbed ? active === name : isAlone(counts, name);
    tabShown[name] = isFilled(counts, name);
  }
  return { tabbed, selected: active, shown, tabShown };
}

/** ArrowLeft/Right walk the SHOWN tabs, wrapping; a hidden tab is not a stop,
 *  and with two of three shown the pair still toggles (T:18326-18338). */
export function nextTab(
  counts: ListCounts,
  from: ListName,
  dir: 1 | -1,
): ListName | null {
  const shown = filledTabs(counts);
  const at = shown.indexOf(from);
  if (at < 0 || shown.length < 2) return null;
  const step = dir === 1 ? 1 : shown.length - 1;
  return shown[(at + step) % shown.length];
}

/**
 * WHICH LIST IS SHOWING IS THE BLOCK'S STATE, not the component's (T:18260-18265
 * — "leaving for a chat and coming back keeps the tab you were on").
 *
 * A module-level `let`, which is what T's `listTab` is: `Lists` unmounts the
 * moment the reader enters a chat, so component state cannot hold this and Back
 * always returned to "Recent chats". Hoisting it into `ClaudeChat` would not do
 * either — the mounted chat is remounted by a mode switch, and T's own variable
 * outlives that too. It lives as long as the page, which is exactly T's scope.
 *
 * `computeLists` already falls the selection back to the first filled tab when
 * the remembered one has since emptied, so a stale name here is never a blank
 * panel.
 */
let rememberedListTab: ListName = "recent";

export function rememberedTab(): ListName {
  return rememberedListTab;
}

export function rememberTab(name: ListName): void {
  rememberedListTab = name;
}

/** Tests only: put the page's memory back to its boot value. Exported rather
 *  than reached through the module object so a suite cannot forget which
 *  variable it is resetting. */
export function resetRememberedTab(): void {
  rememberedListTab = "recent";
}
