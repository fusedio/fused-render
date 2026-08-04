import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

const src = (p) => fileURLToPath(new URL("./src/" + p, import.meta.url));

// Build output ships inside the Python package (like the vendored template
// libs): `pip install` needs no node. The server serves the built shell for
// `/`, `/view/*` and `/embed/*`; assets resolve via the absolute base below.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@shell": src("shell"),
      "@platform": src("platform"),
      "@apps": src("apps"),
      "@assets": src("assets"),
    },
  },
  base: "/static/shell-dist/",
  build: {
    outDir: "../fused_render/static/shell-dist",
    emptyOutDir: true,
  },
  server: {
    // `npm run dev` proxies API/render traffic to a running fused-render
    // server for hot-reload development of the shell itself.
    proxy: {
      "/api": "http://127.0.0.1:1777",
      "/render": "http://127.0.0.1:1777",
      "/static": "http://127.0.0.1:1777",
      "/template-assets": "http://127.0.0.1:1777",
    },
  },
});
