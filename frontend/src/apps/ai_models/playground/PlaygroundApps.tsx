// The strip under the stage: showcase apps that can USE the selected model.
// The playground proves a model works in one call; these cards are where that
// call already lives inside something finished — click one and the app opens
// with this model in its own URL param (appMatch.ts is the matching rule and
// the metadata contract).
//
// The cards are the SAME AppPreviewCard /apps and /home draw — authored
// preview.png (a required file in the showcase repo), live-iframe hover, the
// whole fallback chain — fed a synthesized AppInfo for the clone's copy of
// each app. The one difference is the destination: the card's `href` override
// carries the model handoff (`?<ai_model_param>=<model id>`), which hrefFor
// deliberately never does.
//
// Its own file, deliberately: PlaygroundTab is under concurrent redesign
// (#760) and this section must cost it two lines, not a rebase.
//
// The catalog read is `action: "catalog"` ONLY — never `refresh`. The hub's
// Showcase tab owns escalation-on-no-cache (Apps.tsx useShowcaseSync); a tab
// for trying models must not be the thing that clones a git repo. No cache,
// error, or nothing matching → the section renders nothing at all: an empty
// heading would advertise a feature this machine cannot show.
import { useCallback, useEffect, useRef, useState } from "react";
import { matchPlaygroundApps, type ShowcaseAppMeta } from "./appMatch";
import { AppPreviewCard } from "@platform/ui/AppPreviewCard";
import { runCommunity, SHOWCASE_TAG, touchCommunityApp } from "@platform/lib/community";
import { urlForFsPath } from "@platform/lib/router";
import type { AppInfo } from "@platform/lib/api";

// Card width + gap must match .pg-apps-row in ai-playground.css.
const CARD_W = 300;
const CARD_GAP = 18;
// The fold: two full rows, whatever a full row happens to be at this width.
const ROWS_BEFORE_FOLD = 2;

// How many cards fit across the strip right now — the same measurement /home
// makes for its one-row sections (shell/Home.tsx useStripCount), minus that
// hook's `limit` half: Home sizes a FETCH by its count, while the showcase
// catalog arrives whole here and "Show more" is a slice, not a request. The
// numbers are this strip's own (300/18), not Home's (330/16).
//
// A CALLBACK ref, not useRef+useEffect, for the reason Home documents: the
// measured element unmounts and remounts (a different model, a catalog that
// lands late), and a mount-once effect goes on observing the detached node —
// clientWidth 0, one card per row, forever. The callback tears the old
// observer down and measures whatever is actually on screen.
//
// `null` until measured, and the section draws no cards until then: a guessed
// default is a row that has to be redrawn. Nothing flashes for it — a callback
// ref runs in the commit phase, so the count is in before the browser paints.
function useRowCapacity() {
  const [perRow, setPerRow] = useState<number | null>(null);
  const roRef = useRef<ResizeObserver | null>(null);
  const ref = useCallback((el: HTMLDivElement | null) => {
    roRef.current?.disconnect();
    roRef.current = null;
    if (!el) return;
    const measure = () => {
      const fits = Math.max(1, Math.floor((el.clientWidth + CARD_GAP) / (CARD_W + CARD_GAP)));
      // A ResizeObserver fires for every pixel of a window drag; only a
      // changed card count is a reason to re-render.
      setPerRow((prev) => (prev === fits ? prev : fits));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    roRef.current = ro;
  }, []);
  return { ref, perRow };
}

type ShowcaseCatalog = {
  status?: string;
  cache_root?: string;
  apps?: ShowcaseAppMeta[];
};

// The clone's copy of the app, in AppInfo's shape so AppPreviewCard can treat
// it exactly like a workspace app. Every showcase app is required to carry
// index.html and preview.png at its root (the repo's CI contract), so both
// paths are stated rather than probed — a missing/broken png falls down the
// card's own fallback chain anyway.
function showcaseAppInfo(cacheRoot: string, app: ShowcaseAppMeta): AppInfo {
  const dir = `${cacheRoot}/${app.slug}`;
  return {
    name: app.slug,
    title: app.name || app.slug,
    tag: SHOWCASE_TAG,
    path: dir,
    entry: `${dir}/index.html`,
    entry_html: `${dir}/index.html`,
    preview_image: `${dir}/preview.png`,
  };
}

export function PlaygroundApps({ capability, modelId }: { capability: string; modelId: string }) {
  const [catalog, setCatalog] = useState<ShowcaseCatalog | null>(null);
  const { ref: rowRef, perRow } = useRowCapacity();
  // Folded again whenever the model changes: the next model's matches are a
  // different, usually shorter list, and an expansion the reader asked for on
  // one model is not consent to a wall of cards on the next.
  const [expanded, setExpanded] = useState(false);
  useEffect(() => setExpanded(false), [modelId]);
  useEffect(() => {
    let alive = true;
    runCommunity<ShowcaseCatalog>({ action: "catalog" }).then(
      (data) => alive && setCatalog(data),
      () => alive && setCatalog(null),
    );
    return () => {
      alive = false;
    };
  }, []);

  if (!catalog || catalog.status !== "ok" || !catalog.cache_root || !catalog.apps?.length) {
    return null;
  }
  const offers = matchPlaygroundApps(catalog.apps, capability, modelId);
  if (!offers.length) return null;
  const cacheRoot = catalog.cache_root;

  // Pre-measure the row draws its own box (so the ResizeObserver has something
  // to measure) and nothing inside it. Commit-phase only — see useRowCapacity.
  const fold = perRow === null ? 0 : perRow * ROWS_BEFORE_FOLD;
  const shown = expanded ? offers : offers.slice(0, fold);
  // One grid rule covers both halves of the layout the section wants. Fewer
  // cards than a row holds → fewer TRACKS, and `justify-content: center` puts
  // that short row in the middle. More than a row holds → the full track count,
  // so the grid fills left-to-right and a partial last row starts at the left
  // edge under the first card above it, never floating mid-band.
  const columns = Math.max(1, Math.min(shown.length, perRow ?? 1));

  return (
    <section className="pg-apps" aria-label="Apps that can use this model">
      <h4 className="pg-apps-head">Use it in an app</h4>
      <div
        className="pg-apps-row"
        ref={rowRef}
        style={{ gridTemplateColumns: `repeat(${columns}, ${CARD_W}px)` }}
      >
        {shown.map(({ app, recommended }) => (
          <span
            key={app.slug}
            style={{ display: "contents" }}
            // The showcase open-marker — fire-and-forget ordering metadata,
            // the same touch the hub's Open sends. On a capture-phase wrapper
            // because the card's own click handler is spoken for.
            onClickCapture={() => touchCommunityApp(app.slug)}
          >
            <AppPreviewCard
              app={showcaseAppInfo(cacheRoot, app)}
              badge={recommended ? "Recommended for this model" : undefined}
              // The model handoff, in the app's OWN param name — never
              // literally `model`, which the shell's sidebar owns.
              href={urlForFsPath(
                `${cacheRoot}/${app.slug}/index.html`,
                `?${encodeURIComponent(app.ai_model_param!)}=${encodeURIComponent(modelId)}`,
              )}
            />
          </span>
        ))}
      </div>
      {/* Only when the fold actually hides something — and only once measured,
          so it never appears for a beat and then leaves. Reveals ALL of the
          rest in one click: the list is a handful of matches, not a feed. */}
      {perRow !== null && offers.length > fold && !expanded && (
        <button type="button" className="pg-apps-more" onClick={() => setExpanded(true)}>
          Show {offers.length - fold} more
          <svg
            width="12"
            height="12"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="M6 9l6 6 6-6" />
          </svg>
        </button>
      )}
    </section>
  );
}
