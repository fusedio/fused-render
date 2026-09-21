// The "explain with AI" hand-off every system/runtime ErrorBanner (see
// ErrorBanner.tsx's `onExplain` prop) uses to open a Claude chat seeded with
// the error — SPEC-doctor-git-ai-errors.md Part A. Deliberately narrow:
// this ONLY explains, it never fixes. Unlike RepoUpdatesDock.tsx's
// `repoFixPrompt` (repo-updates-lib.ts), whose prompt tells Claude to
// "explain... then fix it", a click on this action must not start edits —
// the prompt below explicitly tells Claude to stop after explaining.
import { getConfig } from "@platform/lib/api";
import { stageClaudeAsk } from "@apps/explorer/lib/pending-claude-ask";
import { navigate } from "@platform/lib/router";

/**
 * Builds the seeded prompt. `context` is free text a call site adds on top
 * of the raw message — e.g. which action was being attempted, or a curated
 * remedy already shown in the banner — never a second copy of the message
 * itself.
 */
export function explainErrorPrompt(message: string, context?: string): string {
  const parts = [`An error appeared in the app. The message shown was:\n${message}`];
  if (context) parts.push(context);
  parts.push(
    "Explain what this means and why it likely happened, in plain terms. " +
      "Do not fix anything or make any changes yet — just help me understand " +
      "the error first.",
  );
  return parts.join("\n\n");
}

// Module-level cache for the one config fetch every folderless call site
// needs purely to find a folder to open a chat in — modeled on
// `home-path.ts`'s `cachedHome`/`inFlight` pair (same rationale: several
// folderless surfaces asking for this at once should share one round trip,
// not each fire their own). `undefined` means unresolved; a failed fetch
// leaves it unresolved so a later call gets to try again rather than being
// stuck on one failed attempt forever.
let cachedDefaultFolder: string | undefined;
let inFlight: Promise<string | undefined> | null = null;

function fetchDefaultFolder(): Promise<string | undefined> {
  if (inFlight) return inFlight;
  inFlight = getConfig()
    .then((c) => {
      cachedDefaultFolder = c.fused_dir;
      return cachedDefaultFolder;
    })
    .catch(() => {
      inFlight = null;
      return undefined;
    });
  return inFlight;
}

/** The folder a folderless surface (AI Models, settings) hands off to. */
export function resolveDefaultFolder(): Promise<string | undefined> {
  if (cachedDefaultFolder !== undefined) return Promise.resolve(cachedDefaultFolder);
  return fetchDefaultFolder();
}

// Test seam: the cache outlives any component/test file, so a test that
// resolves it must be able to put it back for the next one.
export function resetDefaultFolderCache(): void {
  cachedDefaultFolder = undefined;
  inFlight = null;
}

/**
 * Opens a Claude chat seeded with `prompt`. `folderPath` is the surface's
 * own folder-scoped chat (pass it whenever the surface has one); omit it on
 * a folderless page and this resolves the default folder
 * (`Config.fused_dir`) instead. A resolve failure (no config, no fused_dir)
 * is a silent no-op — there is no folder to stage the ask against, and
 * failing loudly over a "explain this error" click would just be a second,
 * more confusing error.
 */
export async function explainWithAi(prompt: string, folderPath?: string): Promise<void> {
  const path = folderPath ?? (await resolveDefaultFolder());
  if (!path) return;
  stageClaudeAsk(path, prompt);
  navigate(path, { isDir: true });
}
