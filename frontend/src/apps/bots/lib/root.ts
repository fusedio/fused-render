// The page's DOM roots inside the shell. FusedBot owned the whole document, so its layout classes and CSS variables
// sat on <body> and its portals went to document.body. Here the page is one route among many: styles/bots.css is
// scoped under `.bots-page`, so
//   * the layout plumbing (lib/layout.ts) writes its classes and variables on the route's root element, and
//   * a portal targets one `.bots-page.bots-portal-host` element appended to <body> while the route is mounted
//     (a portal straight to <body> would sit outside the scope and render unstyled).

let root: HTMLElement | null = null;
let host: HTMLElement | null = null;
// Stand-in while nothing is mounted (late timers, tests): class/style writes land on a detached node and vanish.
let detached: HTMLElement | null = null;

/** App's ref callback: the `.bots-page` element, or null on unmount. */
export function setBotsRoot(el: HTMLElement | null): void { root = el; }

/** The mounted `.bots-page` root (a detached stand-in when there is none, so callers never null-check). */
export function botsRoot(): HTMLElement {
  if (root) return root;
  if (!detached && typeof document !== "undefined") detached = document.createElement("div");
  return detached as HTMLElement;
}

/** True while the Bots route is on screen. */
export const botsMounted = (): boolean => root !== null && root.isConnected;

/** The portal target: ONE element for the module's life (a portal keeps the node it rendered into), attached to
 *  <body> on first use (portals render before effects run) and by App's mount effect, detached on unmount. */
export function portalHost(): HTMLElement {
  if (!host) {
    host = document.createElement("div");
    host.className = "bots-page bots-portal-host";
  }
  if (!host.isConnected) document.body.appendChild(host);
  return host;
}

/** App unmount: take the portal host off <body> with the route (the next mount re-attaches the same node). */
export function detachPortalHost(): void { host?.remove(); }
