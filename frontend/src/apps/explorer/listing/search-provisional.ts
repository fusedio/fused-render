// A blur decides between three fates for the search box, and the split that
// matters is committed vs. not — never where the text came from. `committed`
// answers "do the rows on screen answer this query": true once a query has
// actually been searched (an auto-filtering query the moment its results
// land, a base-escaping one only once Enter has run it — decision 4's gate).
// A query sitting in the box that nothing has searched yet is the app's own
// half-finished gesture, not a result the user is looking at, so a click
// elsewhere reads as declining it: clear the field and fold back to the
// resting crumb strip, the way a browser location bar lets a click elsewhere
// drop text nobody pressed Enter on. A committed query is different — real
// rows are on screen, and losing them to a stray click would be the outage
// this box exists to avoid, so those survive a blur untouched.
//
// This is the whole decision blur needs to make, pulled out so the input's
// onBlur handler in Listing.tsx and this file's tests can't drift apart:
// an empty field always unpins regardless of commit state (nothing to lose
// either way); a non-empty, uncommitted query discards; a non-empty,
// committed query keeps the box open.
export type SearchBoxBlurAction = "discard" | "keep-open" | "unpin";

export function searchBoxBlurAction(
  committed: boolean,
  queryEmpty: boolean,
): SearchBoxBlurAction {
  if (queryEmpty) return "unpin";
  return committed ? "keep-open" : "discard";
}
