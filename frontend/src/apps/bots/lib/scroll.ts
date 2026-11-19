// Bring an element into view inside ONE scroll container, never its ancestors. `el.scrollIntoView` scrolls every
// scrollable ancestor, the app shell's included (the whole page shifts up, its header cut off); these set only the
// given container's scrollTop, as the thread does (components/threadDom.ts).

/** The container's new scrollTop that brings [top, bottom) (in the container's content coordinates) into its
 *  viewport [viewTop, viewTop + viewH), moving as little as possible ("nearest"); a box taller than the viewport
 *  aligns its top. */
export function nearestScrollTop(top: number, bottom: number, viewTop: number, viewH: number): number {
  if (top < viewTop || bottom - top > viewH) return top;
  if (bottom > viewTop + viewH) return bottom - viewH;
  return viewTop;
}

/** Scroll `scroller` (and only it) so `el`, inside it, is in view. */
export function revealWithin(scroller: HTMLElement | null, el: HTMLElement | null): void {
  if (!scroller || !el || !scroller.contains(el)) return;
  const s = scroller.getBoundingClientRect(), r = el.getBoundingClientRect();
  const top = r.top - s.top - scroller.clientTop + scroller.scrollTop;
  scroller.scrollTop = nearestScrollTop(top, top + r.height, scroller.scrollTop, scroller.clientHeight);
}
