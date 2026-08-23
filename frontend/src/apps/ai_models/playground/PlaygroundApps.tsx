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
import { useEffect, useState } from "react";
import { matchPlaygroundApps, type ShowcaseAppMeta } from "./appMatch";
import { AppPreviewCard } from "@platform/ui/AppPreviewCard";
import { runCommunity, SHOWCASE_TAG, touchCommunityApp } from "@platform/lib/community";
import { urlForFsPath } from "@platform/lib/router";
import type { AppInfo } from "@platform/lib/api";

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

  return (
    <section className="pg-apps" aria-label="Apps that can use this model">
      <h4 className="pg-apps-head">Use it in an app</h4>
      <div className="pg-apps-row">
        {offers.map(({ app, recommended }) => (
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
    </section>
  );
}
