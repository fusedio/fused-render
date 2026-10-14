// Window expandos shared with the injected runtime (runtime.js, plain JS):
//  - _fusedParamBoundary: set by the tab shell (TM-3); the runtime's ancestor
//    climb stops below a boundary-marked window.
//  - _fusedUrlHooked: per-document marker for the fused:urlchange hook
//    (lib/layout-codec.ts attachEmbedUrlChange).
//  - _fusedTabs: a shell-tabs host's API (shell/TabHost.tsx), the surface the
//    native menu's ⌘T/⌘W and mac_window.py evaluate against. Inline import
//    type: a top-level import would turn this ambient file into a module.
//  - _fusedTabHidden: written by that host on each tab's window — true while
//    the tab is not the active one (platform/lib/pause-while-hidden.ts).
interface Window {
  _fusedParamBoundary?: boolean;
  _fusedUrlHooked?: boolean;
  _fusedTabs?: import("./shell/tab-host-api").FusedTabsApi;
  _fusedTabHidden?: boolean;
}

// Baked in by vite.config.js `define` from fused_render/__init__.py — the
// version this bundle was built from, compared against /api/config's served
// version to detect a stale tab (server-status.ts).
declare const __BUILD_VERSION__: string;

// Vite handles CSS side-effect imports at build time.
declare module "*.css";

// Vite resolves image imports to their served URL.
declare module "*.png" {
  const url: string;
  export default url;
}
