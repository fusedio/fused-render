// Window expandos shared with the injected runtime (runtime.js, plain JS):
//  - _fusedParamBoundary: set by the tab shell (TM-3); the runtime's ancestor
//    climb stops below a boundary-marked window.
//  - _fusedUrlHooked: per-document marker for the fused:urlchange hook
//    (lib/layout-codec.ts attachEmbedUrlChange).
interface Window {
  _fusedParamBoundary?: boolean;
  _fusedUrlHooked?: boolean;
}

// Vite handles CSS side-effect imports at build time.
declare module "*.css";
