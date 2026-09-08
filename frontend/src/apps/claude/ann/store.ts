// THE NOTES THEMSELVES: the list, the param it survives in, and the four ways
// notes leave it (edited, removed, discarded, resolved after a clean send).
// T:6600-6620 (`annLoad`/`annSave`), 6580 (`annRoundStart`), 8401
// (`annNotesDiscard`), 10608 (`annResolveSent`).
//
// Plain TS with one injected `ParamsStore`, no React: `useAnnotations.ts`
// subscribes. That split is what lets the send path (which runs outside any
// render) stamp `label`/`sent` on the very list the chips are drawn from.
//
// `paneview` is NOT here even though it is one of the three params this
// subsystem reads. `pane/useNarrowView.ts` owns it already — the narrow view IS
// that param — and a second writer for one key is the bug D146 names in another
// costume. `mode.ts` consumes `narrowView.view` through a callback instead.
import type { ParamsStore } from "../params/store";
import { labelFor, resolveIn } from "./geometry";
import type { Annotation } from "./types";

/** T:6600 — the param's key. */
export const ANN_PARAM = "annotations";
/** T:7731 — "1" comment, "2" recording, anything else off. */
export const ANN_MODE_PARAM = "annmode";

/** T:6600 `annLoad`. Forgiving on purpose: the param is user-editable text in a
 *  URL, and a malformed one means "no notes", never a broken chat. */
export function parseAnnotations(raw: string | undefined | null): Annotation[] {
  try {
    const arr: unknown = JSON.parse(raw || "[]");
    if (!Array.isArray(arr)) return [];
    return arr.filter((c): c is Annotation => !!c && typeof c === "object");
  } catch {
    return [];
  }
}

/** T:6612 `annSave`'s payload — `JSON.stringify` of the list, verbatim, so a
 *  param written by the template reads here and the other way round. */
export function serializeAnnotations(list: readonly Annotation[]): string {
  return JSON.stringify(list);
}

export interface AnnStoreOptions {
  params: ParamsStore;
  /**
   * T:6606 — the HOSTED layout boots EMPTY. A sidebar teardown is a mode switch
   * as often as it is a reload and the param cannot say which, so re-hydrating
   * would resurrect notes anchored to a pane the reader has since left.
   */
  hosted?: boolean;
  /** Injected in tests. */
  now?: () => number;
  /** `crypto.randomUUID` in the browser (T:7419); injected in tests so ids are
   *  assertable. */
  newId?: () => string;
}

export interface AnnStore {
  /** The list, as one stable array identity per change — `useSyncExternalStore`
   *  requires a snapshot that does not change while nothing has. */
  list(): readonly Annotation[];
  /** T:6608 `annPending` — the unsent ones, which are the send payload. */
  pending(): Annotation[];
  /** Fires after every write that changed something. */
  subscribe(cb: (list: readonly Annotation[]) => void): () => void;

  /** T:6580 `annRoundStart` — when the mode was last armed. The pin gate. */
  roundStart(): number;
  /** T:7631 — a fresh round over a clean app. Called by `mode.ts` on every arm,
   *  never by the UI. */
  startRound(at?: number): void;

  /** T:7419 `annCommit`'s new-note branch. Returns the note it saved. */
  add(note: Omit<Annotation, "id" | "createdAt"> & { id?: string; createdAt?: number }): Annotation;
  /** T:7424 — an EXISTING pending note's words. A sent note is Claude's already
   *  and is left alone. */
  edit(id: string, content: string): void;
  /** T:7009 (the chip's ✕) and T:7409 (the editor's Delete). */
  remove(id: string): void;
  /** T:8392 — a discarded recording's marks, all of them, in one write. */
  removeMany(ids: Iterable<string>): void;
  /** T:5867 step 1a of `enterNoPane`: the notes point at a document this target
   *  does not have. */
  clear(): void;

  /**
   * T:8401 `annNotesDiscard`'s list half — THIS round's unsent notes go, and
   * only this round's: an earlier round's are not this round's to throw, and a
   * sent one is already Claude's. Returns whether anything went.
   */
  discardRound(): boolean;
  /** T:10608 `annResolveSent` — a run that carried notes finished cleanly, so
   *  everything stamped `sent` is handled: drop it. */
  resolveSent(): boolean;
  /** T:16053 — the badge letters, stamped at SEND time off each note's index in
   *  the whole list (not in `pending`), so the letter on the overview is the
   *  letter on the chip. Mutates and saves; returns the stamped notes. */
  stampLabels(pending: readonly Annotation[]): Annotation[];
  /** T:16065 — the send took them. */
  markSent(pending: readonly Annotation[]): void;
  /** T:16086 — and the send did not land: give them back, re-APPENDING any that
   *  `resolveSent` has dropped in the meantime rather than flipping a flag on a
   *  snapshot nothing holds. */
  unmarkSent(pending: readonly Annotation[]): void;

  /** T:10256 `annApplyOverview`'s write half: replace the notes whose ids these
   *  carry, keeping list order. One save, one notification, whatever the count —
   *  the overview folds into a whole message, not note by note. */
  merge(notes: readonly Annotation[]): void;

  /** T:6680 `annResolve` — the note's element in a document, or null. */
  resolve(c: Annotation, doc: Document | null): Element | null;

  /** T:7761 `annModeWant`/`annModeSync`: "1" armed, "2" recording, "0" off — and
   *  the write is SKIPPED when the URL already MEANS this, because a semantic
   *  no-op still costs a history entry (T:7639, RH/PR). */
  syncModeParam(want: "0" | "1" | "2"): void;
  /** T:7743 `annBootMode`'s reading. */
  modeParam(): string | undefined;
}

export function createAnnStore(opts: AnnStoreOptions): AnnStore {
  const { params } = opts;
  const now = opts.now ?? (() => Date.now());
  const newId =
    opts.newId ??
    (() =>
      typeof crypto !== "undefined" && crypto.randomUUID
        ? crypto.randomUUID()
        : "ann-" + now().toString(36) + "-" + Math.random().toString(36).slice(2, 8));

  let list: Annotation[] = opts.hosted ? [] : parseAnnotations(params.get(ANN_PARAM));
  let round = 0;
  const subs = new Set<(list: readonly Annotation[]) => void>();

  // ONE write path, so there is no way to change the list without the param and
  // the subscribers hearing about it — T's `annSave` is `params.set` plus
  // `renderAnn`, and every mutation in T goes through it.
  const save = () => {
    params.set({ [ANN_PARAM]: serializeAnnotations(list) });
    for (const cb of subs) cb(list);
  };
  // A replacement, not an in-place splice: the snapshot identity has to change
  // exactly when the content does.
  const replace = (next: Annotation[]) => {
    list = next;
    save();
  };

  return {
    list: () => list,
    pending: () => list.filter((c) => !c.sent),
    subscribe(cb) {
      subs.add(cb);
      return () => {
        subs.delete(cb);
      };
    },

    roundStart: () => round,
    startRound(at) {
      round = at ?? now();
    },

    add(note) {
      const saved: Annotation = {
        ...(note as Annotation),
        id: note.id ?? newId(),
        createdAt: note.createdAt ?? now(),
      };
      replace(list.concat([saved]));
      return saved;
    },
    edit(id, content) {
      const c = list.find((a) => a.id === id);
      if (!c || c.sent) return;
      replace(list.map((a) => (a.id === id ? { ...a, content } : a)));
    },
    remove(id) {
      const next = list.filter((a) => a.id !== id);
      if (next.length !== list.length) replace(next);
    },
    removeMany(ids) {
      const drop = new Set(ids);
      if (!drop.size) return;
      const next = list.filter((a) => !drop.has(a.id));
      if (next.length !== list.length) replace(next);
    },
    clear() {
      // THE PARAM IS LEFT ALONE (T:5767, `enterNoPane` step 1a): this drops the
      // LIST, it does not rewrite the URL. A folder that still carries an old
      // bookmark's `annotations` keeps it — the same forgiving posture the
      // stale `annmode`, `paneview` and `split` get there, and the reason the
      // bookmark still round-trips the day that folder becomes an app and gets
      // its pane back. So this is the one write that notifies WITHOUT saving.
      if (!list.length) return;
      list = [];
      for (const cb of subs) cb(list);
    },

    discardRound() {
      const keep = list.filter((a) => a.sent || (a.createdAt || 0) < round);
      if (keep.length === list.length) return false;
      replace(keep);
      return true;
    },
    resolveSent() {
      if (!list.some((c) => c.sent)) return false;
      replace(list.filter((c) => !c.sent));
      return true;
    },
    stampLabels(pending) {
      const stamped: Annotation[] = [];
      const next = list.slice();
      pending.forEach((p) => {
        const i = next.findIndex((a) => a.id === p.id);
        if (i === -1) return;
        const c = { ...next[i], label: labelFor(i) };
        next[i] = c;
        stamped.push(c);
      });
      if (stamped.length) replace(next);
      return stamped;
    },
    markSent(pending) {
      const ids = new Set(pending.map((p) => p.id));
      if (!ids.size) return;
      replace(list.map((a) => (ids.has(a.id) ? { ...a, sent: 1 as const } : a)));
    },
    unmarkSent(pending) {
      if (!pending.length) return;
      const byId = new Map(list.map((a) => [a.id, a]));
      const next = list.map((a) =>
        byId.has(a.id) && pending.some((p) => p.id === a.id) ? { ...a, sent: 0 as const } : a,
      );
      for (const p of pending) {
        if (!byId.has(p.id)) next.push({ ...p, sent: 0 });
      }
      replace(next);
    },

    merge(notes) {
      if (!notes.length) return;
      const by = new Map(notes.map((n) => [n.id, n]));
      let touched = false;
      const next = list.map((a) => {
        const n = by.get(a.id);
        if (!n || n === a) return a;
        touched = true;
        return n;
      });
      if (touched) replace(next);
    },

    resolve: (c, doc) => resolveIn(c, doc),

    syncModeParam(want) {
      const cur = params.get(ANN_MODE_PARAM);
      const curOn = cur === "1" || cur === "2";
      const wantOn = want === "1" || want === "2";
      if (curOn !== wantOn || (wantOn && cur !== want)) {
        params.set({ [ANN_MODE_PARAM]: want });
      }
    },
    modeParam: () => params.get(ANN_MODE_PARAM),
  };
}
