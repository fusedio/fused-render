// The face's anime.js routines (OpenBot core.js): one action set per group of face roots (header #hface + the
// selected list row #lface move together; the picker preview #pface is separate), the status-mood loop for the
// selected bot, emoji reactions, pointer-tracking eyes and the picker's mood cycle. Off under reduced motion.
// A module singleton: the roots are found by id at call time, like OpenBot's document.querySelectorAll.
import { useEffect } from "react";
import { animate as animeAnimate, createTimeline, stagger, svg, type AnimationParams, type TargetsParam } from "animejs";
import type { Bot } from "../lib/api";
import { FACE_RX, MOODS, PICK_CYCLE, fkey, moodOf, type FaceAction, type Mood } from "../lib/face";
import { cur, useBotsSelector } from "../state/store";

type Actions = { q: (s: string) => NodeListOf<Element>; pose: (m: Mood, duration?: number) => void } & Record<FaceAction, () => void>;

const rnd = (a: number, b: number) => a + Math.random() * (b - a);
const blend = { composition: "blend" } as const;
const wait = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
// Brand faces have no eyes or tear: animating an empty selection makes anime warn, so those calls become no-ops.
// What they have instead is `.mark` (the glyph group): it breathes where eyes would blink, glances where eyes would
// look, and leans toward the pointer where eyes would follow it. Mark offsets are in glyph units (a 24-unit box
// drawn at scale 2), so 1 unit here is 2 face units.
const animate = (t: NodeListOf<Element>, o: Parameters<typeof animeAnimate>[1]) => (t.length ? animeAnimate(t, o) : null);
let lastMouse = 0;  // when the pointer last moved; the random glance yields to eye tracking while it is fresh

function mk(...roots: string[]): Actions {
  const q = (s: string) => document.querySelectorAll(roots.map((r) => `${r} ${s}`).join(", "));
  return {
    q,
    pose: (m, duration = 0) => {
      const [translateY, rotate, scaleX, scaleY] = m.eye;
      animate(q(".face"), { translateY: m.face[0], rotate: m.face[1], duration, ease: "outBack(1.4)" });
      animate(q(".eyeg"), { translateY, rotate: (_?: unknown, i = 0) => rotate * (i % 2 ? 1 : -1), scaleX, scaleY, duration, ease: "outBack(1.4)" });
    },
    blink: () => {
      animate(q(".eye"), { scaleY: [1, .1, 1], duration: 180 });
      // a brand face breathes instead: the mark swells a touch and the disc with it
      animate(q(".mark"), { scale: [1, 1.09, 1], duration: 720, ease: "inOutSine", ...blend });
      if (!q(".eye").length) animate(q(".body"), { scaleX: [1, 1.025, 1], scaleY: [1, 1.025, 1], duration: 720, ease: "inOutSine", ...blend });
    },
    // Looking around: eyes glance together, drift apart or closer, one grows a touch bigger than the other, and the body leans into it.
    look: () => {
      if (Date.now() - lastMouse < 1500) return;
      const dx = rnd(-6, 6), gap = rnd(-2, 3), big = Math.random() < .5 ? 0 : 1;
      animate(q(".eye"), { translateX: (_?: unknown, i = 0) => dx + (i % 2 ? gap : -gap), skewX: rnd(-14, 14), scale: (_?: unknown, i = 0) => i % 2 === big ? rnd(1.05, 1.3) : rnd(.85, 1), duration: 420, ease: "outQuad", ...blend });
      animate(q(".body"), { scaleX: rnd(.95, 1.06), scaleY: rnd(.95, 1.06), rotate: rnd(-4, 4) + dx * .3, duration: 520, ease: "outQuad", ...blend });
      animate(q(".mark"), { translateX: dx * .35, translateY: rnd(-1.2, 1.2), rotate: rnd(-9, 9), duration: 480, ease: "outQuad", ...blend });
    },
    tear: () => { animate(q(".tear"), { translateY: [0, 24], scaleY: [.6, 1.4], opacity: [0, .95, .95, 0], duration: 1300, ease: "inQuad" }); },
    gasp: () => { animate(q(".face"), { translateY: [0, -12, 0], scaleY: [1, 1.2, .94, 1], scaleX: [1, .88, 1.04, 1], duration: 520, ease: "outQuad", ...blend }); },
    snore: () => { animate(q(".face"), { scaleX: [1, 1.1, 1], scaleY: [1, 1.09, 1], duration: 1700, ease: "inOutSine", ...blend }); },
    bounce: () => { animate(q(".face"), { scaleX: [1, 1.18, .92, 1], scaleY: [1, .82, 1.08, 1], duration: 650, ease: "outElastic(1, .5)", ...blend }); },
    // The builder's excitement spin: eyes dart off, the body squashes and twists, rainbow ribbons draw around it, sparks pop, eyes snap back.
    spin: () => {
      const rib = q(".ribbons path"); if (!rib.length) return;
      for (const g of q(".sparks g")) { const a = rnd(0, 6.28), r = rnd(40, 52); g.setAttribute("transform", `translate(${50 + r * Math.cos(a)} ${50 + r * Math.sin(a)})`); }
      const tl = createTimeline({ defaults: { ease: "inOutSine" } });
      const add = (t: NodeListOf<Element> | SVGGeometryElement[], o: AnimationParams, at: number) => { if (t.length) tl.add(t as TargetsParam, o, at); };
      add(q(".eye"), { translateX: 16, scaleX: 0, duration: 220, ease: "inQuad" }, 0);
      add(q(".face"), { translateY: [0, -14, 0, -4, 0], duration: 1000, ease: "inOutQuad" }, 0);
      add(q(".body"), { scaleX: [1, .72, 1.12, .96, 1], scaleY: [1, 1.12, .84, 1.03, 1], rotate: [0, -12, 6, 0], duration: 1100, ease: "outElastic(1, .6)" }, 0);
      add(q(".ribbons"), { opacity: [1, 1, 0], rotate: [40, -50], duration: 1100, ease: "outCubic" }, 80);
      add(svg.createDrawable(rib as NodeListOf<SVGGeometryElement>), { draw: ["0 0", "0 .55", ".6 1", "1 1"], strokeWidth: [5, 5, 2.5, 1], duration: 900, delay: stagger(70), ease: "inOutQuad" }, 100);
      add(q(".sparks path"), { scale: [0, 1.4, 0], rotate: [0, 180], opacity: [0, 1, 0], duration: 620, delay: stagger(50, { start: 520 }), ease: "outQuad" }, 0);
      add(q(".eye"), { translateX: [-16, 0], scaleX: [0, 1], skewX: 0, duration: 420, ease: "outBack(1.6)" }, 640);
      add(q(".mark"), { rotate: [0, 360], scale: [1, 1.25, 1], duration: 1000, ease: "inOutBack(1.2)" }, 0);
    },
  };
}

let A: Actions | null = null, P: Actions | null = null;
let faceKey = "", faceGen = 0, pickGen = 0;

/** Header face: the status-mood routine runs for the selected bot only; list avatars stay static frames. No-op until the key changes. */
export function setFace(b: Bot | undefined): void {
  if (!b) { faceKey = ""; ++faceGen; return; }
  const key = fkey(b); if (key === faceKey) return; faceKey = key;
  if (!A) return;
  const g = ++faceGen, m = MOODS[moodOf(b)], acts = A;
  acts.pose(m);
  (async () => {
    await wait(900);
    while (g === faceGen) {
      for (const [a, d] of m.loop) { if (g !== faceGen) return; acts[a](); await wait(d); }
      await wait(8000);
    }
  })();
}

/** A reaction plays on the face (👍 blink, 😂 🎉 bounce, ❤️ spin, 😮 gasp, 👎 tear), then it settles back into its status mood. */
export async function reactFace(emoji: string): Promise<void> {
  const [mood, ...acts] = FACE_RX[emoji] || [], b = cur(); if (!A || !mood || !b) return;
  const g = ++faceGen, a0 = A;
  a0.pose(MOODS[mood], 400); await wait(250);
  for (const a of acts) { a0[a](); await wait(a === "tear" || a === "spin" ? 1500 : 650); }
  if (g === faceGen) { faceKey = ""; setFace(cur()); }
}

/** After a pick the picker preview walks happy → sad → surprised → excited → sleepy, then settles back to happy. `g` must equal the current pick generation. */
export async function cycle(g: number): Promise<void> {
  if (!P) return;
  const p = P;
  for (const [k, a] of PICK_CYCLE) {
    if (g !== pickGen) return;
    p.pose(MOODS[k], 450); await wait(300); p[a](); await wait(1300);
  }
  if (g === pickGen) p.pose(MOODS.idle, 450);
}
/** The picker after each pick: OpenBot `cycle(++pickGen)`. */
export const playPickCycle = (): Promise<void> => cycle(++pickGen);
/** The picker closing: OpenBot `++pickGen` (stops a running cycle). */
export const endPickCycle = (): void => { ++pickGen; };

/** Load the routines (unless reduced motion) and the pointer-tracking eyes. Returns the teardown. */
export function initFaceAnim(): () => void {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return () => {};
  A = mk("#hface", "#lface"); P = mk("#pface");
  // The active bot's eyes follow the pointer: aim from the header face toward the cursor, clamped so the pupils stay inside the body,
  // stronger the farther away the cursor is. One frame per pointer event at most; the eyes drift back to centre when the cursor leaves.
  let raf = 0, qk = { gap: 0, big: -1, sz: 1, until: 0 };
  const onMove = (e: MouseEvent) => {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      const h = document.getElementById("hface"), b = cur(); if (!h || !b || !A || moodOf(b) === "paused") return;  // a sleeping face keeps its eyes shut
      const r = h.getBoundingClientRect();
      const dx = e.clientX - (r.left + r.width / 2), dy = e.clientY - (r.top + r.height / 2), d = Math.hypot(dx, dy) || 1, k = Math.min(1, d / 320);
      lastMouse = Date.now();
      // Every couple of seconds of tracking, roll a quirk: half the time none, otherwise the eyes drift apart a little and one grows a touch.
      if (lastMouse > qk.until) qk = Math.random() < .5 ? { gap: 0, big: -1, sz: 1, until: lastMouse + rnd(1500, 3000) } : { gap: rnd(-1.5, 3), big: Math.random() < .5 ? 0 : 1, sz: rnd(1.1, 1.3), until: lastMouse + rnd(1500, 3000) };
      animate(A.q(".eye"), { translateX: (_?: unknown, i = 0) => dx / d * 6 * k + (i % 2 ? qk.gap : -qk.gap), translateY: dy / d * 3 * k, skewX: 0, scale: (_?: unknown, i = 0) => i % 2 === qk.big ? qk.sz : 1, duration: 160, ease: "outQuad", ...blend });
      // the brand mark leans the same way, and tilts a little into it
      animate(A.q(".mark"), { translateX: dx / d * 2.4 * k, translateY: dy / d * 1.8 * k, rotate: dx / d * 7 * k, duration: 160, ease: "outQuad", ...blend });
    });
  };
  const onLeave = () => {
    lastMouse = 0;
    if (!A) return;
    animate(A.q(".eye"), { translateX: 0, translateY: 0, duration: 400, ease: "outQuad", ...blend });
    animate(A.q(".mark"), { translateX: 0, translateY: 0, rotate: 0, duration: 400, ease: "outQuad", ...blend });
  };
  document.addEventListener("mousemove", onMove);
  document.addEventListener("mouseleave", onLeave);
  faceKey = ""; setFace(cur());
  return () => {
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseleave", onLeave);
    cancelAnimationFrame(raf); ++faceGen; ++pickGen; A = P = null; faceKey = "";
  };
}

/** Mount once (App): starts the animator and re-poses the header face whenever the selected bot's face key changes. Runs after the panes commit, so #hface exists. */
export function useFaceAnimator(): void {
  const key = useBotsSelector((s) => { const b = s.bots.find((x) => x.id === s.sel); return b ? fkey(b) : ""; });
  useEffect(() => initFaceAnim(), []);
  useEffect(() => { setFace(cur()); }, [key]);
}
