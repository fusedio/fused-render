// What the Local page's SEARCH FACE is showing, decided in one place.
//
// The page has two mutually exclusive faces — its own sections (a carousel per
// capability, then "Fetched by engines") and one grid of Hub search results —
// and three pieces of chrome that have to agree about which one is on screen:
// the grid itself, the "searching huggingface.co" caption that discloses the
// outbound request, and the control that goes BACK. Written inline they were
// separate `&&`s over the same condition, which is one chance per condition to
// leave one behind: a missing way back is a page with no exit.
//
// This module is `discoverView.ts` retargeted (D426). It was written for a
// Discover TAB whose two faces were a curated shortlist and these same results;
// that tab is gone and the search is a face of the Local page now, so what the
// idle face is CALLED is no longer this module's business — the page's own
// sections name themselves. What survives unchanged is the rule that made the
// swap legible, because it is the same swap.
//
// Here rather than in the component for the reason `engines.ts` is: the
// component cannot be unit-tested in this repo (there is no DOM harness, by
// design — every test here drives a pure module), and this is the part with a
// rule in it. The JSX that consumes it is three ternaries.
//
// It also owns the two MENUS' vocabulary and the one sort that the Hub cannot
// perform — see `ResultSort` — for the same reason: a page-level sort whose
// whole safety property is "this value never reaches the wire" is a rule, and a
// rule belongs somewhere it can be driven.
import type { HubSort, HubTask } from "@platform/lib/api";

/** Which face of the Local page is on screen. */
export type SearchFace = "models" | "results";

export interface SearchChrome {
  /** Which grid is on screen. Results REPLACE the page's sections rather than
   *  stacking under them — the carousels answer "what should I even get", which
   *  is the question somebody has BEFORE they type, and showing both asks the
   *  reader which of two grids is talking to them. */
  face: SearchFace;
  /** What the results section is CALLED, on screen, in the page's own heading
   *  language — or null in the idle face, which has no one heading because it
   *  has no one section (its capability rows and "Fetched by engines" each name
   *  themselves).
   *
   *  Replacing three sections with one is only legible if the new one says its
   *  name, in the slot and the treatment the sections it replaced used. */
  heading: string | null;
  /** The disclosure that this is the one place in the app asking a third party
   *  a question. Only while there is a question. */
  showsSearchNote: boolean;
  /** Whether to offer the way BACK to the page's own sections.
   *
   *  True exactly when `face` is "results", and that is the whole point of it
   *  living here: the escape hatch has to be on screen in every state the
   *  reader can be stuck in, and the state that stranded people was the one an
   *  ✕-on-the-input would have missed — a task picked from the select with an
   *  empty search box. There is nothing in the box to clear, the sections are
   *  gone, and the only route back is guessing that the select's first option
   *  restores them.
   *
   *  Derived rather than asked as a second question, because two conditions
   *  that are opposites today are two chances for a results page to have no way
   *  off it. */
  showsReset: boolean;
}

/** Which face the page is showing, from the SETTLED query and task filter.
 *
 *  Settled, not live: the caller debounces, and driving the layout off every
 *  keystroke would swap the carousels out on the first letter and back on a
 *  backspace.
 *
 *  Whitespace is not a query. A box holding a space is a box nobody has typed
 *  into, and treating it as one replaces the page with "nothing matches that" —
 *  which reads as the app having lost its own models rather than as a search
 *  having been run.
 */
export function searchChrome(q: string, task: string): SearchChrome {
  const asked = !!(q.trim() || task.trim());
  return {
    face: asked ? "results" : "models",
    heading: asked ? "Search results" : null,
    showsSearchNote: asked,
    // Asked a question, so there is a way back from the answer. Never in the
    // idle face, where the control would undo nothing and a permanent "clear"
    // would teach the reader that the page is always filtered.
    showsReset: asked,
  };
}

/** The muted right-hand fact beside SEARCH RESULTS: what was asked, and how
 *  many came back from where.
 *
 *  This is the slot the capability rows put their byte subtotal in, and it is
 *  what makes the results heading read as a sibling of "User downloaded models"
 *  rather than as chrome from somewhere else. It is also the one place the count
 *  and the host are stated together, which is the difference between "24
 *  models" and "24 models on somebody else's server".
 *
 *  Both halves are optional and for different reasons. The QUERY is absent when
 *  a task filter is the whole question — quoting `""` there would report a
 *  search nobody typed. The COUNT is absent while the request is in flight:
 *  a "0 on huggingface.co" standing over an empty grid is a wrong answer where
 *  silence is a missing one.
 *
 *  `failed` is the same rule for the same reason, and it is a REQUIRED argument
 *  rather than an optional one because a summary that guessed would state the
 *  wrong fact silently. A search that did not come back has no count to report:
 *  a soft failure answers 200 with an `error` and `models: []`, which as a
 *  length is "0 on huggingface.co" — the heading saying the Hub HAS none of
 *  these, next to a banner saying we never heard back. A hard rejection is
 *  worse still, leaving the previous search's rows in state so the count comes
 *  from a different question entirely. What was ASKED survives a failure and is
 *  still worth showing; what came BACK does not exist.
 */
export function resultsSummary(
  q: string,
  shown: number | null,
  host: string,
  failed: boolean,
): string | null {
  const parts: string[] = [];
  if (q.trim()) parts.push(`"${q.trim()}"`);
  if (shown !== null && !failed) parts.push(`${shown} on ${host}`);
  return parts.length ? parts.join(" · ") : null;
}

// ---- The two menus, and the one sort the Hub cannot do ---------------------

/** How the results are ordered, as the PAGE offers it — the Hub's own sorts plus
 *  `size`, which the Hub cannot perform.
 *
 *  **Deliberately not a widening of `HubSort`.** The list endpoint's sort is a
 *  server-side allowlist (`_SORTS` in `routers/hub_models.py`) and it has no
 *  size in it, because the Hub refuses to expand `usedStorage` on a list at all
 *  — a size is one request per repo. So "size" is a value that must never reach
 *  the wire, and the way to guarantee that is a type that cannot carry it there:
 *  every request goes through `wireSort`, and `searchHubModels` still takes a
 *  `HubSort`. A `sort` union with "size" in it would put the guarantee in the
 *  hands of whoever writes the next call site.
 */
export type ResultSort = HubSort | "size";

export interface SortOption {
  value: ResultSort;
  /** Two words at most: this is a trigger label as well as a menu row. */
  label: string;
  /** What the ordering MEANS, on hover — "Downloads" alone does not say over
   *  what period, and "New" does not say new to whom. */
  title: string;
}

/** Every ordering the page offers, in the order the menu lists them: the three
 *  ways the Hub ranks a search, then the one the page ranks itself.
 *
 *  Size is LAST and not first even though it is the most concrete of them,
 *  because it is the only one that costs a measurement (see `wireSort`) and the
 *  menu reads best when the cheap answers come first. */
export const SORTS: readonly SortOption[] = [
  { value: "downloads", label: "Downloads", title: "Most downloaded in the last month" },
  { value: "likes", label: "Likes", title: "Most liked on the Hub" },
  { value: "updated", label: "Updated", title: "Changed most recently" },
  { value: "created", label: "New", title: "Published most recently" },
  {
    value: "size",
    label: "Size",
    title:
      "Smallest first, measured on this page. The Hub cannot rank a search by size, so this " +
      "orders the most-downloaded results by what the whole repo weighs — which takes a moment.",
  },
];

/** The sort the SERVER is asked for, which is never "size".
 *
 *  Size becomes `downloads`: the page has to pick some candidate set to rank,
 *  and the honest one is the set anybody would have got by default — the
 *  most-downloaded models matching the query, then reordered by what they cost.
 *  Ranking "the most recently published 24 models, smallest first" would be two
 *  questions in one control, and neither of them the one that was asked.
 */
export function wireSort(sort: ResultSort): HubSort {
  return sort === "size" ? "downloads" : sort;
}

/** Whether this ordering is the page's own work rather than the Hub's — i.e.
 *  whether the results have to be measured before they can be shown in order. */
export function sortsOnPage(sort: ResultSort): boolean {
  return sort === "size";
}

/** The option the sort trigger wears: its icon's key, its label, its hover.
 *
 *  Falls back to the first entry rather than returning null, because the trigger
 *  is a control that always has a current value — a menu button showing nothing
 *  is a control that looks broken, and the state it would be showing (a sort the
 *  page does not offer) cannot be reached from the menu anyway. */
export function activeSort(sort: ResultSort): SortOption {
  return SORTS.find((s) => s.value === sort) ?? SORTS[0];
}

/** What the task trigger wears, from the filter in force and the glossary the
 *  server handed over.
 *
 *  "Any task" is the unfiltered label and it is not a placeholder: it means any
 *  task THIS APP RUNS, the menu beside it holds only those (D313, HS-0a), and an
 *  empty-looking control would invite the reader to think nothing was applied
 *  where in fact a whole registry is.
 *
 *  A tag the glossary does not explain still gets a label — its own tag — rather
 *  than falling back to "Any task": the filter IS in force, the results are
 *  narrowed, and a trigger claiming otherwise would be the one control on the
 *  row describing a different page than the one on screen. That happens when the
 *  `hub/tasks` GET failed (the menu is empty, the filter survives from before)
 *  or when a runner is unregistered between the two.
 */
export function activeTask(
  task: string,
  tasks: readonly HubTask[],
): { label: string; title: string } {
  const t = task.trim();
  if (!t) {
    return {
      label: "Any task",
      title: "Any task an engine here can run — pick one to show only models for that job",
    };
  }
  const known = tasks.find((x) => x.tag === t);
  return {
    label: known?.label ?? t,
    title: known?.help ?? "Showing only models for this task",
  };
}

/** The results, smallest repo first, with the unmeasured ones last.
 *
 *  ASCENDING because the useful reading of a size sort on a page whose every
 *  card has a Download button is "what fits" — somebody sorting a list of
 *  multi-gigabyte models by size is looking for the one they can afford, not for
 *  the biggest thing on the Hub.
 *
 *  Unknown sizes go LAST, and both kinds of unknown go there together: `null` is
 *  the Hub having no total for that repo, `undefined` is nobody having asked (or
 *  having asked and failed — see `lookupTotalSize`). They are different facts
 *  about why there is no number, and neither is a number, so sorting either into
 *  the middle would put a repo of unknown size between two known ones and invite
 *  the reader to read a size off its position.
 *
 *  STABLE, so the server's own ranking survives as the tie-break: two repos of
 *  identical size, and the twenty with no size at all, stay in most-downloaded
 *  order rather than being shuffled into an order nothing chose.
 */
export function bySizeAscending<T>(
  models: readonly T[],
  sizeOf: (model: T) => number | null | undefined,
): T[] {
  return models.slice().sort((a, b) => {
    const x = sizeOf(a);
    const y = sizeOf(b);
    if (typeof x !== "number") return typeof y !== "number" ? 0 : 1;
    if (typeof y !== "number") return -1;
    return x - y;
  });
}

/** What the Hub asks of somebody before it will hand over a gated repo. */
export interface GateChrome {
  /** Two words on the card, in the same slot the "✓ downloaded" badge uses. */
  pill: string;
  /** The whole of what the reader has to DO, on hover. The pill without this
   *  would be the badge D313 deleted: honest, and no help. */
  title: string;
  /** Whether to offer the Download button anyway. True exactly when this
   *  machine holds a token — the token is what turns "you cannot have this"
   *  into "you may already have accepted this", and the Hub is the one that
   *  gets to refuse. */
  canDownload: boolean;
  /** The link to the model's Hub page, worded for the gate — or null when the
   *  Download button is the action. */
  action: string | null;
}

/** What a gated result offers instead of a bare Download button, or null.
 *
 *  Gated repos were dropped from the results entirely, on the rule that every
 *  card must carry a working button (D313). D316 narrows that: the rule is
 *  that an ENGINE HERE CAN RUN IT, not that nothing further is asked of the
 *  user. A licence you accept by signing in and clicking is a step somebody can
 *  take, several of the best-known models on the Hub sit behind exactly one,
 *  and a search that silently omitted them was answering a question nobody
 *  asked.
 *
 *  What must not come back is the old `gated` pill — a badge that named the
 *  problem and left the button that would 403 sitting beside it. So the gate
 *  decides the ACTION as well as the label: with no token the card's action is
 *  the licence page rather than a download that cannot start. The way to GET a
 *  token is offered once beside the results rather than on every card (D426),
 *  because it is one act for the whole grid.
 *
 *  `manual` is the one gate that takes more than signing in — the repo's owner
 *  grants access by hand — and it is worth its own words, because somebody told
 *  to "accept the terms" on one of those goes looking for a button that is not
 *  there.
 */
export function gateChrome(
  gated: "auto" | "manual" | null,
  authenticated: boolean,
): GateChrome | null {
  if (!gated) return null;
  const manual = gated === "manual";
  const pill = manual ? "gated — by approval" : "gated";
  if (authenticated) {
    return {
      pill,
      title: manual
        ? "Gated: this repo's owner grants access by hand. This machine has a Hugging Face " +
          "token, so the download will work once they have — and fail until then."
        : "Gated: accept this repo's licence on its Hub page if you have not already. This " +
          "machine has a Hugging Face token, so the download can go ahead once you have.",
      canDownload: true,
      action: null,
    };
  }
  return {
    pill,
    title:
      (manual
        ? "Gated: this repo's owner grants access by hand — request it on the Hub page. "
        : "Gated: sign in on the Hub page and accept this repo's licence. ") +
      "Downloading it here also needs a token on this machine — sign in to Hugging " +
      "Face beside these results, in Preferences → AI, or set HF_TOKEN.",
    canDownload: false,
    action: manual ? "Request access" : "Accept terms",
  };
}

/** Whether these results need the sign-in offered beside them at all.
 *
 *  **Only where a gated result needs it** (D426). A login prompt standing over
 *  every search would be this page recommending an account to somebody who
 *  never hit a wall — and the wall is the only thing that makes the offer
 *  legible: a token is what turns the "Accept terms" link on the card below
 *  into an ordinary Download. Already signed in, or nothing gated came back,
 *  and there is nothing here to say.
 */
export function needsHubLogin(
  models: readonly { gated: "auto" | "manual" | null }[] | null,
  authenticated: boolean,
): boolean {
  if (authenticated || !models) return false;
  return models.some((m) => !!m.gated);
}
