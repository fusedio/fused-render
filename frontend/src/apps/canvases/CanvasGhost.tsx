// The canvas ghost's element (rules and reasons: canvas-ghost.ts).
import { useEffect, useRef, useState } from "react";
import { GHOST_FADE_MS, GHOST_HOLD_MS, ghostPhase, ghostWord } from "./canvas-ghost";

/** The canvas.toml view over the workbench frame until the workbench is up.
 *
 *  `src` is `ghostSrc(...)`, `file` the canvas.toml it draws, `up` whether the
 *  workbench frame has said hello. Mount it with a `key` that changes when
 *  the workbench frame goes down again (a reload), so each load gets a ghost
 *  of the file as it is then. Fills its positioned parent; takes no pointer. */
export function CanvasGhost({ src, file, up }: { src: string; file: string; up: boolean }) {
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState(false);
  const [wasShown, setWasShown] = useState(false);
  // When the workbench came up, and a clock that ticks only while leaving.
  const upAtRef = useRef<number | null>(null);
  const [now, setNow] = useState(() => performance.now());

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      // The ghost is this app's own page in this frame: nothing else may
      // speak for it.
      if (event.origin !== window.location.origin) return;
      if (event.source !== frameRef.current?.contentWindow) return;
      const word = ghostWord(event.data, file);
      if (word === "ready") setReady(true);
      else if (word === "failed") setFailed(true);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [file]);

  if (up && upAtRef.current === null) upAtRef.current = performance.now();
  const sinceUpMs = up && upAtRef.current !== null ? Math.max(0, now - upAtRef.current) : 0;
  const phase = ghostPhase({ ready, failed, up, wasShown, sinceUpMs });

  useEffect(() => {
    if (phase === "shown" && !wasShown) setWasShown(true);
  }, [phase, wasShown]);

  // The hold and the fade are two moments in time: wake at each.
  useEffect(() => {
    if (!up || phase === "gone") return;
    const at = upAtRef.current ?? performance.now();
    const next = phase === "leaving" ? at + GHOST_HOLD_MS + GHOST_FADE_MS : at + GHOST_HOLD_MS;
    const timer = window.setTimeout(() => setNow(performance.now()), Math.max(0, next - performance.now()) + 1);
    return () => window.clearTimeout(timer);
  }, [up, phase, now]);

  if (phase === "gone") return null;
  return (
    <iframe
      ref={frameRef}
      src={src}
      title="Canvas layout while the workbench loads"
      aria-hidden="true"
      tabIndex={-1}
      data-canvas-ghost={phase}
      style={{
        position: "absolute",
        inset: 0,
        width: "100%",
        height: "100%",
        border: 0,
        // Over the workbench frame, under the screenshot scrim. Never takes
        // the pointer: what is under it is what the user is waiting for.
        zIndex: 1,
        pointerEvents: "none",
        opacity: phase === "shown" ? 1 : 0,
        transition: phase === "leaving" ? `opacity ${GHOST_FADE_MS}ms ease-out` : "none",
      }}
    />
  );
}
