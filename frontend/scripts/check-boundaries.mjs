// Import-boundary check for the shell/platform/apps layering (no eslint dep).
//
//   platform/**   may import: platform, assets            (never shell or apps)
//   apps/<x>/**   may import: platform, assets, apps/<x>  (never shell or other apps)
//   shell/**      may import: anything (it composes the apps)
//   src root      may import: anything (entry files)
//
// Run via `npm run check:boundaries`; wired into `npm run build`.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SRC = fileURLToPath(new URL("../src", import.meta.url));

const files = [];
(function walk(d) {
  for (const e of fs.readdirSync(d, { withFileTypes: true })) {
    const p = path.join(d, e.name);
    if (e.isDirectory()) walk(p);
    else if (/\.(tsx|ts)$/.test(e.name)) files.push(p);
  }
})(SRC);

// Layer of a src-relative path: "platform", "shell", "apps/<name>", or null (root).
function layerOf(rel) {
  if (rel.startsWith("platform/")) return "platform";
  if (rel.startsWith("shell/")) return "shell";
  const m = rel.match(/^apps\/([^/]+)\//);
  if (m) return "apps/" + m[1];
  return null;
}

// Resolve an import specifier to a src-relative path, or null for externals.
function resolveSpec(spec, fileRel) {
  if (spec.startsWith("@platform/")) return "platform/" + spec.slice("@platform/".length);
  if (spec.startsWith("@shell/")) return "shell/" + spec.slice("@shell/".length);
  if (spec.startsWith("@apps/")) return "apps/" + spec.slice("@apps/".length);
  if (spec.startsWith("@assets/")) return "assets/" + spec.slice("@assets/".length);
  if (spec.startsWith(".")) return path.normalize(path.join(path.dirname(fileRel), spec));
  return null; // bare import: node_modules
}

const violations = [];
for (const file of files) {
  const fileRel = path.relative(SRC, file);
  const from = layerOf(fileRel);
  if (from === null || from === "shell") continue; // root + shell may import anything
  const text = fs.readFileSync(file, "utf8");
  for (const m of text.matchAll(/(?:from\s+|import\s+|import\()["']([^"']+)["']/g)) {
    const target = resolveSpec(m[1], fileRel);
    if (target === null || target.startsWith("assets/")) continue;
    const to = layerOf(target);
    const allowed = to === "platform" || to === from;
    if (!allowed) violations.push(`${fileRel}: imports "${m[1]}" (${to ?? "src root"}) — not allowed from ${from}`);
  }
}

if (violations.length) {
  console.error("Import-boundary violations:\n" + violations.map((v) => "  " + v).join("\n"));
  console.error("\nRules: platform imports only platform; an app imports only platform + itself; shell may import anything.");
  process.exit(1);
}
console.log(`boundaries OK (${files.length} files)`);
