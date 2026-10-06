// The thread's DOM helpers (OpenBot chat.js): "at the end" measurement, the anchors a rebuild restores, pinning to the
// end, and updateToBottom (the pinned flag + retiring the pill's count). Shared by Thread, ToBottom and Composer.
import { cur, eventsOf, getState, setNewCount, setPinned, viewedSet } from "../state/store";

/** Within this many px of the bottom counts as "at the end" (following along). */
export const END_GAP = 80;

/** Distance from the bottom of the scrolled content. */
export const gapOf = (th: HTMLElement): number => th.scrollHeight - th.scrollTop - th.clientHeight;

/** An .ev wrapper is display:contents, so it has no box: the message inside it carries the height and the position. */
export const evBox = (el: Element | null): HTMLElement | null =>
  el ? (el.querySelector(":scope > :not(.day):not(.new)") as HTMLElement | null) : null;

export interface Anchor { seq: string; y: number }
/**
 * The first few messages on screen and where each sits inside the thread's box — what a rebuild has to reproduce.
 * Reading the oldest history, the topmost one can itself be trimmed away by the cap, so the ones under it come along as fallbacks.
 */
export function topVisible(th: HTMLElement, want = 3): Anchor[] {
  const top = th.getBoundingClientRect().top, out: Anchor[] = [];
  for (const el of th.querySelectorAll<HTMLElement>(".ev[data-seq]")) {
    const box = evBox(el); if (!box || !box.offsetHeight) continue;  // no box, or hidden by the search filter: nothing to anchor on
    const y = box.getBoundingClientRect().top - top;
    if (y + box.offsetHeight > 0 && out.push({ seq: el.dataset.seq || "", y }) === want) break;
  }
  return out;
}

/** Put the first surviving anchor back where it was. False when none survived, so the caller can fall back. */
export function restoreAnchor(th: HTMLElement, anchors: Anchor[] | null): boolean {
  for (const a of anchors || []) {
    const box = evBox(th.querySelector(`.ev[data-seq="${CSS.escape(a.seq)}"]`));
    if (!box) continue;
    th.scrollTop += box.getBoundingClientRect().top - th.getBoundingClientRect().top - a.y;
    return true;
  }
  return false;
}

/** Scroll to the very end now and again next frame: late layout (fonts, the composer's ctl row toggling) settles after the write. */
export function pinToEnd(th: HTMLElement, botId: string | undefined): void {
  th.scrollTop = th.scrollHeight;
  requestAnimationFrame(() => { if (th.dataset.bot === botId) th.scrollTop = th.scrollHeight; });
}

/** Every event of the bot counts as viewed (the pill has done its job). */
export function viewAll(id: string): void {
  const set = viewedSet(id);
  for (const x of eventsOf(id)) set.add(x.seq);
}

/**
 * After a scroll or a render: record whether the thread sits at its end (state.pinned). At the end everything above is
 * behind you, so retire the count here too: the observer alone could not, because a message taller than the viewport
 * never reaches its 40% visibility threshold, so the pill stuck on while a long reply streamed in.
 */
export function updateToBottom(th: HTMLElement | null): void {
  if (!th) return;
  const atEnd = gapOf(th) <= END_GAP, b = cur();
  setPinned(atEnd);
  if (b && atEnd && getState().newCount) { viewAll(b.id); setNewCount(0); }
}
