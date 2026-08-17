// What the Discover tab is showing, decided in one place.
//
// The tab has two mutually exclusive faces — a curated shortlist and a grid of
// Hub search results — and four pieces of chrome that have to agree about
// which one is on screen: the grid itself, the ALL-CAPS heading that NAMES it,
// the note under that heading, and the "searching huggingface.co" caption that
// discloses the outbound request. Written inline they were separate `&&`s over
// the same two strings, which is one chance per condition to leave one behind:
// a preamble reading "picked to run on this machine" standing over a grid of
// search results is the page describing cards that are not there any more.
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
  /** The line that says the sections below are a curated pick rather than
   *  everything installable. Only over the sections it describes. */
  showsPreamble: boolean;
  /** The disclosure that this is the one place in the app asking a third party
   *  a question. Only while there is a question. */
  showsSearchNote: boolean;
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
    // The two captions are exclusive by construction rather than by two
    // conditions that happen to be opposites today.
    showsPreamble: !asked,
    showsSearchNote: asked,
  };
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
 */
export function resultsSummary(q: string, shown: number | null, host: string): string | null {
  const parts: string[] = [];
  if (q.trim()) parts.push(`"${q.trim()}"`);
  if (shown !== null) parts.push(`${shown} on ${host}`);
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
