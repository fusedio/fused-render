// /marketplace — the browse page: ONE grid of the catalog, newest release
// first. Chips (`?category=<id>`) and the search field (`?q=`) narrow that
// same grid in place; the featured carousel sits above it on the home view
// (All, no search) when the index marks any app featured. No shelves, no
// per-category rows: a catalog this size (tens of apps) is read fastest as
// one honest grid, and shelves re-showed the same card two or three times
// down the page (33 cards for 21 apps).
//
// Chips and search filter IN PLACE: the URL is kept current with
// replaceState (a typed query) / pushState (a chip) — no navigation, so
// nothing remounts and the field keeps focus — and Back out of a chip lands
// on the previous filter, which App.tsx remounts from the URL.
//
// SORT: Newest release (default) · Most remixed · Most installed, kept in
// `?sort=` (replaceState). The control is HIDDEN until the catalog carries
// a signal to sort by — some app with a remix or an install count — since a
// sort over all-zero counts is three labels for one order. A typed query
// keeps its relevance order (the server's, or name hits first locally).
import { useMemo, useRef, useState } from "react";
import { ArrowDownWideNarrow, RefreshCw, SearchX } from "lucide-react";
import { replaceSearch } from "@platform/lib/router";
import { CardSkeleton, Delayed } from "@platform/ui/appstore";
import {
  refreshCatalog,
  useMarketplaceSearch,
  type AppEntry,
  type Catalog,
  type CatalogSnapshot,
} from "@platform/lib/marketplace";
import { HeroCarousel } from "./HeroCarousel";
import {
  CategoryGlyph,
  EmptyState,
  MarketCard,
  Notice,
  ago,
  authorLabel,
} from "./shared";

/** Case-insensitive match over the fields a reader would search by. Every
 *  word must hit somewhere ("local chat" finds Local Chat, not every app
 *  that says "local"). */
export function matches(app: AppEntry, q: string): boolean {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const hay = [
    app.name,
    app.description,
    authorLabel(app),
    app.author?.name ?? "",
    app.category,
    app.slug,
    ...app.tags,
  ]
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

export type SortKey = "newest" | "remixed" | "installed";

export const SORTS: { key: SortKey; label: string }[] = [
  { key: "newest", label: "Newest release" },
  { key: "remixed", label: "Most remixed" },
  { key: "installed", label: "Most installed" },
];

function releasedAt(app: AppEntry): number {
  const t = Date.parse(
    app.last_release_at ?? app.updated_at ?? app.published_at ?? "",
  );
  return Number.isNaN(t) ? 0 : t;
}

/** Whether any listed app carries a count worth sorting by. */
export function hasSortSignal(apps: AppEntry[]): boolean {
  return apps.some(
    (a) => (a.remix_count ?? 0) > 0 || (a.install_count ?? 0) > 0,
  );
}

/** `apps` in `key`'s order, newest release breaking ties; apps with no
 *  release date sort last. Ties keep their catalog order (a stable sort). */
export function sortApps(apps: AppEntry[], key: SortKey): AppEntry[] {
  const out = apps.slice();
  const byDate = (a: AppEntry, b: AppEntry) => releasedAt(b) - releasedAt(a);
  if (key === "remixed")
    out.sort(
      (a, b) => (b.remix_count ?? 0) - (a.remix_count ?? 0) || byDate(a, b),
    );
  else if (key === "installed")
    out.sort(
      (a, b) => (b.install_count ?? 0) - (a.install_count ?? 0) || byDate(a, b),
    );
  else out.sort(byDate);
  return out;
}

function readSort(): SortKey {
  const v = new URLSearchParams(location.search).get("sort");
  return v === "remixed" || v === "installed" ? v : "newest";
}

/** The URL's `?sort=` follows the control, the rest of the query kept. */
function writeSort(key: SortKey) {
  const sp = new URLSearchParams(location.search);
  if (key === "newest") sp.delete("sort");
  else sp.set("sort", key);
  const s = sp.toString();
  replaceSearch(location.pathname + (s ? "?" + s : ""));
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
  const [chosen, setChosen] = useState<SortKey>(readSort);
  const sortable = useMemo(() => hasSortSignal(apps), [apps]);
  // Without a signal the control is hidden and the order is newest-first,
  // whatever an old URL's `?sort=` says.
  const sort: SortKey = sortable ? chosen : "newest";
  const typed = !!q.trim();
  const local = useMemo(() => {
    const byCat =
      category === "all" ? apps : apps.filter((a) => a.category === category);
    const hits = byCat.filter((a) => matches(a, q));
    return typed ? rank(hits, q) : sortApps(hits, sort);
  }, [apps, category, q, typed, sort]);
  const search = useMarketplaceSearch(q, category);
  // Server results once they have answered; while loading the previous server
  // results stay (or the local filter before the first answer); an error is a
  // silent fall back to the local filter.
  const useServer =
    typed &&
    (search.status === "ok" ||
      (search.status === "loading" && search.apps.length > 0));
  const filtered: AppEntry[] = useServer ? search.apps : local;
  const featured = useMemo(() => apps.filter((a) => a.featured), [apps]);
  const home = category === "all" && !typed;
  const rootRef = useRef<HTMLDivElement>(null);

  return (
    <div className="mp-discover" ref={rootRef}>
      <CatalogNotices snap={snap} />
      <div className="mp-toolbar">
        <Chips
          catalog={catalog}
          total={apps.length}
          value={category}
          onChange={onCategory}
        />
        {sortable && (
          <SortControl
            value={sort}
            disabled={!catalog || typed}
            onChange={(k) => {
              setChosen(k);
              writeSort(k);
            }}
          />
        )}
      </div>

      {!catalog ? (
        error ? (
          <EmptyState
            title="Couldn't load the Community"
            body={error}
            action={
              <button
                type="button"
                className="mp-btn"
                onClick={() => void refreshCatalog({ force: true })}
                disabled={loading}
              >
                <RefreshCw size={14} strokeWidth={2.2} aria-hidden="true" /> Try
                again
              </button>
            }
          />
        ) : (
          <Delayed>
            <div className="mp-shelf-head">
              <span
                className="as-skel"
                style={{ width: 120, height: 16, borderRadius: 5 }}
              />
            </div>
            <div className="mp-grid">
              {Array.from({ length: 6 }, (_, k) => (
                <CardSkeleton key={k} />
              ))}
            </div>
          </Delayed>
        )
      ) : (
        <>
          {home && featured.length > 0 && (
            <HeroCarousel apps={featured} catalog={catalog} />
          )}
          <section className="mp-results" aria-live="polite">
            <div className="mp-shelf-head">
              <h2 className="mp-shelf-title">
                {typed ? (
                  <>
                    Results for <span className="mp-q">“{q.trim()}”</span>
                    {category !== "all" && (
                      <span className="mp-results-in">
                        {" "}
                        in {labelOf(catalog, category)}
                      </span>
                    )}
                  </>
                ) : category === "all" ? (
                  "All apps"
                ) : (
                  labelOf(catalog, category)
                )}
              </h2>
              <span className="mp-results-count">
                {filtered.length}
                {useServer && search.nextCursor ? "+" : ""}{" "}
                {filtered.length === 1 ? "app" : "apps"}
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
                title={
                  q.trim() ? (
                    <>No apps match “{q.trim()}”</>
                  ) : (
                    "Nothing here yet"
                  )
                }
                body={
                  q.trim()
                    ? category !== "all"
                      ? `Nothing in ${labelOf(catalog, category)} matches. Try All categories, or a shorter word.`
                      : "Try a shorter word, or browse by category."
                    : "No apps in this category yet."
                }
                action={
                  q.trim() ? (
                    <button
                      type="button"
                      className="mp-btn"
                      onClick={onClearSearch}
                    >
                      Clear search
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="mp-btn"
                      onClick={() => onCategory("all")}
                    >
                      Browse all apps
                    </button>
                  )
                }
              />
            )}
            {useServer && search.nextCursor && (
              <div className="mp-load-more">
                <button
                  type="button"
                  className="mp-btn"
                  onClick={search.loadMore}
                  disabled={search.loadingMore}
                >
                  Load more
                </button>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}

function SortControl({
  value,
  onChange,
  disabled,
}: {
  value: SortKey;
  onChange: (k: SortKey) => void;
  disabled?: boolean;
}) {
  return (
    <label className="mp-sort">
      <ArrowDownWideNarrow size={13} strokeWidth={2.2} aria-hidden="true" />
      <span className="mp-sort-label">Sort</span>
      <select
        className="mp-sort-select"
        value={value}
        disabled={disabled}
        aria-label="Sort apps"
        onChange={(e) => onChange(e.target.value as SortKey)}
      >
        {SORTS.map((s) => (
          <option key={s.key} value={s.key}>
            {s.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function labelOf(catalog: Catalog | null, id: string): string {
  return catalog?.categories.find((c) => c.id === id)?.label ?? id;
}

function Chips({
  catalog,
  total,
  value,
  onChange,
}: {
  catalog: Catalog | null;
  total: number;
  value: string;
  onChange: (id: string) => void;
}) {
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
    <button
      type="button"
      className="mp-notice-btn"
      onClick={() => void refreshCatalog({ force: true })}
      disabled={loading}
    >
      <RefreshCw
        size={13}
        strokeWidth={2.2}
        aria-hidden="true"
        className={loading ? "is-spinning" : ""}
      />
      {loading ? "Retrying…" : "Retry"}
    </button>
  );
  if (catalog.stale) {
    return (
      <Notice tone="warn" action={retry}>
        You're offline or the Community is unreachable. Showing apps cached{" "}
        {ago(catalog.fetched_at)}.
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

/** The quiet last line of the page: where the catalog came from and when,
 *  plus a refresh that drops the local cache and re-fetches the catalog
 *  (it does not re-read the index). Stage B: the catalog names its index
 *  repo (`index`, "owner/name") and the link goes there; an older server
 *  without one still served the community repo. */
export function CatalogFooter({
  catalog,
  loading = false,
}: {
  catalog: Catalog | null;
  loading?: boolean;
}) {
  if (!catalog || catalog.source !== "udf-api") return null;
  const version = String(catalog.catalog_version ?? "").slice(0, 8);
  const index =
    typeof catalog.index === "string" &&
    /^[\w.-]+\/[\w.-]+$/.test(catalog.index)
      ? catalog.index
      : null;
  const repo = index ?? "fusedio/fused-render-community-apps";
  return (
    <footer className="mp-footer" title={catalog.warning ?? undefined}>
      {index ? "Reviewed apps from " : "Apps from "}
      <a
        href={`https://github.com/${repo}`}
        target="_blank"
        rel="noopener noreferrer"
      >
        {repo.split("/")[1]}
      </a>
      {version && <> · catalog {version}</>} · updated {ago(catalog.fetched_at)}
      <button
        type="button"
        className="mp-footer-refresh"
        onClick={() => void refreshCatalog({ force: true })}
        disabled={loading}
        title="Re-fetch the catalog"
        aria-label="Refresh catalog"
      >
        <RefreshCw
          size={12}
          strokeWidth={2.2}
          aria-hidden="true"
          className={loading ? "is-spinning" : ""}
        />
      </button>
    </footer>
  );
}
