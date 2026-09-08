// Agent / engine failures → the `Trouble` the UI shows a card for.
//
// The CLASSIFIER IS NOT REIMPLEMENTED HERE. `platform/lib/trouble.ts` already
// holds the two-tier matcher (unconditional NAMED phrases, plus SHAPE patterns
// that only count when the message is ABOUT Claude), the copy blocks and the
// deep links, and `tests/test_trouble_parity.py` pins those strings against
// T:13511-13602's copy of them. This file only maps that verdict onto the chat's
// own kinds and adds the two failures the template surfaces separately:
//
//   * `needs-install` — the project venv is not built yet. The chat shows this
//     as a card rather than running the installer (protocol/agent.ts's
//     `AgentNeedsInstall`, design.md §4).
//   * `unknown-run` — the poll refused the run id: a bookmarked mid-run URL, a
//     pruned tmp, or a run that belongs to another target (T:17788-17796,
//     agent.py:3802/3824).
//
// Everything else lands on the platform kinds. `raw` splits three ways, because
// "the fetch never left the machine" and "agent.py raised" are different news
// for the reader even though the template drew both as its plain red row.
import {
  CLAUDE_INSTALL_COMMAND,
  troubleHelpUrl,
  troubleInstructions,
  troubleKind as platformTroubleKind,
  troubleReport,
  type TroubleKind as PlatformTroubleKind,
} from "@platform/lib/trouble";

import { AgentError, AgentNeedsInstall } from "./agent";
import type { Trouble, TroubleKind } from "./controller-api";

export { CLAUDE_INSTALL_COMMAND, troubleHelpUrl };

/** T:17790 / agent.py:3802 — poll's own refusals, matched verbatim. */
export const UNKNOWN_RUN_ERROR = "unknown run_id";
export const OTHER_TARGET_ERROR = "run is for another target";

/** Either refusal means "nothing to attach to" and takes the same recovery
 *  (clear the `run` param, resolve stragglers) — T:17788-17796. */
export function isUnknownRun(error: string | null | undefined): boolean {
  const e = String(error || "");
  return e === UNKNOWN_RUN_ERROR || e === OTHER_TARGET_ERROR;
}

// A failure that never reached the server at all: `fetch` rejects with these,
// and they say nothing about Claude. Worth their own kind because the answer is
// "is fused-render still running", not anything about the CLI — which is the
// first of TROUBLE_STEPS.raw's own instructions (T:13584).
const NETWORK = /failed to fetch|networkerror|load failed|network request failed|err_connection/i;

/** The chat kind for a message the platform classifier calls `raw`. */
function rawKind(message: string, fromAgent: boolean): TroubleKind {
  if (NETWORK.test(message)) return "network";
  return fromAgent ? "engine" : "generic";
}

/** platform kind → the chat's. `notfound` is the CLI missing; `login` and
 *  `limit` keep their names. */
function fromPlatform(kind: PlatformTroubleKind, message: string, fromAgent: boolean): TroubleKind {
  if (kind === "notfound") return "cli-missing";
  if (kind === "login") return "login";
  if (kind === "limit") return "limit";
  return rawKind(message, fromAgent);
}

/** The platform kind a `Trouble` should be RENDERED as — what
 *  `platform/ui/TroubleCard.tsx` takes. The chat's extra kinds have no card copy
 *  of their own, so they fall to `raw`, whose card is the error plus the two
 *  copy buttons. */
export function platformKindOf(kind: TroubleKind): PlatformTroubleKind {
  if (kind === "cli-missing") return "notfound";
  if (kind === "login") return "login";
  if (kind === "limit") return "limit";
  return "raw";
}

export interface TroubleContext {
  /** The chat target, for the copy blocks (T:13627 "Chat target: …"). */
  file: string | null;
  /** `location.pathname + location.search`, when the host can say (T:13633). */
  page?: string;
}

/** T:13692 — "using the chat on <FILE|this folder>". */
export function troubleWhat(file: string | null): string {
  return "using the chat on " + (file || "this folder");
}

/** Build a `Trouble` from a message and an explicit kind. */
export function troubleOf(kind: TroubleKind, message: string, detail?: string): Trouble {
  return detail ? { kind, message, detail } : { kind, message };
}

/**
 * Classify a plain error MESSAGE — poll's `error` field, a `start`/`send`
 * refusal, `data.error` on the ending poll (T:13698 `addError`).
 *
 * `fromAgent` says whether agent.py itself raised (a traceback exists), which is
 * what separates `engine` from `generic`.
 */
export function troubleFromMessage(message: string, fromAgent = false, detail?: string): Trouble {
  const text = String(message || "");
  if (isUnknownRun(text)) return troubleOf("unknown-run", text);
  return troubleOf(fromPlatform(platformTroubleKind(text), text, fromAgent), text, detail);
}

/**
 * Classify a THROWN failure: `AgentNeedsInstall`, `AgentError` (agent.py raised
 * — `ok:false` with a traceback, D69), an abort, or anything else.
 */
export function troubleFromError(err: unknown): Trouble {
  if (err instanceof AgentNeedsInstall) {
    return troubleOf("needs-install", err.message, err.needs.requirements.join("\n") || undefined);
  }
  if (err instanceof AgentError) {
    return troubleFromMessage(err.message, true, err.traceback);
  }
  const message = err instanceof Error ? err.message : String(err);
  return troubleFromMessage(message, false);
}

/** The text behind "Copy the details" (T:13630 `troubleReport`). */
export function troubleDetailsText(t: Trouble, ctx: TroubleContext): string {
  return troubleReport({
    what: troubleWhat(ctx.file),
    error: t.detail || t.message,
    page: ctx.page,
  });
}

/** The text behind "Copy Claude Code instructions" (T:13617
 *  `troubleInstructions`). The brief always has to say how to FIND the
 *  installation: this side never knows where the app lives. */
export function troubleInstructionsText(t: Trouble, ctx: TroubleContext): string {
  return troubleInstructions({
    what: troubleWhat(ctx.file),
    error: t.detail || t.message,
    page: ctx.page,
  });
}

/** The help deep link for a trouble (T:13683). */
export function troubleLink(t: Trouble): string {
  return troubleHelpUrl(platformKindOf(t.kind));
}
