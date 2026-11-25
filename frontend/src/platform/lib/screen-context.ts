// WHAT THE WINDOW SHOWS BEHIND A FLOATING CHAT — a bot's rail card (or an
// expert's chat, wherever it is mounted off the Bots page) is drawn over
// another page of the same document, and the user talks to it about that page
// ("summarise this file", "what is this folder"). This is the few lines of
// text that tell the bot where the user stands: the page's title and URL, and
// the place the Claude rail stands on (a folder, a file, a canvas and its
// selected node) when there is one.
//
// A DESCRIPTION OF A LOCATION, never contents: no file text, no pixels. A bot
// that may read the place does so with its own tools.
//
// Read at SEND time (a pinned card outlives page hops), by the bots composer
// (`screen` on /api/bots/<id>/send) and the expert chat (the `screen` field of
// the `terminal_hint` param, which agent.py writes as a `<user-screen>` line).
import { currentUrl, fsPathFromLocation, IS_EMBED } from "@platform/lib/router";

/** Where the rail stands, as the shell's rail last said it (`RailPlace`,
 *  flattened: platform cannot import the shell). */
export type ScreenPlace =
  | { kind: "folder"; dir: string; word?: string }
  | { kind: "file"; file: string }
  | { kind: "canvas"; name: string; env: string; dir?: string | null; node?: string | null };

let place: ScreenPlace | null = null;

/** The top rail's place, said on every hop and node selection (ClaudeRail);
 *  null when the rail stands on no page. */
export function publishScreenPlace(next: ScreenPlace | null): void {
  place = next;
}

const FIELD_MAX = 300;

/** One value as single-line text, capped (the server cleans it again). */
function one(value: string | null | undefined): string {
  const text = (value || "").replace(/\s+/g, " ").trim();
  return text.length <= FIELD_MAX ? text : text.slice(0, FIELD_MAX - 1) + "…";
}

function placeLines(p: ScreenPlace): string[] {
  if (p.kind === "folder") return [`${p.word || "Folder"}: ${one(p.dir)}`];
  if (p.kind === "file") return [`File: ${one(p.file)}`];
  const lines = [`Canvas: ${one(p.name)} (${one(p.env)})`];
  if (p.dir) lines.push(`Canvas folder on disk: ${one(p.dir)}`);
  if (p.node) lines.push(`Selected node: ${one(p.node)}`);
  return lines;
}

/** The lines describing the page this document shows now, or "" when there is
 *  nothing to say (an embed, no DOM). Every field is optional but the URL. */
export function screenContext(): string {
  try {
    if (IS_EMBED || typeof document === "undefined") return "";
    const lines: string[] = [];
    const title = one(document.title);
    if (title) lines.push(`Page: ${title}`);
    lines.push(`URL path: ${one(currentUrl())}`);
    if (place) lines.push(...placeLines(place));
    else {
      const fs = fsPathFromLocation();
      if (fs) lines.push(`Path: ${one(fs)}`);
    }
    return lines.join("\n");
  } catch {
    return "";
  }
}
