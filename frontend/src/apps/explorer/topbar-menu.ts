// WHO ANSWERS A RIGHT-CLICK ON THE CRUMB BAR — the view underneath it.
//
// The bar itself (Breadcrumb.tsx) knows a path and nothing else. The menu a
// right-click on it should open is the CONTENT's: over a folder that is the
// listing's own header menu, built from a clipboard, a refetch and a dialog
// stack the bar cannot see (listing/useFileOps); over a file it is the preview's
// file menu, whose Rename drives a prompt dialog the preview owns (Preview's
// usePreviewFileMenu). Both already keep menu state, render a <ContextMenu>, and
// hold the overlay count that makes the listing's keyboard shortcuts stand down
// while a menu is open — so the bar does not build a menu at all. It asks the
// owner to open one at the cursor.
//
// A module store rather than props for the reason folder-chrome.ts is one: the
// bar and the content are SIBLINGS under App, and which of them the content
// resolves to is decided several levels below the bar, after it has rendered.
//
// No subscribe/snapshot pair here, unlike the slot stores: nothing RENDERS from
// this. The opener is read at event time, inside the contextmenu handler, so a
// publish must not (and does not) re-render the bar.
//
// A STACK with newest-wins, for the same reason folder-chrome keeps one: a
// folder→file (or folder→folder) swap holds both views mounted for a commit,
// and a straight assign-on-publish/clear-on-release would leave the bar either
// pointing at the outgoing view or at nothing, depending on effect order. Each
// publisher gets its own release, idempotent, called by the effect that made it.
//
// Only the view that OWNS the bar publishes — the folder listing that claimed
// the chrome, or the preview whose actions are in the topbar. A preview pane
// inside a folder view, or a listing embedded in one, has its own chrome and
// must not answer for the window's bar.
export type TopbarMenuOpener = (x: number, y: number) => void;

type Publisher = { open: TopbarMenuOpener };

let publishers: Publisher[] = [];

export function publishTopbarMenu(open: TopbarMenuOpener): () => void {
  const p: Publisher = { open };
  publishers.push(p);
  let released = false;
  return () => {
    if (released) return;
    released = true;
    const i = publishers.indexOf(p);
    if (i >= 0) publishers.splice(i, 1);
  };
}

// Opens the current owner's menu at the cursor. Returns false when nobody owns
// the bar (a static label bar, a view still resolving its stat) — the caller
// then leaves the event alone, so the browser's own menu shows rather than
// nothing at all.
export function openTopbarMenu(x: number, y: number): boolean {
  const owner = publishers[publishers.length - 1];
  if (!owner) return false;
  owner.open(x, y);
  return true;
}

// Test seam: the store outlives any component.
export function resetTopbarMenu(): void {
  publishers = [];
}
