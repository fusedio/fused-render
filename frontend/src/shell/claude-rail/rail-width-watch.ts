// THE RAIL CHANGED WIDTH (the expandable rail, `rail_extras_enabled`; owner,
// 2026-11-21): expanded, collapsed, or dragged wider or narrower — every frame
// of the width glide and every move of the drag. The rail's cards are placed
// against the rail's left edge and re-place on a window resize or on their
// row reshaping; neither happens when only the rail moves its edge, so each
// card's follow effect also listens here. Width only: the rail's height moves
// with the terminal drawer and the window, which the cards already follow.
// A hidden rail (0 wide) says nothing.

/** Calls `fn` with the rail's new width whenever it changes; returns the stop.
 *  Inert without a rail or a ResizeObserver (a test renderer). */
export function watchRailWidth(rail: Element | null, fn: (width: number) => void): () => void {
  if (!rail || typeof ResizeObserver === "undefined" || typeof rail.getBoundingClientRect !== "function") return () => {};
  let was = rail.getBoundingClientRect().width;
  const ro = new ResizeObserver(() => {
    const w = rail.getBoundingClientRect().width;
    if (w === was) return;
    was = w;
    if (w > 0) fn(w);
  });
  ro.observe(rail);
  return () => ro.disconnect();
}
