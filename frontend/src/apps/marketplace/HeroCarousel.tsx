// The Discover page's featured carousel (All, no search): one app per slide —
// eyebrow, big title, description, Get, author on the left; its screenshot
// bleeding off the right edge over a soft wash tinted from the app's own
// hue. Auto-advances every 7s, pauses while hovered or focused (and when
// the tab is hidden), dots to jump. Slides cross-fade; reduced motion
// collapses that to a cut (reduced-motion.css) and turns auto-advance off
// altogether — a slide that changes on its own is motion too.
import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { AppIcon, tintIndex } from "@platform/ui/appstore";
import { appDetailHref, type AppEntry, type Catalog } from "@platform/lib/marketplace";
import { AppActionPill, metaLine, openDetail } from "./shared";

const ADVANCE_MS = 7000;
const REDUCE_QUERY = "(prefers-reduced-motion: reduce)";

function useReducedMotion(): boolean {
  const [reduce, setReduce] = useState(() => typeof window !== "undefined" && !!window.matchMedia?.(REDUCE_QUERY).matches);
  useEffect(() => {
    const mq = window.matchMedia?.(REDUCE_QUERY);
    if (!mq) return;
    const on = () => setReduce(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return reduce;
}

export function HeroCarousel({ apps, catalog }: { apps: AppEntry[]; catalog: Catalog | null }) {
  const [raw, setI] = useState(0);
  // Hover and keyboard focus pause independently: leaving with the mouse
  // must not resume while focus is still on a slide's pill or link — the next
  // tick would mark that slide inert and drop the reader's focus.
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const paused = hovered || focused;
  const reduce = useReducedMotion();
  const n = apps.length;
  // The featured list can shrink under us (a catalog refresh): never point
  // past its end, even for the one render before the state catches up.
  const i = n ? Math.min(raw, n - 1) : 0;
  useEffect(() => {
    if (raw !== i) setI(i);
  }, [raw, i]);
  const auto = n > 1 && !paused && !reduce;
  useEffect(() => {
    if (!auto) return;
    const id = setInterval(() => {
      if (document.visibilityState === "visible") setI((v) => (v + 1) % n);
    }, ADVANCE_MS);
    return () => clearInterval(id);
  }, [n, auto, i]);
  if (n === 0) return null;
  const go = (k: number) => setI(((k % n) + n) % n);
  return (
    <section
      className="mp-hero"
      aria-roledescription="carousel"
      aria-label="Featured apps"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onFocus={() => setFocused(true)}
      onBlur={(e) => {
        // Focus moving between controls inside the carousel stays paused.
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setFocused(false);
      }}
    >
      {apps.map((app, k) => (
        <article
          key={app.slug}
          className={"mp-hero-slide as-tint-" + tintIndex(app.slug) + (k === i ? " is-current" : "")}
          aria-roledescription="slide"
          aria-label={`${k + 1} of ${n}: ${app.name}`}
          aria-hidden={k !== i}
          // Off-screen slides keep their pill and link out of the Tab order
          // (aria-hidden alone leaves them focusable).
          ref={(el) => {
            if (el) el.inert = k !== i;
          }}
          onClick={(e) => {
            if ((e.target as HTMLElement).closest("button, a")) return;
            openDetail(app.slug);
          }}
        >
          <div className="mp-hero-copy">
            <div className="mp-hero-eyebrow">Featured</div>
            <h2 className="mp-hero-title">
              <a
                href={appDetailHref(app.slug)}
                tabIndex={k === i ? 0 : -1}
                onClick={(e) => {
                  if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
                  e.preventDefault();
                  openDetail(app.slug);
                }}
              >
                {app.name}
              </a>
            </h2>
            <p className="mp-hero-desc">{app.description}</p>
            <div className="mp-hero-foot">
              <AppActionPill app={app} size="md" emphasis="filled" />
              <span className="mp-hero-by">
                <AppIcon src={app.icon_url} name={app.name} seed={app.slug} size={22} />
                <span>{metaLine(app, catalog)}</span>
              </span>
            </div>
          </div>
          <div className="mp-hero-art" aria-hidden="true">
            {app.preview_url ? (
              <img src={app.preview_url} alt="" draggable={false} loading={k === 0 ? "eager" : "lazy"} />
            ) : (
              <AppIcon src={app.icon_url} name={app.name} seed={app.slug} size={120} raised />
            )}
          </div>
        </article>
      ))}
      {n > 1 && (
        <>
          <button type="button" className="mp-hero-nav is-prev" aria-label="Previous" onClick={() => go(i - 1)}>
            <ChevronLeft size={18} strokeWidth={2.2} aria-hidden="true" />
          </button>
          <button type="button" className="mp-hero-nav is-next" aria-label="Next" onClick={() => go(i + 1)}>
            <ChevronRight size={18} strokeWidth={2.2} aria-hidden="true" />
          </button>
          <div className="mp-hero-dots" role="tablist" aria-label="Choose slide">
            {apps.map((app, k) => (
              <button
                key={app.slug}
                type="button"
                role="tab"
                aria-selected={k === i}
                aria-label={app.name}
                className={"mp-hero-dot" + (k === i ? " is-current" : "")}
                onClick={() => go(k)}
              >
                {k === i && auto && <span className="mp-hero-dot-fill" key={i} />}
              </button>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
