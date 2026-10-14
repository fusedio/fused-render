// The Marketplace (apps/marketplace) — one lazy route for the whole
// `/marketplace` prefix (shell/App.tsx), split here by path:
//
//   /marketplace                 Discover  (?category=<id>, ?q=<text>)
//   /marketplace/installed       Installed / Updates
//   /marketplace/app/<slug>      App detail
//
// The page owns the scroll container, so the sticky header can sit inside it
// and know when the content has scrolled under it. The search query lives
// here: on Discover it filters in place (URL kept with replaceState); on the
// other pages Enter carries it to Discover.
import { useEffect, useRef, useState } from "react";
import { navigateUrl, pushHistory, replaceSearch } from "@platform/lib/router";
import { MARKETPLACE_HOME, useCatalog } from "@platform/lib/marketplace";
import { StoreHeader, type StoreTab } from "./StoreHeader";
import { CatalogFooter, Discover } from "./Discover";
import { InstalledPage } from "./InstalledPage";
import { AppDetailPage } from "./AppDetailPage";
import { InstallSheetHost } from "./install-flow";
import { EmptyState } from "./shared";

type Route = { kind: "discover" } | { kind: "installed" } | { kind: "app"; slug: string } | { kind: "unknown" };

export function parseMarketplacePath(pathname: string): Route {
  const p = pathname.replace(/\/+$/, "");
  if (p === MARKETPLACE_HOME) return { kind: "discover" };
  if (p === MARKETPLACE_HOME + "/installed") return { kind: "installed" };
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
  const s = sp.toString();
  return MARKETPLACE_HOME + (s ? "?" + s : "");
}

export function MarketplacePage() {
  const route = parseMarketplacePath(location.pathname);
  const snap = useCatalog();
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

  // Back/Forward between two filters of THIS page: the chip's pushState is
  // not a navigation (no nav event), so the shell's route mark never moved
  // and its popstate check sees "same route" — no remount. Re-read the URL
  // here instead. (A pop that does change route remounts us anyway.)
  useEffect(() => {
    if (route.kind !== "discover") return;
    const onPop = () => {
      if (parseMarketplacePath(location.pathname).kind !== "discover") return;
      const next = readParams();
      setCategory(next.category);
      setQ(next.q);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [route.kind]);

  const apps = snap.catalog?.apps ?? [];
  const updates = apps.filter((a) => a.installed && a.update_available && a.compatible).length;
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
    body = <AppDetailPage slug={route.slug} snap={snap} />;
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
    <div className="mp-page" ref={scrollRef} onScroll={(e) => setScrolled(e.currentTarget.scrollTop > 4)}>
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
      <main className="mp-inner">
        {body}
        {route.kind !== "unknown" && <CatalogFooter catalog={snap.catalog} />}
      </main>
      <InstallSheetHost />
    </div>
  );
}

export default MarketplacePage;
