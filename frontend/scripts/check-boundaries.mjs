// Import-boundary check for the shell/platform/apps layering (no eslint dep).
//
//   platform/**   may import: platform, assets            (never shell or apps)
//   apps/<x>/**   may import: platform, assets, apps/<x>, and the shared apps
//                 listed in SHARED_APPS   (never shell or any other app)
//   shell/**      may import: anything (it composes the apps)
//   src root      may import: anything (entry files)
//
// Run via `npm run check:boundaries`; wired into `npm run build`.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SRC = fileURLToPath(new URL("../src", import.meta.url));

// All layer/spec comparisons use forward slashes; path.relative/normalize
// emit backslashes on Windows, so normalize every derived path through this.
const posix = (p) => p.split(path.sep).join("/");

const files = [];
(function walk(d) {
  for (const e of fs.readdirSync(d, { withFileTypes: true })) {
    const p = path.join(d, e.name);
    if (e.isDirectory()) walk(p);
    else if (/\.(tsx|ts)$/.test(e.name)) files.push(p);
  }
})(SRC);

// Layer of a src-relative path: "platform", "shell", "apps/<name>", or null (root).
// An app's package root ("apps/explorer", from a bare "@apps/explorer" import) is
// part of that app's layer, hence the optional trailing segment.
function layerOf(rel) {
  if (rel === "platform" || rel.startsWith("platform/")) return "platform";
  if (rel === "shell" || rel.startsWith("shell/")) return "shell";
  const m = rel.match(/^apps\/([^/]+)(\/|$)/);
  if (m) return "apps/" + m[1];
  return null;
}

// Resolve an import specifier to a src-relative path, or null for externals.
function resolveSpec(spec, fileRel) {
  if (spec.startsWith("@platform/")) return "platform/" + spec.slice("@platform/".length);
  if (spec.startsWith("@shell/")) return "shell/" + spec.slice("@shell/".length);
  if (spec.startsWith("@apps/")) return "apps/" + spec.slice("@apps/".length);
  if (spec.startsWith("@assets/")) return "assets/" + spec.slice("@assets/".length);
  if (spec.startsWith(".")) return posix(path.normalize(path.join(path.dirname(fileRel), spec)));
  return null; // bare import: node_modules
}

/**
 * Apps that are EMBEDDED SURFACES rather than routes: another app hosts them as
 * a pane, the way the shell hosts a route. `apps/claude` is the only one — the
 * Claude chat is framed by the explorer's sidebar, its folder listing pane, its
 * content pane and the canvases workspace, and every one of those is an app.
 *
 * Not lifted into `platform/` instead, which was the alternative: platform may
 * not import apps either (the rule above), so a `platform/ui/ChatMount.tsx`
 * re-export would need this same exception pointing the other way — and it would
 * put a 20-file subsystem with its own protocol, panes and styles in the layer
 * that is meant to hold the pieces every app shares.
 *
 * A shared app is still an APP: it imports platform and itself and nothing else,
 * which the loop below enforces by EXCLUDING a shared app from the shared-app
 * allowance — so `apps/explorer → apps/claude` passes while `apps/claude →`
 * any other app does not, whatever else SHARED_APPS grows to hold.
 */
const SHARED_APPS = new Set(["apps/claude"]);

const violations = [];
for (const file of files) {
  const fileRel = posix(path.relative(SRC, file));
  const from = layerOf(fileRel);
  if (from === null || from === "shell") continue; // root + shell may import anything
  const text = fs.readFileSync(file, "utf8");
  for (const m of text.matchAll(/(?:from\s+|import\s+|import\()["']([^"']+)["']/g)) {
    const target = resolveSpec(m[1], fileRel);
    if (target === null || target.startsWith("assets/")) continue;
    const to = layerOf(target);
    // The shared-app allowance belongs to APP importers only, and never to a
    // shared app's own imports:
    //   * `platform/**` files are walked too (the loop skips only the root and
    //     shell), so an unscoped clause let a `platform/ui/ChatHost.tsx` import
    //     `@apps/claude` — contradicting this file's own header AND the argument
    //     in the SHARED_APPS comment, which declines to lift the chat into
    //     `platform/` precisely because platform may not import apps;
    //   * a shared app is still an app: without excluding `from`, the moment
    //     SHARED_APPS holds two entries they may import each other, which is the
    //     cross-app coupling this whole script exists to prevent.
    const sharedAllowed =
      from.startsWith("apps/") && !SHARED_APPS.has(from) && to !== null && SHARED_APPS.has(to);
    const allowed = to === "platform" || to === from || sharedAllowed;
    if (!allowed) violations.push(`${fileRel}: imports "${m[1]}" (${to ?? "src root"}) — not allowed from ${from}`);
  }
}

if (violations.length) {
  console.error("Import-boundary violations:\n" + violations.map((v) => "  " + v).join("\n"));
  console.error(
    "\nRules: platform imports only platform; an app imports only platform, itself and the shared apps (" +
      [...SHARED_APPS].join(", ") +
      "); shell may import anything.",
  );
  process.exit(1);
}
console.log(`boundaries OK (${files.length} files)`);
