// The strip under the stage: showcase apps that can USE the selected model.
// The playground proves a model works in one call; these cards are where that
// call already lives inside something finished — click one and the app opens
// with this model in its own URL param (appMatch.ts is the matching rule and
// the metadata contract).
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
import { runCommunity, touchCommunityApp } from "@platform/lib/community";
import { urlForFsPath } from "@platform/lib/router";

type ShowcaseCatalog = {
  status?: string;
  cache_root?: string;
  apps?: ShowcaseAppMeta[];
};

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

  return (
    <section className="pg-apps" aria-label="Apps that can use this model">
      <h4 className="pg-apps-head">Use it in an app</h4>
      <div className="pg-apps-grid">
        {offers.map(({ app, recommended }) => (
          <a
            key={app.slug}
            className="pg-apps-card"
            // The showcase clone's own copy, opened in place (the same file
            // the hub's Open uses) — with the model in the app's OWN param
            // name, never `model`: the shell's sidebar owns that key.
            href={urlForFsPath(
              `${catalog.cache_root}/${app.slug}/index.html`,
              `?${encodeURIComponent(app.ai_model_param!)}=${encodeURIComponent(modelId)}`,
            )}
            onClick={() => touchCommunityApp(app.slug)}
          >
            <span className="pg-apps-name">
              {app.name || app.slug}
              {recommended && (
                <span
                  className="pg-apps-badge"
                  title="This app names this exact model in its metadata"
                >
                  Recommended
                </span>
              )}
            </span>
            {app.description && <span className="pg-apps-desc">{app.description}</span>}
          </a>
        ))}
      </div>
    </section>
  );
}
