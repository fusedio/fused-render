// Apps hub — lives at "/apps", chrome-free like Home (no sidebar, no
// breadcrumb). Every detected app in the workspace (GET /api/apps) as a tile
// grid, narrowed by a search box (name/title, case-insensitive) and tag
// filter chips derived from the apps themselves. The full list is sorted
// once per fetch (recency, name breaks ties) and filtering only ever hides
// tiles — nothing reorders under interaction.
import { useEffect, useMemo, useState } from "react";
import { getApps } from "../lib/api";
import type { AppInfo } from "../lib/api";
import { navigateUrl } from "../lib/router";
import { ErrorBanner } from "../components/ErrorBanner";
import { AppCard } from "../components/AppCard";
import { NewAppPanel } from "./Home";

type Loaded<T> = { status: "loading" } | { status: "ok"; data: T } | { status: "error"; message: string };

export default function Apps() {
  const [apps, setApps] = useState<Loaded<AppInfo[]>>({ status: "loading" });
  const [query, setQuery] = useState("");
  const [tag, setTag] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    let alive = true;
    getApps().then(
      ({ apps }) =>
        alive &&
        setApps({
          status: "ok",
          // Sorted once at load — see header comment.
          data: apps
            .slice()
            .sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0) || a.name.localeCompare(b.name)),
        }),
      (e: Error) => alive && setApps({ status: "error", message: e.message }),
    );
    return () => {
      alive = false;
    };
  }, []);

  const all = apps.status === "ok" ? apps.data : [];
  const tags = useMemo(() => [...new Set(all.map((a) => a.tag))].sort(), [all]);
  const q = query.trim().toLowerCase();
  const shown = all.filter(
    (a) =>
      (tag === null || a.tag === tag) &&
      (q === "" ||
        a.name.toLowerCase().includes(q) ||
        (a.title ?? "").toLowerCase().includes(q) ||
        a.tag.toLowerCase().includes(q)),
  );

  return (
    <div className="apps-page">
      <div className="apps-inner">
        <header className="apps-head">
          <button type="button" className="apps-back" onClick={() => navigateUrl("/")}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M19 12H5M11 18l-6-6 6-6" />
            </svg>
            Home
          </button>
          <h1 className="apps-title">Apps</h1>
          <p className="apps-sub">
            Every app detected in your workspace — search by name or narrow by tag.
          </p>
        </header>

        <div className="apps-toolbar">
          <div className="apps-search">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <circle cx="11" cy="11" r="7" />
              <path d="M21 21l-4.3-4.3" />
            </svg>
            <input
              type="search"
              placeholder="Search apps…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              autoFocus
            />
          </div>
          <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" aria-hidden="true">
              <path d="M12 5v14M5 12h14" />
            </svg>
            New app
          </button>
          {tags.length > 0 && (
            <div className="apps-tags" role="group" aria-label="Filter by tag">
              <button
                type="button"
                className={"apps-tag-chip" + (tag === null ? " is-active" : "")}
                onClick={() => setTag(null)}
              >
                All
              </button>
              {tags.map((t) => (
                <button
                  key={t}
                  type="button"
                  className={"apps-tag-chip" + (tag === t ? " is-active" : "")}
                  onClick={() => setTag(tag === t ? null : t)}
                >
                  {t}
                </button>
              ))}
            </div>
          )}
        </div>

        {apps.status === "error" && <ErrorBanner>{apps.message}</ErrorBanner>}
        {apps.status === "loading" && <div className="home-loading">Loading…</div>}
        {apps.status === "ok" && (
          <>
            <div className="apps-count">
              {shown.length === all.length
                ? `${all.length} app${all.length === 1 ? "" : "s"}`
                : `${shown.length} of ${all.length} apps`}
            </div>
            {shown.length === 0 ? (
              <div className="home-empty">
                {all.length === 0
                  ? "No apps yet. Hit “New app” above to create one."
                  : "No apps match — clear the search or tag filter."}
              </div>
            ) : (
              <div className="home-apps apps-grid">
                {shown.map((app) => (
                  <AppCard key={app.path} app={app} />
                ))}
              </div>
            )}
          </>
        )}
      </div>

      {creating && <NewAppPanel onClose={() => setCreating(false)} />}
    </div>
  );
}
