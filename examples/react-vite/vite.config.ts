import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// fused-render serves the page at `/render?path=<abs index.html>`, not from
// the app folder, so a bare `<script src="./assets/x.js">` 404s. The page
// loads the bundle through `fused.rawUrl()` instead (see index.html), which
// means the build has to produce ONE JavaScript file with a FIXED name, and
// one stylesheet beside it:
//
//   dist/app.js   — React, the app, every chunk inlined
//   dist/app.css  — every imported stylesheet
//
// `vite build --watch` keeps both fresh while you edit; refresh the tab to
// pick up a rebuild.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Browser targets fused-render supports: current Chrome, Firefox, Safari
    // and the macOS WKWebView popover.
    target: "es2020",
    rollupOptions: {
      input: "src/main.tsx",
      output: {
        format: "es",
        entryFileNames: "app.js",
        assetFileNames: "app.[ext]",
        // A relative `import("./chunk.js")` inside the bundle would resolve
        // against the raw-file URL, not the folder, so there must be no
        // chunks at all.
        inlineDynamicImports: true,
      },
    },
  },
});
