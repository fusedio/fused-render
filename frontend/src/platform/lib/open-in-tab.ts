// "Open in a new tab" for every shell affordance, routed through the shell
// tabs host when one is above us (`shell_tabs_enabled`). Under shell tabs a
// native window's top-level document hosts each surface as an iframe tab and
// exposes `window._fusedTabs` (shell/tab-host-api.ts); a `window.open` from
// inside a tab would instead pop a separate native window (or a browser tab),
// which is exactly what the feature exists to stop. Without a host — flag
// off, a plain browser tab, `fused-render serve` — this is today's
// `window.open(url, "_blank")`, unchanged.
//
// Navigating IN PLACE (sidebar rows, spaLinkProps) is not this helper's job:
// the host's own navigation sink covers those.

// Structural, not imported: the host API lives with the tab host
// (shell/tab-host-api.ts) and this file only needs `open`. Keeping the type
// local keeps platform/lib free of a shell import.
interface TabHostLike {
  open(url: string, opts?: { activate?: boolean; focusExisting?: boolean }): string;
}

// The host is the top-level document of the window. Found the way
// notifications.ts finds `_fusedIngestNotification`: read it off `window.top`,
// try/caught because a cross-origin or sandboxed frame (snapshots, an app's
// own sandboxed iframe) throws on the property read. `window` itself is
// checked first: the sidebar lives in the host document, outside the tabs, so
// its own menus are callers too.
function tabHost(): TabHostLike | null {
  for (const w of [window, window.top]) {
    if (!w) continue;
    try {
      const api = (w as unknown as { _fusedTabs?: TabHostLike })._fusedTabs;
      if (api && typeof api.open === "function") return api;
    } catch {
      // Cross-origin/sandboxed: no host reachable from this frame.
    }
  }
  return null;
}

/** True when a shell tabs host is reachable — the window shows tabs. */
export function hasTabHost(): boolean {
  return tabHost() !== null;
}

/** Open `url` (a same-origin path+query: `/explorer/view/...`, an app's open
 *  url, or an `/explorer/embed/...` url, which the host keeps as an embed tab)
 *  as a new tab of the host when there is one — returns true — else in a new
 *  browser tab / native window via `window.open` — returns false.
 *  `focusExisting` asks the host to activate a tab already showing `url`
 *  instead of adding a duplicate. `features` is the third `window.open`
 *  argument, passed through EXACTLY as the call site passed it before this
 *  helper existed, so with no host the open is byte-for-byte today's. */
export function openInTab(
  url: string,
  opts?: { focusExisting?: boolean; features?: string },
): boolean {
  const host = tabHost();
  if (host) {
    try {
      // The host answers "" when it refused the url (not one it can make a
      // tab of); the plain open below still honours the click.
      if (host.open(url, { focusExisting: opts?.focusExisting ?? false })) return true;
    } catch {
      // Cross-origin/sandboxed or a host mid-teardown — the plain open below.
    }
  }
  if (opts?.features === undefined) window.open(url, "_blank");
  else window.open(url, "_blank", opts.features);
  return false;
}
