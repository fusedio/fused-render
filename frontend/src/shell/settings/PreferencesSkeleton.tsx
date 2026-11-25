// The Preferences page while it loads: the page's own frame (title, search
// box, nav column, a head and cards of rows) drawn in shimmer bars, on the
// real layout classes so nothing moves when the answer lands and the nav folds
// above the rows at the same container width the page does.
//
// Its own module with no imports from the page: App.tsx shows it as the
// Suspense fallback while the Preferences chunk downloads, and the page shows
// the same body while it waits for its reads — one loading look, start to end.
// Bars only, never a group's name or count: which groups this machine has is
// part of what is being loaded.
import { useEffect, useState, type CSSProperties } from "react";

// The fade-in is held back (preferences.css: a warm read must not flash a
// skeleton) — but one wait is two mounts, the route's fallback and then the
// page's own, and a second mount would restart the hold and blink. A skeleton
// that mounts right after another left picks the fade up where that one was.
const HOLD_MS = 150;
const SAME_WAIT_MS = 200;
let waitStart = 0;
let lastGone = Number.NEGATIVE_INFINITY;

function useFadeDelay(): CSSProperties {
  const [style] = useState<CSSProperties>(() => {
    const now = performance.now();
    if (now - lastGone > SAME_WAIT_MS) waitStart = now;
    return { animationDelay: `${Math.round(HOLD_MS - (now - waitStart))}ms` };
  });
  useEffect(
    () => () => {
      lastGone = performance.now();
    },
    [],
  );
  return style;
}

// Label widths (px) for the nav's two sections and the rows of two cards —
// ragged on purpose, so it reads as text to come and not as a grid.
const NAV: readonly (readonly number[])[] = [
  [58, 84, 66, 92, 72],
  [78, 60, 88, 70],
];
const CARDS: readonly (readonly (readonly [label: number, desc: number, control: number])[])[] = [
  [
    [120, 260, 150],
    [164, 320, 36],
    [104, 220, 36],
    [142, 290, 110],
  ],
  [
    [132, 240, 36],
    [96, 300, 170],
    [156, 210, 36],
  ],
];

function Bar({ w, h, className }: { w: number | string; h?: number; className?: string }) {
  const style: CSSProperties = { width: w, ...(h ? { height: h } : null) };
  return <span className={className ? `skel-bar ${className}` : "skel-bar"} style={style} />;
}

/** The search box's place in the header, before there are settings to search. */
export function SettingsSearchSkeleton() {
  const fade = useFadeDelay();
  return <div className="settings-search settings-skel-search" style={fade} aria-hidden="true" />;
}

/** The nav and the rows. Carries the loading announcement for the whole page. */
export function SettingsBodySkeleton() {
  const fade = useFadeDelay();
  return (
    <div className="settings-body settings-skel" style={fade} role="status" aria-busy="true" aria-label="Loading preferences">
      <div className="settings-nav" aria-hidden="true">
        {NAV.map((items, s) => (
          <div key={s} className="settings-nav-section">
            <div className="settings-nav-heading">
              <Bar w={s === 0 ? 44 : 62} h={8} />
            </div>
            {items.map((w, i) => (
              <div key={i} className="settings-skel-nav-item">
                <Bar w={16} className="icon-skel" />
                <Bar w={w} />
              </div>
            ))}
          </div>
        ))}
      </div>
      <div className="settings-main" aria-hidden="true">
        <div className="settings-skel-head">
          <Bar w={132} h={16} />
          <Bar w="min(360px, 70%)" />
        </div>
        {CARDS.map((rows, c) => (
          <div key={c} className="settings-section">
            <div className="settings-card">
              {rows.map(([label, desc, control], r) => (
                <div key={r} className="settings-row settings-skel-row">
                  <div className="settings-row-main">
                    <Bar w={label} h={12} />
                    <Bar w={`min(${desc}px, 90%)`} />
                  </div>
                  <div className="settings-row-control">
                    <Bar w={control} className="settings-skel-control" />
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** The whole page, for the route's Suspense fallback. */
export default function PreferencesSkeleton() {
  return (
    <div className="settings-page">
      <div className="settings-header">
        <h1 className="settings-title">Preferences</h1>
        <SettingsSearchSkeleton />
      </div>
      <SettingsBodySkeleton />
    </div>
  );
}
