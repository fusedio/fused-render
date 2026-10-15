// /marketplace — the browse page. Two modes off the same catalog:
//
//   * HOME (All, no search): the featured carousel, then one section per
//     server collection (up to two whole rows of the grid as it is laid out
//     right now — 2, 3 or 4 columns; a category shelf's "See all" → that
//     category's grid). No collections → a shelf per category.
//   * FILTERED (`?category=<id>` and/or `?q=`): one flat grid of matches, or
//     the empty state with "Clear search". A typed query is answered by the
//     server search (the local filter takes over silently if it fails); a
//     chip alone filters the cached catalog locally.
//
// Chips and search filter IN PLACE: the URL is kept current with
// replaceState (a typed query) / pushState (a chip) — no navigation, so
// nothing remounts and the field keeps focus — and Back out of a chip lands
// on the previous filter, which App.tsx remounts from the URL.
import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { RefreshCw, SearchX } from "lucide-react";
import { CardSkeleton, Delayed } from "@platform/ui/appstore";
import {
  refreshCatalog,
  useMarketplaceSearch,
  type AppEntry,
  type Catalog,
  type CatalogSnapshot,
} from "@platform/lib/marketplace";
import { HeroCarousel } from "./HeroCarousel";
import { CategoryGlyph, EmptyState, MarketCard, Notice, ago } from "./shared";

/** A shelf holds at most this many rows of the grid. */
const SHELF_ROWS = 2;

/** How many columns `.mp-grid` resolves to inside `root` right now — the
 *  CSS (container queries on the content width) owns the breakpoints, so
 *  this reads the result rather than repeating them. */
function useGridColumns(root: RefObject<HTMLElement>, fallback = 3): number {
  const [cols, setCols] = useState(fallback);
  useEffect(() => {
    const el = root.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const measure = () => {
      const grid = el.querySelector<HTMLElement>(".mp-grid");
      if (!grid) return;
      const n = getComputedStyle(grid).gridTemplateColumns.split(" ").filter(Boolean).length;
      if (n > 0) setCols(n);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [root]);
  return cols;
}

/** Whole rows only: a 4-app shelf on a 3-column grid showing 3 + 1 orphan
 *  reads as a layout bug, so a shelf shows the largest whole number of rows
 *  (up to SHELF_ROWS) and "See all" carries the rest. Fewer apps than one
 *  row are all shown. */
export function shelfCount(available: number, cols: number): number {
  if (cols < 1 || available <= cols) return available;
  return Math.min(SHELF_ROWS * cols, Math.floor(available / cols) * cols);
}

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

export interface HomeSection {
  id: string;
  title: string;
  /** The category "See all" opens; null for a collection with no category. */
  category: string | null;
  /** How many apps "See all" leads to: the category's own count for a category
   *  shelf (the collection lists only a few slugs), else the apps shown. */
  count: number;
  apps: AppEntry[];
}

/** The home page's sections, in order: the server's collections (slugs mapped
 *  through the catalog, unknown or empty ones dropped; a shelf whose id is a
 *  known category links to it, any other shelf has no See all), then the
 *  per-category shelf of every non-empty category no shelf collection covers. */
export function homeSections(catalog: Catalog, apps: AppEntry[]): HomeSection[] {
  const bySlug = new Map(apps.map((a) => [a.slug, a]));
  const cats = new Map(catalog.categories.map((c) => [c.id, c]));
  const fromCollections: HomeSection[] = (catalog.collections ?? [])
    .map((c) => {
      const cat = c.kind === "shelf" ? cats.get(c.id) : undefined;
      const shelf = c.slugs.map((s) => bySlug.get(s)).filter((a): a is AppEntry => !!a);
      return { id: c.id, title: c.title, category: cat ? cat.id : null, count: cat ? cat.count : shelf.length, apps: shelf };
    })
    .filter((s) => s.apps.length > 0);
  const covered = new Set(fromCollections.filter((s) => s.category).map((s) => s.id));
  const rest: HomeSection[] = catalog.categories
    .filter((c) => c.count > 0 && !covered.has(c.id))
    .map((c) => ({ id: c.id, title: c.label, category: c.id, count: c.count, apps: apps.filter((a) => a.category === c.id) }))
    .filter((s) => s.apps.length > 0);
  return fromCollections.concat(rest);
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
  const local = useMemo(() => {
    const byCat = category === "all" ? apps : apps.filter((a) => a.category === category);
    return rank(byCat.filter((a) => matches(a, q)), q);
  }, [apps, category, q]);
  const search = useMarketplaceSearch(q, category);
  // Server results once they have answered; while loading the previous server
  // results stay (or the local filter before the first answer); an error is a
  // silent fall back to the local filter.
  const useServer = !!q.trim() && (search.status === "ok" || (search.status === "loading" && search.apps.length > 0));
  const filtered: AppEntry[] = useServer ? search.apps : local;
  const sections = useMemo(() => (catalog ? homeSections(catalog, apps) : []), [catalog, apps]);
  const featured = useMemo(() => apps.filter((a) => a.featured), [apps]);
  const home = category === "all" && !q.trim();
  const rootRef = useRef<HTMLDivElement>(null);
  const cols = useGridColumns(rootRef);

  return (
    <div className="mp-discover" ref={rootRef}>
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
          {sections.map((sec) => {
            const shown = shelfCount(sec.apps.length, cols);
            return (
              <section className="mp-shelf" key={sec.id} aria-labelledby={`shelf-${sec.id}`}>
                <div className="mp-shelf-head">
                  <h2 className="mp-shelf-title" id={`shelf-${sec.id}`}>
                    {sec.title}
                  </h2>
                  {sec.category && (
                    <button type="button" className="mp-see-all" onClick={() => onCategory(sec.category!)}>
                      {sec.count > shown ? `See all ${sec.count}` : "See all"}
                    </button>
                  )}
                </div>
                <div className="mp-grid">
                  {sec.apps.slice(0, shown).map((a) => (
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
              {filtered.length}
              {useServer && search.nextCursor ? "+" : ""} {filtered.length === 1 ? "app" : "apps"}
            </span>
          </div>
          {filtered.length ? (
            <div className="mp-grid">
              {filtered.map((a) => (
                <MarketCard key={a.slug} app={a} catalog={catalog} />
              ))}
            </div>
          ) : search.status === "loading" ? null : (
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
          {useServer && search.nextCursor && (
            <div className="mp-load-more">
              <button type="button" className="mp-btn" onClick={search.loadMore} disabled={search.loadingMore}>
                Load more
              </button>
            </div>
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

/** Offline / fallback banners: a stale cache served because the Marketplace
 *  was unreachable, or a refresh error. */
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
        You're offline or the Marketplace is unreachable. Showing apps cached {ago(catalog.fetched_at)}.
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
  return null;
}

/** The quiet last line of the page: where the catalog came from and when. */
export function CatalogFooter({ catalog }: { catalog: Catalog | null }) {
  if (!catalog || catalog.source !== "udf-api") return null;
  const version = String(catalog.catalog_version ?? "").slice(0, 8);
  return (
    <footer className="mp-footer" title={catalog.warning ?? undefined}>
      Apps from{" "}
      <a href="https://udf.ai" target="_blank" rel="noopener noreferrer">
        udf.ai
      </a>
      {version && <> · catalog {version}</>} · updated {ago(catalog.fetched_at)}
    </footer>
  );
}
