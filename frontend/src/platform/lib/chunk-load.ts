/**
 * The messages a failed dynamic import rejects with: Chromium, WebKit, and
 * webpack/vite-style chunk loaders' own names for it.
 *
 * Platform-level (not `apps/claude`) because two boundaries ask the question:
 * the chat's own `ChatChunkBoundary` and the shell-wide `ShellErrorBoundary`
 * that catches every OTHER lazy route's rejection. A hashed chunk that is no
 * longer on disk — a tab open across an update, a request landing in the
 * seconds of a relaunch — is the one render-time throw whose fix is always
 * "reload", so the boundaries word it that way only when the message says so.
 */
const CHUNK_LOAD = /Failed to fetch dynamically imported module|Importing a module script failed|ChunkLoadError|Loading chunk|error loading dynamically imported module/i;

/** Did this render-time throw come from a chunk failing to load? */
export function isChunkLoadError(message: string): boolean {
  return CHUNK_LOAD.test(message);
}
