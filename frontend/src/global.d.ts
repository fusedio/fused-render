// Window expandos shared with the injected runtime (runtime.js, plain JS):
//  - _fusedParamBoundary: set by the tab shell (TM-3); the runtime's ancestor
//    climb stops below a boundary-marked window.
//  - _fusedUrlHooked: per-document marker for the fused:urlchange hook
//    (lib/layout-codec.ts attachEmbedUrlChange).
interface Window {
  _fusedParamBoundary?: boolean;
  _fusedUrlHooked?: boolean;
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
