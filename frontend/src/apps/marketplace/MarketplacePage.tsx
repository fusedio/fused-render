// The Marketplace (apps/marketplace) — one lazy route for the whole
// `/marketplace` prefix (shell/App.tsx), split here by path:
//
//   /marketplace                 Discover  (?category=<id>, ?q=<text>)
//   /marketplace/installed       Installed / Updates
//   /marketplace/app/<slug>      App detail (`?via=link`: reached from a
//                                listing link, fused-render://open?
//                                marketplace=<slug> — shown bare, with no
//                                store chrome, while the store is off;
//                                CONTRIB_LOOP.md §18.5)
//   /marketplace/file?path=<p>   A `.fused` app file's install page
//                                (FusedFilePage) — also rendered in place
//                                for `/explorer/view|embed/<p>.fused` by
//                                the shell, which passes `fusedFile`
//
// The page owns the scroll container, so the sticky header can sit inside it
// and know when the content has scrolled under it. The search query lives
// here: on Discover it filters in place (URL kept with replaceState); on the
// other pages Enter carries it to Discover.
import { useEffect, useRef, useState } from "react";
import { IS_EMBED, navigateUrl, pushHistory, replaceSearch } from "@platform/lib/router";
import { useNavEpoch } from "@platform/lib/hooks";
import { MARKETPLACE_HOME, releaseWaiting, useCatalog } from "@platform/lib/marketplace";
import { useMarketplaceFeature } from "@platform/lib/marketplace-flag";
import { StoreHeader, type StoreTab } from "./StoreHeader";
import { CatalogFooter, Discover } from "./Discover";
import { InstalledPage } from "./InstalledPage";
import { AppDetailPage } from "./AppDetailPage";
import { FusedFilePage } from "./FusedFilePage";
import { InstallSheetHost } from "./install-flow";
import { EmptyState } from "./shared";

type Route =
  | { kind: "discover" }
  | { kind: "installed" }
  | { kind: "app"; slug: string }
  | { kind: "file"; path: string }
  | { kind: "unknown" };

/** A listing link's landing (§18.5): the deep link opens the app's page with
 *  this marker, and the page is shown bare when the store is off. */
export function isListingLink(search: string): boolean {
  return new URLSearchParams(search).get("via") === "link";
}

/** The `.fused` install page's own address. */
export const MARKETPLACE_FILE = MARKETPLACE_HOME + "/file";

export function fusedFileHref(path: string): string {
  return MARKETPLACE_FILE + "?path=" + encodeURIComponent(path);
}

export function parseMarketplacePath(pathname: string, search = ""): Route {
  const p = pathname.replace(/\/+$/, "");
  if (p === MARKETPLACE_HOME) return { kind: "discover" };
  if (p === MARKETPLACE_HOME + "/installed") return { kind: "installed" };
  if (p === MARKETPLACE_FILE) {
    const path = new URLSearchParams(search).get("path") || "";
    return path.startsWith("/") ? { kind: "file", path } : { kind: "unknown" };
  }
  const m = /^\/marketplace\/app\/([^/]+)$/.exec(p);
  if (m) {
    try {
      return { kind: "app", slug: decodeURIComponent(m[1]) };
    } catch {
      return { kind: "unknown" };
    }
  }
  return { kind: "unknown" };
}

function readParams(): { category: string; q: string } {
  const sp = new URLSearchParams(location.search);
  return { category: sp.get("category") || "all", q: sp.get("q") || "" };
}

function searchFor(category: string, q: string): string {
  const sp = new URLSearchParams();
  if (category && category !== "all") sp.set("category", category);
  if (q.trim()) sp.set("q", q.trim());
  // Discover's sort (?sort=) is its own control's to change: kept here.
  const sort = new URLSearchParams(location.search).get("sort");
  if (sort) sp.set("sort", sort);
  const s = sp.toString();
  return MARKETPLACE_HOME + (s ? "?" + s : "");
}

export function MarketplacePage({ fusedFile }: { fusedFile?: string } = {}) {
  // Discover, Installed and an app's page are paths of ONE mounted page, so the
  // header and scroll container survive a hop between them. `useNavEpoch` is
  // the subscription (it counts pushState and popstate alike); the route is
  // re-read from the URL on every render.
  const navEpoch = useNavEpoch();
  const route: Route = fusedFile ? { kind: "file", path: fusedFile } : parseMarketplacePath(location.pathname, location.search);
  // A chrome-free embed (the window a `.fused` double-click opened) shows the
  // page alone: no store header to wander off into the catalog from. So does
  // a `.fused` opened with the store OFF (`marketplace_enabled`; the install
  // page is the one `.fused` view whatever the flag says): no header whose
  // tabs would land on the "Marketplace is off" notice, and no catalog read
  // — the store's API is not asked for on a file open the flag never offered.
  const storeOn = useMarketplaceFeature();
  // A listing link (`?via=link`) lands on the app's page whatever the flag
  // says — the `.fused` rule extended to listings — and, with the store
  // off, bare: no header whose tabs lead to the "Marketplace is off" notice.
  const viaLink = route.kind === "app" && isListingLink(location.search);
  const bare = (route.kind === "file" && (IS_EMBED || storeOn !== true)) || (viaLink && storeOn !== true);
  const snap = useCatalog(!bare);
  const initial = route.kind === "discover" ? readParams() : { category: "all", q: "" };
  const [category, setCategory] = useState(initial.category);
  const [q, setQ] = useState(initial.q);
  const [scrolled, setScrolled] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Arriving on Discover with a query (Enter on another page) keeps typing.
  useEffect(() => {
    if (route.kind === "discover" && initial.q) {
      const el = inputRef.current;
      el?.focus();
      el?.setSelectionRange(el.value.length, el.value.length);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Landing on another page of the store, or on Discover with other filters
  // than the page shows (the Discover link dropping them): back to the top, and
  // on Discover the filters the URL names. Not on the first render, which
  // already read them.
  const routeKey = route.kind === "app" ? "app:" + route.slug : route.kind === "file" ? "file:" + route.path : route.kind;
  const lastRouteKey = useRef(routeKey);
  useEffect(() => {
    const next = route.kind === "discover" ? readParams() : { category: "all", q: "" };
    const moved = lastRouteKey.current !== routeKey;
    if (!moved && next.category === category && next.q === q) return;
    lastRouteKey.current = routeKey;
    scrollRef.current?.scrollTo({ top: 0 });
    setCategory(next.category);
    setQ(next.q);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navEpoch, routeKey]);

  // Back/Forward between two filters of THIS page: the chip's pushState is
  // not a navigation (no nav event), so the shell's route mark never moved
  // and its popstate check sees "same route" — no remount. Re-read the URL
  // here instead. (A pop that does change route remounts us anyway.)
  useEffect(() => {
    if (route.kind !== "discover") return;
    const onPop = () => {
      if (parseMarketplacePath(location.pathname, location.search).kind !== "discover") return;
      const next = readParams();
      setCategory(next.category);
      setQ(next.q);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [route.kind]);

  const apps = snap.catalog?.apps ?? [];
  const updates = apps.filter((a) => releaseWaiting(a) && a.compatible).length;
  const installedCount = apps.filter((a) => a.installed).length;

  const onQ = (next: string) => {
    setQ(next);
    if (route.kind === "discover") replaceSearch(searchFor(category, next));
  };
  const onCategory = (id: string) => {
    if (id === category) return;
    setCategory(id);
    pushHistory(null, searchFor(id, q));
    scrollRef.current?.scrollTo({ top: 0 });
  };
  const onSubmit = () => {
    if (route.kind !== "discover" && q.trim()) navigateUrl(searchFor("all", q));
  };

  const tab: StoreTab = route.kind === "discover" ? "discover" : route.kind === "installed" ? "installed" : null;

  let body;
  if (route.kind === "discover") {
    body = <Discover snap={snap} category={category} q={q} onCategory={onCategory} onClearSearch={() => onQ("")} />;
  } else if (route.kind === "installed") {
    body = <InstalledPage snap={snap} />;
  } else if (route.kind === "app") {
    body = <AppDetailPage slug={route.slug} snap={snap} bare={bare} />;
  } else if (route.kind === "file") {
    body = <FusedFilePage key={route.path} path={route.path} />;
  } else {
    body = (
      <EmptyState
        title="Page not found"
        body="This Marketplace address doesn't lead anywhere."
        action={
          <button type="button" className="mp-btn" onClick={() => navigateUrl(MARKETPLACE_HOME)}>
            Go to the Marketplace
          </button>
        }
      />
    );
  }

  return (
    <div className={"mp-page" + (bare ? " is-bare" : "")} ref={scrollRef} onScroll={(e) => setScrolled(e.currentTarget.scrollTop > 4)}>
      {!bare && (
        <StoreHeader
          tab={tab}
          q={q}
          onQ={onQ}
          onSubmit={onSubmit}
          scrolled={scrolled}
          updates={updates}
          installedCount={installedCount}
          inputRef={inputRef}
        />
      )}
      <main className="mp-inner">
        {body}
        {route.kind !== "unknown" && route.kind !== "file" && !bare && (
          <CatalogFooter catalog={snap.catalog} loading={snap.loading} />
        )}
      </main>
      <InstallSheetHost />
    </div>
  );
}

export default MarketplacePage;
