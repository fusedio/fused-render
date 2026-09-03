// The phone grid. One screen, one job: find the app, tap it.
//
// Data is /api/lan/apps (fused_render/lan.py) — the same rows and the same
// order the desktop /apps hub shows (opened_at ?? updated_at, newest first), so
// the two never disagree about what is "recent". Search matches title, name
// and tag; the filter strip is the set of tags actually present plus "Linked"
// for folders outside ~/Fused. Recency is the sort key, so the dividers say
// it out loud (Today / This week / Earlier) instead of decorating.
import { useEffect, useMemo, useState } from "react";
import { SearchIcon, XIcon, AppWindowIcon, WifiOffIcon } from "lucide-react";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@platform/shadcn/ui/empty";
import { Input } from "@platform/shadcn/ui/input";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { ToggleGroup, ToggleGroupItem } from "@platform/shadcn/ui/toggle-group";
import { cn } from "@platform/lib/utils";
import { Identifier, SectionHeading } from "@platform/ui/flow/Typography";

interface LanAppRow {
  name: string;
  title: string | null;
  tag: string | null;
  path: string;
  linked: boolean;
  recency: number; // epoch seconds; 0 = never opened and no mtime
  url: string;
  preview: string | null; // preview.png, full-bleed
  icon: string | null; // icon.svg
}

const FILTER_KEY = "fused-render:lan-filter";
// Inside the native iOS shell (its webview marks the UA; lan.py keys off the
// same marker). The shell draws its own top bar, so the page drops its title.
const IN_APP = /FusedRender-iOS\//.test(navigator.userAgent);
const LINKED = "__linked";

function readFilter(): string {
  try {
    return localStorage.getItem(FILTER_KEY) || "";
  } catch {
    return "";
  }
}
function writeFilter(value: string) {
  try {
    if (value) localStorage.setItem(FILTER_KEY, value);
    else localStorage.removeItem(FILTER_KEY);
  } catch {
    /* private mode */
  }
}

function bucketOf(recency: number, now: number): string {
  if (!recency) return "Never opened";
  const age = now - recency;
  if (age < 86400) return "Today";
  if (age < 7 * 86400) return "This week";
  if (age < 30 * 86400) return "This month";
  return "Earlier";
}

function ago(recency: number, now: number): string {
  if (!recency) return "—";
  const s = Math.max(0, now - recency);
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  if (s < 30 * 86400) return `${Math.floor(s / 86400)}d`;
  const d = new Date(recency * 1000);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function LanApp() {
  const [apps, setApps] = useState<LanAppRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<string>(readFilter);
  const now = useMemo(() => Date.now() / 1000, [apps]);

  useEffect(() => {
    let alive = true;
    fetch("/api/lan/apps", { cache: "no-store" })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return (await r.json()) as { apps: LanAppRow[] };
      })
      .then((data) => alive && setApps(data.apps))
      .catch((e) => alive && setError((e as Error).message));
    return () => {
      alive = false;
    };
  }, []);

  // Tags in order of how many apps carry them; "Linked" only when something is.
  const tags = useMemo(() => {
    const counts = new Map<string, number>();
    for (const a of apps || []) if (a.tag) counts.set(a.tag, (counts.get(a.tag) || 0) + 1);
    const sorted = [...counts.entries()].sort((x, y) => y[1] - x[1] || x[0].localeCompare(y[0]));
    const out = sorted.map(([tag, count]) => ({ value: tag, label: tag, count }));
    const linked = (apps || []).filter((a) => a.linked).length;
    if (linked) out.push({ value: LINKED, label: "Linked", count: linked });
    return out;
  }, [apps]);

  // A stored filter whose tag no longer exists falls back to All.
  useEffect(() => {
    if (apps && filter && !tags.some((t) => t.value === filter)) {
      setFilter("");
      writeFilter("");
    }
  }, [apps, tags, filter]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (apps || []).filter((a) => {
      if (filter === LINKED && !a.linked) return false;
      if (filter && filter !== LINKED && a.tag !== filter) return false;
      if (!q) return true;
      return (
        (a.title || "").toLowerCase().includes(q) ||
        a.name.toLowerCase().includes(q) ||
        (a.tag || "").toLowerCase().includes(q)
      );
    });
  }, [apps, query, filter]);

  // Groups in the order the sorted list already has them.
  const groups = useMemo(() => {
    const out: { label: string; rows: LanAppRow[] }[] = [];
    for (const a of shown) {
      const label = bucketOf(a.recency, now);
      const last = out[out.length - 1];
      if (last && last.label === label) last.rows.push(a);
      else out.push({ label, rows: [a] });
    }
    return out;
  }, [shown, now]);

  const onFilter = (value: string) => {
    setFilter(value);
    writeFilter(value);
  };

  return (
    <main className="mx-auto max-w-3xl px-4 pb-[max(24px,env(safe-area-inset-bottom))] text-sm">
      <header className="sticky top-0 z-10 -mx-4 bg-background/90 px-4 pt-[max(12px,env(safe-area-inset-top))] pb-3 backdrop-blur">
        {/* Inside the native shell the top bar already says "Apps"; the page
            keeps only the search and filters. */}
        {!IN_APP && (
          <div className="flex items-baseline justify-between gap-3 pb-3">
            <h1 className="m-0 text-xl font-bold">Apps</h1>
            <Identifier>{apps ? `${shown.length}/${apps.length}` : ""}</Identifier>
          </div>
        )}
        <div className="relative">
          <SearchIcon className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            type="search"
            inputMode="search"
            enterKeyHint="search"
            autoCapitalize="off"
            autoCorrect="off"
            placeholder="Search apps"
            aria-label="Search apps"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="h-11 rounded-md pl-9 pr-9 text-base"
          />
          {query && (
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Clear search"
              onClick={() => setQuery("")}
              className="absolute top-1/2 right-1.5 -translate-y-1/2"
            >
              <XIcon />
            </Button>
          )}
        </div>
        {tags.length > 1 && (
          <div className="lan-strip -mx-4 mt-3 overflow-x-auto px-4 [scrollbar-width:none]">
            <ToggleGroup
              value={[filter]}
              onValueChange={(v) => onFilter((v[0] as string) ?? "")}
              variant="outline"
              size="sm"
              spacing={2}
              className="w-max"
              aria-label="Filter by tag"
            >
              <ToggleGroupItem value="" className="rounded-full px-3">
                All
              </ToggleGroupItem>
              {tags.map((t) => (
                <ToggleGroupItem key={t.value} value={t.value} className="rounded-full px-3">
                  {t.label}
                  <span className="ml-1.5 font-mono text-xs tabular-nums opacity-60">{t.count}</span>
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          </div>
        )}
      </header>

      {error && (
        <Empty className="mt-16">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <WifiOffIcon />
            </EmptyMedia>
            <EmptyTitle>Can't reach the computer</EmptyTitle>
            <EmptyDescription>
              {error}. Make sure the phone is on the same Wi-Fi and sharing is on in Preferences.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      )}

      {!error && apps === null && (
        <div className="grid grid-cols-2 gap-3 pt-4 sm:grid-cols-3">
          {Array.from({ length: 6 }, (_, i) => (
            <Skeleton key={i} className="aspect-[4/5] rounded-lg motion-reduce:animate-none" />
          ))}
        </div>
      )}

      {apps && apps.length === 0 && (
        <Empty className="mt-16">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <AppWindowIcon />
            </EmptyMedia>
            <EmptyTitle>No apps yet</EmptyTitle>
            <EmptyDescription>Apps you build on the computer show up here.</EmptyDescription>
          </EmptyHeader>
        </Empty>
      )}

      {apps && apps.length > 0 && shown.length === 0 && (
        <Empty className="mt-16">
          <EmptyHeader>
            <EmptyTitle>Nothing matches</EmptyTitle>
            <EmptyDescription>
              No app called “{query.trim()}”{filter ? ` under ${filter === LINKED ? "Linked" : filter}` : ""}.
            </EmptyDescription>
          </EmptyHeader>
          <Button variant="outline" size="sm" onClick={() => { setQuery(""); onFilter(""); }}>
            Show everything
          </Button>
        </Empty>
      )}

      {groups.map((g) => (
        <section key={g.label} className="pt-4">
          <SectionHeading className="m-0 mb-2 text-xs">{g.label}</SectionHeading>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            {g.rows.map((a) => (
              <AppCard key={a.path} app={a} now={now} />
            ))}
          </div>
        </section>
      ))}
    </main>
  );
}

function AppCard({ app, now }: { app: LanAppRow; now: number }) {
  const label = app.title || app.name;
  const initial = (label.trim()[0] || "?").toLowerCase();
  return (
    <a
      href={app.url}
      className={cn(
        "flex flex-col overflow-hidden rounded-lg border border-border bg-card text-card-foreground shadow-sm outline-none",
        "focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50",
        // Press feedback under the thumb; guarded.
        "motion-safe:transition-transform motion-safe:duration-100 motion-safe:active:scale-[0.97]",
      )}
    >
      {/* The tile: preview.png full-bleed, else the author's icon.svg as-is,
          else the app's initial on the muted surface (neutral — status colour
          is the only chroma on the page). */}
      <div className="flex aspect-[3/2] items-center justify-center overflow-hidden bg-muted text-muted-foreground" aria-hidden>
        {app.preview ? (
          <img src={app.preview} alt="" className="size-full object-cover" loading="lazy" />
        ) : app.icon ? (
          <img src={app.icon} alt="" className="size-12 object-contain" />
        ) : (
          <span className="text-2xl font-bold leading-none">{initial}</span>
        )}
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-2 p-3">
        <div className="line-clamp-2 text-sm leading-snug font-medium">{label}</div>
        <div className="mt-auto flex items-center justify-between gap-2">
          {app.tag ? (
            <Badge variant="secondary" className="max-w-[70%] truncate rounded-full px-2 text-xs">
              {app.linked ? "linked" : app.tag}
            </Badge>
          ) : (
            <span />
          )}
          <Identifier className="tabular-nums">{ago(app.recency, now)}</Identifier>
        </div>
      </div>
    </a>
  );
}
