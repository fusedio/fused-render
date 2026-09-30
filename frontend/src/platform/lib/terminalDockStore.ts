// Whether the status-bar terminal drawer is open — shared between
// `TerminalDock.tsx` (the chip, the only writer) and `TerminalDrawer.tsx`
// (the reader, rendered as a separate sibling in App.tsx's tree; see
// PLAN-status-bar-terminal.md Task 5) without either one importing the
// other.
//
// A MODULE-LEVEL STORE, deliberately NOT `useStatusChip`/`useExclusiveSection`
// (`platform/lib/statusChip.ts`/`platform/lib/exclusiveSection.ts`): those
// give a chip hover-to-preview and "closes when a sibling section opens"
// behaviour, both wrong here — hover-to-preview on a surface you type into
// would open a shell under your pointer, and the drawer reserves its own
// height rather than floating over the other three panels' `.dl-panel`, so
// it has nothing to arbitrate space with (PLAN's Decisions). This is
// therefore its own minimal store, the same shape `apps/ai_models/lib/aiRuntime.ts`
// uses for a single shared value with no context provider — one boolean, one
// setter, subscribed to with `useSyncExternalStore` so a re-render only
// happens when the value actually changes.
import { useSyncExternalStore } from "react";
import { IS_EMBED } from "./router";
import { isWindows } from "./platform";

/** Whether `TerminalDrawer` is actually mounted right now. `!IS_EMBED &&
 * !isWindows` is necessary but not sufficient: App.tsx only mounts the
 * drawer on its main route, not on the onboarding route (the setup wizard is
 * a pre-app surface with no sidebar, status bar or docks) — a Run affordance
 * rendered there would hand a command to a drawer that does not exist.
 * `TerminalDrawer` registers itself here for exactly as long as it is
 * mounted (see `registerTerminalDrawerMounted`). */
let mounted = false;
const mountListeners = new Set<() => void>();

/** Called by `TerminalDrawer` in a mount effect; returns the cleanup to run
 * on unmount. Also used directly by tests that need `canRunInTerminal()`/
 * `useCanRunInTerminal()` to read true without rendering a real drawer. */
export function registerTerminalDrawerMounted(): () => void {
  mounted = true;
  for (const listener of mountListeners) listener();
  return () => {
    mounted = false;
    for (const listener of mountListeners) listener();
  };
}

function subscribeMounted(listener: () => void): () => void {
  mountListeners.add(listener);
  return () => mountListeners.delete(listener);
}

function canRunSnapshot(): boolean {
  return !IS_EMBED && !isWindows && mounted;
}

/** Whether a "run in terminal" affordance may show at all, read once and not
 * kept in sync with later mount/unmount — for an event handler or a
 * non-React module (markdown.ts) deciding what to do right now, not for
 * deciding what to render. A component or hook that decides whether to SHOW
 * a Run button must use `useCanRunInTerminal()` instead, so it re-renders
 * when the drawer mounts or unmounts (e.g. navigating to/from onboarding).
 * Every Run affordance in the app — the health strip, the trouble card, the
 * chat kebab, task/explorer "open in terminal", and the markdown/tool-chip
 * run buttons — goes through one of these two instead of re-deriving the
 * gate, so the day either constraint changes there is exactly one place to
 * edit. */
export function canRunInTerminal(): boolean {
  return canRunSnapshot();
}

/** The reactive form of `canRunInTerminal()` — subscribes to drawer
 * mount/unmount so a component re-renders the moment a Run affordance
 * should appear or disappear. */
export function useCanRunInTerminal(): boolean {
  return useSyncExternalStore(subscribeMounted, canRunSnapshot);
}

let open = false;
const listeners = new Set<() => void>();

function set(next: boolean): void {
  if (next === open) return;
  open = next;
  for (const listener of listeners) listener();
}

export function toggleTerminalDock(): void {
  set(!open);
}

export function closeTerminalDock(): void {
  set(false);
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot(): boolean {
  return open;
}

export function useTerminalDockOpen(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot);
}

/** A "open the drawer in this folder / run this command in it" request from
 * outside the drawer/chip (the explorer's "Open in Terminal" menu item,
 * `fused.terminal.open`/`.run` via the page-API bridge). `cwd`/`command` are
 * optional so a bare "just open the drawer" request can share the same
 * shape as a folder-scoped or command-scoped one. `execute` defaults to
 * true, so a request with no `execute` field runs immediately; pass
 * `execute: false` to have `command` typed at the prompt WITHOUT the
 * trailing Enter, for a model-suggested shell command a person should read
 * before it runs. */
export interface TerminalRequest {
  cwd?: string;
  command?: string;
  execute?: boolean;
}

// One slot, not a queue: a newer request replacing an unconsumed older one
// matches "open the terminal here" semantics (the user cares about the
// latest folder/command they asked for, not a backlog of every one they
// clicked before the drawer got around to reading it). Separate listener set
// from `listeners` above because `open` alone does not change on a second
// `openTerminal()` call while the drawer is already open — TerminalDrawer
// still needs to learn a new request arrived in that case.
let pendingRequest: TerminalRequest | null = null;
let pendingVersion = 0;
const pendingListeners = new Set<() => void>();

/** Open the drawer, optionally carrying a pending cwd/command request for
 * TerminalDrawer to consume once a session id is available. Passing no
 * `req` (or one with both fields undefined) just opens the drawer, same as
 * `toggleTerminalDock()` when already closed. */
export function openTerminal(req?: TerminalRequest): void {
  if (req && (req.cwd !== undefined || req.command !== undefined)) {
    pendingRequest = req;
    pendingVersion += 1;
    for (const listener of pendingListeners) listener();
  }
  set(true);
}

/** Read the pending request without consuming it — used to decide a
 * brand-new session's create-time `cwd` before a session id exists to send
 * input to. */
export function peekPendingTerminalRequest(): TerminalRequest | null {
  return pendingRequest;
}

/** Consume the pending request: returns it once, then clears the slot so a
 * second consumer (or a re-run effect) doesn't resend it. */
export function takePendingTerminalRequest(): TerminalRequest | null {
  const req = pendingRequest;
  pendingRequest = null;
  return req;
}

function subscribePending(listener: () => void): () => void {
  pendingListeners.add(listener);
  return () => pendingListeners.delete(listener);
}

function getPendingVersion(): number {
  return pendingVersion;
}

/** Bumps whenever a new pending request is recorded (including a repeat
 * request while the drawer is already open, when `sessionId` won't itself
 * change) — TerminalDrawer depends on this in the effect that consumes the
 * request, alongside `sessionId`. */
export function usePendingTerminalRequestVersion(): number {
  return useSyncExternalStore(subscribePending, getPendingVersion);
}

/** Test-only: reset between test files (bun runs the whole suite in one
 * module registry, and this store is module-level — see the header). */
export function resetTerminalDockForTests(): void {
  open = false;
  listeners.clear();
  pendingRequest = null;
  pendingVersion = 0;
  pendingListeners.clear();
  mounted = false;
  mountListeners.clear();
}
