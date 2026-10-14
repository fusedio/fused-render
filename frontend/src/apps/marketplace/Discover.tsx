// /marketplace — the browse page. Two modes off the same catalog:
//
//   * HOME (All, no search): the featured carousel, then a shelf per category
//     (six apps, "See all" → that category's grid).
//   * FILTERED (`?category=<id>` and/or `?q=`): one flat grid of matches, or
//     the empty state with "Clear search".
//
// Chips and search filter IN PLACE: the URL is kept current with
// replaceState (a typed query) / pushState (a chip) — no navigation, so
// nothing remounts and the field keeps focus — and Back out of a chip lands
// on the previous filter, which App.tsx remounts from the URL.
import { useMemo } from "react";
import { RefreshCw, SearchX } from "lucide-react";
import { CardSkeleton, Delayed } from "@platform/ui/appstore";
import { refreshCatalog, type AppEntry, type Catalog, type CatalogSnapshot } from "@platform/lib/marketplace";
import { HeroCarousel } from "./HeroCarousel";
import { CategoryGlyph, EmptyState, MarketCard, Notice, ago } from "./shared";

const SHELF_MAX = 6;

/** Case-insensitive match over the fields a reader would search by. Every
 *  word must hit somewhere ("local chat" finds Local Chat, not every app
 *  that says "local"). */
export function matches(app: AppEntry, q: string): boolean {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const hay = [app.name, app.description, app.author?.name ?? "", app.category, app.slug, ...app.tags]
    .join(" ")
    .toLowerCase();
  return words.every((w) => hay.includes(w));
}

/** Name hits first, then the rest — in catalog order within each. */
function rank(apps: AppEntry[], q: string): AppEntry[] {
  const needle = q.trim().toLowerCase();
  if (!needle) return apps;
  const named = apps.filter((a) => a.name.toLowerCase().includes(needle));
  return named.concat(apps.filter((a) => !named.includes(a)));
}

export function Discover({
  snap,
  category,
  q,
  onCategory,
  onClearSearch,
}: {
  snap: CatalogSnapshot;
  category: string;
  q: string;
  onCategory: (id: string) => void;
  onClearSearch: () => void;
}) {
  const { catalog, error, loading } = snap;
  const apps = catalog?.apps ?? [];
  const filtered = useMemo(() => {
    const byCat = category === "all" ? apps : apps.filter((a) => a.category === category);
    return rank(byCat.filter((a) => matches(a, q)), q);
  }, [apps, category, q]);
  const featured = useMemo(() => apps.filter((a) => a.featured), [apps]);
  const home = category === "all" && !q.trim();

  return (
    <div className="mp-discover">
      <CatalogNotices snap={snap} />
      <Chips catalog={catalog} total={apps.length} value={category} onChange={onCategory} />

      {!catalog ? (
        error ? (
          <EmptyState
            title="Couldn't load the Marketplace"
            body={error}
            action={
              <button type="button" className="mp-btn" onClick={() => void refreshCatalog({ force: true })} disabled={loading}>
                <RefreshCw size={14} strokeWidth={2.2} aria-hidden="true" /> Try again
              </button>
            }
          />
        ) : (
          <Delayed>
            <div className="mp-hero is-skeleton as-skel" aria-hidden="true" />
            <div className="mp-shelf-head">
              <span className="as-skel" style={{ width: 120, height: 16, borderRadius: 5 }} />
            </div>
            <div className="mp-grid">
              {Array.from({ length: 6 }, (_, k) => (
                <CardSkeleton key={k} />
              ))}
            </div>
          </Delayed>
        )
      ) : home ? (
        <>
          {featured.length > 0 && <HeroCarousel apps={featured} catalog={catalog} />}
          {catalog.categories
            .filter((c) => c.count > 0)
            .map((c) => {
              const inCat = apps.filter((a) => a.category === c.id);
              if (!inCat.length) return null;
              // Whole rows only: a 4-app shelf showing 3 + 1 orphan reads as
              // a layout bug, so a shelf shows the largest multiple of the
              // three columns (up to SHELF_MAX) and "See all" carries the rest.
              const shown = inCat.length >= 3 ? Math.min(SHELF_MAX, Math.floor(inCat.length / 3) * 3) : inCat.length;
              return (
                <section className="mp-shelf" key={c.id} aria-labelledby={`shelf-${c.id}`}>
                  <div className="mp-shelf-head">
                    <h2 className="mp-shelf-title" id={`shelf-${c.id}`}>
                      {c.label}
                    </h2>
                    <button type="button" className="mp-see-all" onClick={() => onCategory(c.id)}>
                      {inCat.length > shown ? `See all ${inCat.length}` : "See all"}
                    </button>
                  </div>
                  <div className="mp-grid">
                    {inCat.slice(0, shown).map((a) => (
                      <MarketCard key={a.slug} app={a} catalog={catalog} />
                    ))}
                  </div>
                </section>
              );
            })}
        </>
      ) : (
        <section className="mp-results" aria-live="polite">
          <div className="mp-shelf-head">
            <h2 className="mp-shelf-title">
              {q.trim() ? (
                <>
                  Results for <span className="mp-q">“{q.trim()}”</span>
                  {category !== "all" && <span className="mp-results-in"> in {labelOf(catalog, category)}</span>}
                </>
              ) : (
                labelOf(catalog, category)
              )}
            </h2>
            <span className="mp-results-count">
              {filtered.length} {filtered.length === 1 ? "app" : "apps"}
            </span>
          </div>
          {filtered.length ? (
            <div className="mp-grid">
              {filtered.map((a) => (
                <MarketCard key={a.slug} app={a} catalog={catalog} />
              ))}
            </div>
          ) : (
            <EmptyState
              icon={<SearchX size={22} strokeWidth={1.8} />}
              title={q.trim() ? <>No apps match “{q.trim()}”</> : "Nothing here yet"}
              body={
                q.trim()
                  ? category !== "all"
                    ? `Nothing in ${labelOf(catalog, category)} matches. Try All categories, or a shorter word.`
                    : "Try a shorter word, or browse by category."
                  : "No apps in this category yet."
              }
              action={
                q.trim() ? (
                  <button type="button" className="mp-btn" onClick={onClearSearch}>
                    Clear search
                  </button>
                ) : (
                  <button type="button" className="mp-btn" onClick={() => onCategory("all")}>
                    Browse all apps
                  </button>
                )
              }
            />
          )}
        </section>
      )}
    </div>
  );
}

function labelOf(catalog: Catalog | null, id: string): string {
  return catalog?.categories.find((c) => c.id === id)?.label ?? id;
}

function Chips({ catalog, total, value, onChange }: { catalog: Catalog | null; total: number; value: string; onChange: (id: string) => void }) {
  const cats = catalog?.categories.filter((c) => c.count > 0) ?? [];
  const all = [{ id: "all", label: "All", count: total }, ...cats];
  return (
    <div className="mp-chips" role="toolbar" aria-label="Categories">
      {all.map((c) => (
        <button
          key={c.id}
          type="button"
          className={"mp-chip" + (value === c.id ? " is-active" : "")}
          aria-pressed={value === c.id}
          onClick={() => onChange(c.id)}
          disabled={!catalog}
        >
          <CategoryGlyph id={c.id} size={13} />
          <span>{c.label}</span>
          {catalog && <span className="mp-chip-count">{c.count}</span>}
        </button>
      ))}
    </div>
  );
}

/** Offline / fallback banners: a stale cache served because GitHub was
 *  unreachable, or a server warning (rate limit → git mirror). */
export function CatalogNotices({ snap }: { snap: CatalogSnapshot }) {
  const { catalog, error, loading } = snap;
  if (!catalog) return null;
  const retry = (
    <button type="button" className="mp-notice-btn" onClick={() => void refreshCatalog({ force: true })} disabled={loading}>
      <RefreshCw size={13} strokeWidth={2.2} aria-hidden="true" className={loading ? "is-spinning" : ""} />
      {loading ? "Retrying…" : "Retry"}
    </button>
  );
  if (catalog.stale) {
    return (
      <Notice tone="warn" action={retry}>
        You're offline or GitHub is unreachable. Showing apps cached {ago(catalog.fetched_at)}.
      </Notice>
    );
  }
  if (error) {
    return (
      <Notice tone="warn" action={retry}>
        Couldn't refresh the catalog: {error}
      </Notice>
    );
  }
  // A source `warning` with FRESH data (GitHub's API refused, the git mirror
  // answered) is not the reader's problem: the apps are current. It is said
  // quietly in the page footer (CatalogFooter), never as a banner.
  return null;
}

/** The quiet last line of the page: where the catalog came from and when. */
export function CatalogFooter({ catalog }: { catalog: Catalog | null }) {
  if (!catalog || !catalog.repo) return null;
  return (
    <footer className="mp-footer" title={catalog.warning ?? undefined}>
      Apps from{" "}
      <a href={`https://github.com/${catalog.repo}`} target="_blank" rel="noopener noreferrer">
        {catalog.repo}
      </a>
      {catalog.commit && <> @ {catalog.commit.slice(0, 7)}</>}
      {catalog.source === "git-mirror" && <> · via git mirror</>} · updated {ago(catalog.fetched_at)}
    </footer>
  );
}
