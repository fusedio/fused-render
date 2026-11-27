// Window expandos shared with the injected runtime (runtime.js, plain JS):
//  - _fusedParamBoundary: set by the tab shell (TM-3); the runtime's ancestor
//    climb stops below a boundary-marked window.
//  - _fusedUrlHooked: per-document marker for the fused:urlchange hook
//    (lib/layout-codec.ts attachEmbedUrlChange).
//  - _fusedParamOpaque: set by a workspace host (D1514) beside the boundary;
//    the runtime's params read climb stops BEFORE such a window — its URL
//    mirrors whichever tab is active, so its params are no frame's globals.
//  - _fusedWorkspace: a workspace host's API (platform/lib/workspace-bridge);
//    its frames reach it as `parent._fusedWorkspace`. Inline import type: a
//    top-level import would turn this ambient file into a module.
//  - _fusedTabHidden: written by that host on each frame's window — true
//    while the frame is not shown (platform/lib/pause-while-hidden.ts).
interface Window {
  _fusedParamBoundary?: boolean;
  _fusedParamOpaque?: boolean;
  _fusedUrlHooked?: boolean;
  _fusedWorkspace?: import("./platform/lib/workspace-bridge").WorkspaceHostApi;
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
