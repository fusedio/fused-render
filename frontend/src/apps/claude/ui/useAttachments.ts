// THE TRAY: the attachments this message is about to carry, and the four ways
// they get in (camera, paste, drop, real-path drag).
//
// T keeps them in one module-level array (`shotAttached`, T:5995) and calls
// `renderAnn()` after every mutation; here it is state, and the rules that array
// carried come with it:
//
//   * ONE PANE SEAT — a second camera click REPLACES the first picture (D285,
//     T:11309). "The pane, now" has exactly one answer, and two photographs of
//     the same screen in one message is a receipt nobody reads. Pasted pictures
//     are the other kind and stack freely.
//   * NO COUNT CAP (D617) and no size refusal (D615).
//   * CHIPS LAND IN ORDER (T:11625): the upload is sequential, so a drop of six
//     arrives as six chips in the order the user brought them.
//   * A FAILED SEND HANDS THEM BACK, PREPENDED (T:16093, 16708): the user
//     attached them deliberately and may not be able to retake them, and a
//     picture attached WHILE the send was in flight keeps its place after the
//     ones that were already waiting.
import { useCallback, useEffect, useRef, useState } from "react";

import { paneShotBlock } from "../protocol/wire";
import { frameIsCrossOrigin } from "../shots";
import type { Attachment, Receipt } from "../shots/types";
import { ATTACH_API, type AttachApi } from "./attachApi";

export interface UseAttachmentsOptions {
  /** The real pipeline by default; replaced whole in tests (`ATTACH_API`). */
  api?: AttachApi;
  agentDir: string;
  /** The pane's iframe, read at gesture time. */
  frame(): HTMLIFrameElement | null;
  /** What the shutter flashes over — the frame's own box (T:11233 uses the
   *  highlight layer's parent, which is that same box). */
  flashHost(): HTMLElement | null;
  /** "preview" / "app": the word the wire block uses for the pane. */
  paneNoun: string;
  /** The shots dir, when a caller knows it. Left out, the pipeline's own
   *  resolved answer for `agentDir` is used — either way it is FILTERED OUT of
   *  the Read rules, because the spawn line pre-approves it unconditionally and
   *  a duplicate rule would grow on every turn (T:11698).  */
  shotsDir?: string;
}

/** What a send takes out of the tray. */
export interface OutgoingAttachments {
  /** The `<pane-shot>` block, or nothing at all when the tray was empty. */
  blocks: string[];
  /** One Read rule each, for the directories real-path attachments live in. */
  readDirs: string[];
  /** The rows the sent turn wears. */
  receipts: Receipt[];
  /** The attachments themselves, so a send that never landed can hand them
   *  back. */
  items: Attachment[];
}

export interface Attachments {
  items: Attachment[];
  /** A capture is in flight: the camera goes inert (T:11203 `shotBusy`). */
  capturing: boolean;
  /** The camera (T:11258 `shotAttachPane`). */
  capture(): Promise<void>;
  /** ⌘V and a drop of bytes (T:11557 `shotAttachFiles`). */
  addFiles(files: readonly File[]): Promise<void>;
  /** A drag from inside fused-render: the real path, no upload (T:11680). */
  addPaths(paths: readonly string[]): Promise<void>;
  /** The chip's ✕ and the viewer's Discard (T:10654 `shotDrop`). */
  remove(att: Attachment): void;
  /**
   * Empty the tray INTO a send.
   *
   * `lead` rides the same `<pane-shot>` block and the same receipt row the
   * tray's own pictures do, FIRST in the list, and is not part of `items`:
   * PR3's badged overview is the page's own picture of a pane that has since
   * moved on, so a send that never launched must NOT hand it back as a chip the
   * way it hands back what the user attached (T:16549-16553, 16698-16708).
   */
  take(lead?: readonly Attachment[]): OutgoingAttachments;
  /** The send never landed: put them back, prepended. */
  giveBack(items: readonly Attachment[]): void;
}

function errText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export function useAttachments(opts: UseAttachmentsOptions): Attachments {
  const { api = ATTACH_API, agentDir, frame, flashHost, paneNoun, shotsDir } = opts;
  const [items, setItems] = useState<Attachment[]>([]);
  const [capturing, setCapturing] = useState(false);
  /**
   * THE TRAY, AS OF NOW. Read by `take()`, which must see what the tray holds at
   * THAT moment rather than what the render it was created in closed over — and
   * read by the unmount cleanup below, which is the only thing left that can
   * release these Blobs.
   *
   * It is written by `commit` and NOT in the render body, because a render is
   * too late for the one case the unmount revoke exists for: a picture added by
   * `capture`/`addFiles`/`addPaths` whose `setItems` had not painted yet when the
   * parent went away was never in `live.current`, so its Blob was pinned for the
   * life of the page — exactly the leak the cleanup is for. So the ref is the
   * source of truth for the tray's contents and `setItems` is what paints it.
   */
  const live = useRef<Attachment[]>(items);
  /** Every mutation of the tray, in one place, so the ref and the state can
   *  never disagree — computed eagerly off the ref rather than inside React's
   *  updater, whose timing is the whole bug. */
  const commit = useCallback((fn: (prev: readonly Attachment[]) => Attachment[]): void => {
    const next = fn(live.current);
    live.current = next;
    setItems(next);
  }, []);
  const seq = useRef(0);
  /**
   * T:11203 `shotBusy`, and a ref because that is the whole point: a boolean in
   * STATE is only read as of the render that closed over it, so two activations
   * in one tick — a double click, a click racing the keyboard — both passed the
   * guard and two captures ran, the second's seat-swap revoking a thumbnail a
   * chip was already showing. Latched synchronously here; `capturing` stays as
   * the thing the BUTTON reads.
   */
  const busy = useRef(false);
  /** Mounted. Every write below lands after an await (`api` is the network), and
   *  a write into a chat the user has closed is at best a React warning and at
   *  worst a tray that comes back. */
  const alive = useRef(true);
  // `api` through a ref so the unmount cleanup can run ONCE, on the real
  // unmount, rather than on every identity change of an injected pipeline.
  const apiRef = useRef(api);
  apiRef.current = api;

  useEffect(() => {
    // Set on the way IN as well: React re-runs an effect's setup after its
    // cleanup, and StrictMode does it on the first mount, so a flag only ever
    // cleared would leave the tray inert for the rest of the page's life.
    alive.current = true;
    return () => {
      alive.current = false;
      // NOTHING ELSE HOLDS THESE. A chat closed with three pending pictures in
      // the tray pinned three full-pane Blobs for the life of the page: the
      // chips that were the only handles to them went with the unmount, and the
      // send that would have revoked them never happened.
      for (const att of live.current) apiRef.current.revoke(att);
    };
  }, []);

  const capture = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    setCapturing(true);
    // BEFORE the capture, not after: the whole point is that the click is
    // answered immediately, and the capture can take a second on a large page.
    // It is also honest about the moment — the flash marks when the shutter
    // opened (T:11267).
    api.flash(flashHost());
    try {
      // READ AT GESTURE TIME, and handed DOWN: `xo` is the only thing that
      // admits the tab share, and the camera is the only caller that knows which
      // frame is being photographed. Asked for with no options at all, a
      // cross-origin pane the native path could not shoot went to a DOM clone of
      // a document this page cannot open (Bugbot, PR #1064).
      const target = frame();
      const shot = await api.attachPane(agentDir, target, {
        xo: frameIsCrossOrigin(target),
      });
      if (!alive.current) {
        // No chip will ever show it, so this is the last handle to its Blob.
        api.revoke(shot);
        return;
      }
      commit((prev) => {
        const seat = prev.findIndex((s) => s.kind === "pane");
        if (seat === -1) return [...prev, shot];
        const held = prev[seat]!;
        // A REFUSAL NEVER EVICTS A PICTURE (Bugbot, PR #1064). The pane seat is
        // unique, so a second click REPLACES what is in it — and a capture that
        // timed out or failed to encode still comes back as an attachment, with
        // `view: null` and the reason in `viewNote` (`uploadCapture`). Swapping
        // that in threw away a screenshot the reader already had and could still
        // send: one flaky retry, and the evidence was gone with no way back.
        //
        // THE HELD PICTURE IS RETURNED UNTOUCHED, its own `viewNote` included.
        // The refusal's sentence is NOT copied onto it (Bugbot, PR #1064, second
        // pass): `viewNote` says what THIS picture does not show, and it rides
        // the wire (`toWire`) as the caveat under the receipt — so borrowing it
        // for the retry told the agent not to trust a screenshot that is
        // perfectly good, and overwrote whatever the original capture had
        // genuinely caveated. A refusal becoming a chip (T:11305) is the rule
        // for a seat with NOTHING in it, where the chip is the only news there
        // is; here the tray still shows exactly what the send will carry, which
        // is the thing that had to stay true.
        if (!shot.view && held.view) {
          api.revoke(shot); // nothing of its own to hold, but symmetrical
          return prev.slice();
        }
        // The replaced picture's blob URL is the only handle to it.
        api.revoke(held);
        const next = prev.slice();
        next[seat] = shot;
        return next;
      });
    } finally {
      busy.current = false;
      if (alive.current) setCapturing(false);
    }
  }, [api, agentDir, flashHost, frame, commit]);

  /**
   * A PLACEHOLDER PER FILE, replaced as its bytes land. The pipeline yields
   * sequentially and in order, so the n-th attachment belongs to the n-th
   * placeholder — which is what keeps a drop of six from re-ordering itself
   * while the uploads race, and gives the chip a state to show meanwhile (T's
   * chips simply appear one at a time; a chip that says it is on its way is the
   * same news, earlier).
   */
  const addFiles = useCallback(
    async (files: readonly File[]) => {
      if (!files.length) return;
      if (!alive.current) return;
      const ids = files.map(() => "pending:" + ++seq.current);
      commit((prev) => [
        ...prev,
        ...ids.map(
          (id): Attachment => ({ id, kind: "file", view: null, pending: true }),
        ),
      ]);
      let i = 0;
      /** Why the iteration stopped, when it stopped badly: the sentence the
       *  chips left standing get to say. */
      let broke: unknown = null;
      try {
        for await (const att of api.attachFiles(agentDir, [...files])) {
          const id = ids[i++];
          if (!id) break;
          if (!alive.current) {
            // Same reasoning as the camera's: the chip that would have held this
            // one's thumbnail is gone, so this is the last handle to it.
            api.revoke(att);
            break;
          }
          commit((prev) => prev.map((s) => (s.id === id ? att : s)));
        }
      } catch (err) {
        // `attachFile` is written never to throw, and a pipeline that does
        // anyway (a rejected generator, an injected fake) must not be the reason
        // a gesture the user made goes unanswered.
        broke = err;
      } finally {
        // ANYTHING THE PIPELINE NEVER ANSWERED FOR BECOMES A REFUSAL CHIP, never
        // a hole: a placeholder must not sit in the tray claiming a file is
        // still on its way, and removing it outright — what stood here — is the
        // dropped picture vanishing with no answer at all (Bugbot, PR #1064).
        const unspent = ids.slice(i);
        if (unspent.length && alive.current) {
          const why = broke ? " (" + errText(broke) + ")" : "";
          const refused = new Map<string, Attachment>(
            unspent.map((id, n) => [
              id,
              {
                id,
                kind: "file",
                view: null,
                name: files[i + n]?.name || "attached file",
                viewNote: "not attached: it could not be saved" + why,
                why: "could not be saved",
              },
            ]),
          );
          commit((prev) => prev.map((s) => refused.get(s.id) ?? s));
        }
      }
    },
    [api, agentDir, commit],
  );

  const addPaths = useCallback(
    async (paths: readonly string[]) => {
      if (!paths.length) return;
      const added = await api.attachPaths(agentDir, [...paths]);
      if (!alive.current) {
        for (const att of added) api.revoke(att);
        return;
      }
      if (added.length) commit((prev) => [...prev, ...added]);
    },
    [api, agentDir, commit],
  );

  const remove = useCallback(
    (att: Attachment) => {
      api.revoke(att);
      commit((prev) => prev.filter((s) => s !== att && s.id !== att.id));
    },
    [api, commit],
  );

  const take = useCallback(
    (lead: readonly Attachment[] = []): OutgoingAttachments => {
      const mine = live.current.filter((s) => !s.pending);
      // A pending chip is not part of THIS message: its bytes are still on their
      // way, so it stays in the tray for the next one rather than riding as a
      // half-attachment.
      commit((prev) => prev.filter((s) => s.pending));
      // FIRST in the list: the overview is the picture the annotations block
      // tells the model to read (T:16549).
      const sending = [...lead, ...mine];
      if (!sending.length) return { blocks: [], readDirs: [], receipts: [], items: [] };
      const block = paneShotBlock(api.toWire(sending), paneNoun);
      return {
        blocks: block ? [block] : [],
        readDirs: shotsDir
          ? api.readDirs(sending, shotsDir)
          : api.readDirsFor(agentDir, sending),
        receipts: sending.map((s) => api.receiptFor(s)),
        // The tray's own only: `lead` is nobody's to give back.
        items: mine,
      };
    },
    [api, agentDir, paneNoun, shotsDir, commit],
  );

  const giveBack = useCallback(
    (back: readonly Attachment[]) => {
      if (!back.length) return;
      // The chat this send belonged to is gone, and the pictures it was carrying
      // have no tray to come back to.
      if (!alive.current) {
        for (const att of back) apiRef.current.revoke(att);
        return;
      }
      commit((prev) => [...back, ...prev]);
    },
    [commit],
  );

  return { items, capturing, capture, addFiles, addPaths, remove, take, giveBack };
}
