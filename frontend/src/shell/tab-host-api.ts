// The shell-tabs host's window API (`window._fusedTabs`, shell/TabHost.tsx).
//
// A TYPE ONLY, in a file of its own, because it is a contract with code that
// is not TypeScript: the macOS app (mac_window.py) evaluates JavaScript
// against this global for its menu items (⌘T, ⌘W, ⌘⇧[ ⌘⇧], ⌘1–9, Reload,
// Back/Forward, Print) and for "open this app in this window". Every method is
// SYNCHRONOUS so an `evaluateJavaScript` gets its answer in the same call — a
// promise would come back to Python as an opaque object.
//
// The global exists exactly while a TabHost is mounted: Python's probes read
// its absence as "this window has no tabs" and fall back to whole-window
// behaviour (close the window, navigate it), e.g.
//
//   (function(){var t=window._fusedTabs; if(!t) return 'win';
//     if(t.list().length<=1) return 'win'; t.close(); return 'tab';})()
//
// URLs in and out are same-origin path+query WITHOUT the tab's embed marker
// (`/apps/x`, `/explorer/view/a/b.html`, `/tasks`, `/`) — tabs-lib's `tabSrc`
// adds it, `stripTabMarker` removes it.

export interface FusedTabInfo {
  id: string;
  url: string;
  title: string;
  active: boolean;
}

export interface FusedTabsApi {
  /** Open `url` in a tab and return its id. `focusExisting` (default true)
   *  selects a tab already on that pathname instead of opening a second one;
   *  `activate` (default true) switches to it. */
  open(url: string, opts?: { activate?: boolean; focusExisting?: boolean }): string;
  /** Select by tab id or by url (pathname match). False when nothing matched. */
  select(idOrUrl: string): boolean;
  /** Close a tab (default: the active one). Closing the last opens a fresh Home. */
  close(id?: string): void;
  list(): FusedTabInfo[];
  /** Plain data (an evaluateJavaScript result): no Window in it. */
  active(): { id: string; url: string } | null;
  next(): void;
  prev(): void;
  /** 1-based, like ⌘1–9; past the end selects the last tab. */
  nth(n: number): void;
  reloadActive(): void;
  back(): void;
  forward(): void;
  navigateActive(url: string): void;
  print(): void;
  /** open("/", {focusExisting: false}) — ⌘T. */
  openHome(): string;
}
