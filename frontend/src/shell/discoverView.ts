// What the Discover tab is showing, decided in one place.
//
// The tab has two mutually exclusive faces — a curated shortlist and a grid of
// Hub search results — and three pieces of chrome that have to agree about
// which one is on screen: the grid itself, the "these are suggestions"
// preamble above it, and the "searching huggingface.co" caption that discloses
// the outbound request. Written inline they were three separate `&&`s over the
// same two strings, which is three chances to leave one of them behind: a
// preamble reading "Suggested models — picked to run on this machine" standing
// over a grid of search results is the page describing cards that are not
// there any more.
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
    // The two captions are exclusive by construction rather than by two
    // conditions that happen to be opposites today.
    showsPreamble: !asked,
    showsSearchNote: asked,
  };
}
