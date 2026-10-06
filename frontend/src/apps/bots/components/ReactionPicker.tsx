// Emoji reactions (OpenBot chat.js openReactions): a small picker anchored to the button, above it when there is room,
// else below. One reaction per message; clicking the current one clears it. Optimistic (the bot's `reactions` in the
// snapshot is replaced at once), the face plays the reaction, then the poll after act() confirms.
import { useEffect, useLayoutEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { api } from "../lib/api";
import { portalHost } from "../lib/root";
import { act, botById, getState, setState, useBotsSelector } from "../state/store";
import { reactFace } from "./faceAnim";

export const REACTIONS = ["👍", "👎", "❤️", "😂", "🎉", "😮"];

export interface ReactionReq { seq: number; anchor: Element }
export interface ReactionPickerProps {
  /** The message and the button the picker anchors to; null = closed. */
  req: ReactionReq | null;
  onClose: () => void;
}

/** Set (or clear, with "") one message's reaction: optimistic snapshot, face, then the route. */
export async function setReaction(id: string, seq: number, emoji: string): Promise<void> {
  const b = botById(id); if (!b) return;
  const reactions = { ...(b.reactions || {}) };
  if (emoji) reactions[seq] = emoji; else delete reactions[seq];
  setState({ bots: getState().bots.map((x) => (x.id === id ? { ...x, reactions } : x)) });
  if (emoji) void reactFace(emoji);
  await act(() => api.react(id, seq, emoji));
}

export function ReactionPicker({ req, onClose }: ReactionPickerProps) {
  const ref = useRef<HTMLDivElement>(null);
  const sel = useBotsSelector((s) => s.sel);
  const now = useBotsSelector((s) => (req ? s.bots.find((b) => b.id === s.sel)?.reactions?.[req.seq] || "" : ""));
  useLayoutEffect(() => {
    const el = ref.current; if (!el || !req) return;
    const r = req.anchor.getBoundingClientRect(), w = el.offsetWidth, h = el.offsetHeight;
    el.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.left + r.width / 2 - w / 2)) + "px";
    el.style.top = (r.top - h - 8 > 8 ? r.top - h - 8 : r.bottom + 8) + "px";
  }, [req]);
  useEffect(() => {
    if (!req) return;
    const onClick = (e: MouseEvent) => { if (!(e.target as Element).closest?.(".rxpick, [data-react]")) onClose(); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("click", onClick); document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("click", onClick); document.removeEventListener("keydown", onKey); };
  }, [req, onClose]);
  return createPortal(
    <div ref={ref} className={`rxpick${req ? " show" : ""}`}>
      {req ? REACTIONS.map((x) => (
        <button key={x} className={x === now ? "on" : ""} data-emoji={x} onClick={() => {
          onClose();
          if (sel) void setReaction(sel, req.seq, x === now ? "" : x);
        }}>{x}</button>
      )) : null}
    </div>,
    portalHost(),  // under <body> but inside the `.bots-page` scope (lib/root.ts)
  );
}
