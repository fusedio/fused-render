// What the Discover tab is showing, decided in one place.
//
// The tab has two mutually exclusive faces — a curated shortlist and a grid of
// Hub search results — and four pieces of chrome that have to agree about
// which one is on screen: the grid itself, the heading that NAMES it, the
// "searching huggingface.co" caption that discloses the outbound request, and
// the control that goes BACK. Written inline they were separate `&&`s over the
// same two strings, which is one chance per condition to leave one behind: a
// missing way back is a page with no exit.
//
// The heading is the newest of the four and the one the others hang off. The
// faces used to differ only by whether a paragraph of prose was present, which
// is a difference you can only notice if you were watching it appear — scroll
// down a page of results and back up, and nothing at the top said which of the
// two grids you were in.
//
// Here rather than in the component for the reason `engines.ts` is: the
// component cannot be unit-tested in this repo (there is no DOM harness, by
// design — every test here drives a pure module), and this is the part with a
// rule in it. The JSX that consumes it is three ternaries.
export type DiscoverView = "suggested" | "results";

export interface DiscoverChrome {
  /** Which grid is on screen. Results REPLACE the shortlist rather than
   *  stacking under it — a shortlist answers "what should I even get", which
   *  is the question somebody has BEFORE they type, and showing both asks the
   *  reader which of two grids is talking to them. */
  view: DiscoverView;
  /** What that grid is CALLED, on screen, in the page's own heading language.
   *
   *  Replacing one grid with another is only legible if the new one says its
   *  name. Before this, the two states differed by a paragraph of prose
   *  appearing and disappearing above them, so a reader who searched, scrolled
   *  through the results and looked back up had nothing at the top of the page
   *  telling them which of the two things they were in. Both headings now sit
   *  in the same slot with the same treatment, and this field is why exactly
   *  one of them can exist: they are one string, not two `&&`s. */
  heading: string;
  /** The disclosure that this is the one place in the app asking a third party
   *  a question. Only while there is a question. */
  showsSearchNote: boolean;
  /** Whether to offer the way BACK to the shortlist.
   *
   *  True exactly when `view` is "results", and that is the whole point of it
   *  living here: the escape hatch has to be on screen in every state the
   *  reader can be stuck in, and the state that stranded people was the one an
   *  ✕-on-the-input would have missed — a task picked from the select with an
   *  empty search box. There is nothing in the box to clear, the suggestions
   *  are gone, and the only route back is guessing that the select's first
   *  option restores them.
   *
   *  Derived rather than asked as a second question, because two conditions
   *  that are opposites today are two chances for a results page to have no way
   *  off it. */
  showsReset: boolean;
}

/** Which face the tab is showing, from the SETTLED query and task filter.
 *
 *  Settled, not live: the caller debounces, and driving the layout off every
 *  keystroke would swap the shortlist out on the first letter and back on a
 *  backspace.
 *
 *  Whitespace is not a query. A box holding a space is a box nobody has typed
 *  into, and treating it as one replaces the catalog with "nothing matches
 *  that" — which reads as the app having lost its own suggestions rather than
 *  as a search having been run.
 */
export function discoverChrome(q: string, task: string): DiscoverChrome {
  const asked = !!(q.trim() || task.trim());
  return {
    view: asked ? "results" : "suggested",
    heading: asked ? "Search results" : "Suggested models",
    showsSearchNote: asked,
    // Asked a question, so there is a way back from the answer. Never in the
    // curated state, where the control would undo nothing and a permanent
    // "clear" would teach the reader that the page is always filtered.
    showsReset: asked,
  };
}

/** Every model this machine holds a complete copy of, by id, pointing at where
 *  it lives. `null` until the walk has answered. */
export type OnDisk = ReadonlyMap<string, string>;

/** Where this machine's copy of `id` is, or null if there is not one to open.
 *
 *  ONE source for the whole of what a card says about the local copy — the
 *  ✓ downloaded badge, the absence of a Download button, and the Explore link
 *  all come from this map. They used to not: the badge read this live listing
 *  while Explore read `local` from the search reply that first described the
 *  row. That reply is frozen at the moment of the search, so downloading a
 *  model from the results flipped the checkmark on (the re-walk saw it) and
 *  left Explore hidden (the reply still said "none") — no way to open the copy
 *  you had just fetched, on the one card most likely to want it.
 *
 *  Two sources for one fact will always drift; the only question is when. A
 *  path from the listing is also the fresher answer in the other direction: a
 *  model deleted from the Local tab stops offering Explore here without a
 *  re-search.
 *
 *  An empty path is treated as no path. Explore builds a URL out of this, and a
 *  link to nowhere is worse than no link.
 */
export function localCopy(id: string, onDisk: OnDisk | null): string | null {
  return onDisk?.get(id) || null;
}

/** The muted right-hand fact beside SEARCH RESULTS: what was asked, and how
 *  many came back from where.
 *
 *  This is the slot the capability sections put "via MLX Whisper" in, and it is
 *  what makes the results heading read as a sibling of theirs rather than as
 *  chrome from somewhere else. It is also the one place the count and the host
 *  are stated together, which is the difference between "24 models" and "24
 *  models on somebody else's server".
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

/** The same slot beside SUGGESTED MODELS: how many, and picked by whom.
 *
 *  Deliberately the same shape as `resultsSummary` — a count and where it came
 *  from — because the pairing is the whole point. "11 picked for this machine"
 *  against "24 on huggingface.co" is the distinction the reader needs, stated
 *  in one line each.
 */
export function suggestedSummary(shown: number): string {
  return `${shown} picked for this machine`;
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
 *  the licence page rather than a download that cannot start.
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
      "Downloading it here also needs a token on this machine (`huggingface-cli login`, or " +
      "HF_TOKEN in the environment).",
    canDownload: false,
    action: manual ? "Request access" : "Accept terms",
  };
}
