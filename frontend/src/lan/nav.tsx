// The phone page's router. Three sections — the Apps grid at `/`, the bots app
// under `/bots/*`, the Tasks page under `/tasks/*` — and the two latter are the
// DESKTOP'S OWN pages (D1467): `@apps/bots` reads `/bots/<id>` itself
// (hooks/useBotsRoute) and the shell's Tasks page reads `?peek=<key>`
// (task-peek-store). So this router only answers "which section", and leaves
// the rest of the URL to the page it mounts. Navigation inside those pages goes
// through `@platform/lib/router` (pushState + NAV_EVENT); this router listens
// to the same events, so a hop from either side moves both.
import { useEffect, useState, type AnchorHTMLAttributes, type MouseEvent } from "react";
import { ChevronLeftIcon, PlusIcon } from "lucide-react";
import { NAV_EVENT, navigateUrl } from "@platform/lib/router";
import { cn } from "@platform/lib/utils";

export type Section = "apps" | "bots" | "tasks";

export type Route =
  | { name: "grid" }
  | { name: "bots"; id: string | null }
  | { name: "tasks"; peek: string | null };

/** The lan page's sections. lan.py serves lan.html for `/`, `/bots`,
 *  `/bots/<…>` and `/tasks[/<key>]`; anything else falls back to the grid.
 *  `/tasks/<key>` is the iOS shell's deep link (a session id or
 *  `pending:<entry>`); the Tasks page itself wants `?peek=<key>`, and `main.tsx`
 *  rewrites the one into the other before mounting. */
export function parseRoute(pathname: string, search: string): Route {
  const p = (pathname || "/").replace(/\/+$/, "") || "/";
  if (p === "/bots") return { name: "bots", id: null };
  const m = /^\/bots\/([A-Za-z0-9_-]+)$/.exec(p);
  if (m) return { name: "bots", id: m[1] };
  if (p.startsWith("/bots/")) return { name: "bots", id: null };
  if (p === "/tasks") return { name: "tasks", peek: new URLSearchParams(search).get("peek") };
  const t = /^\/tasks\/([^/]+)$/.exec(p);
  if (t) {
    try {
      const key = decodeURIComponent(t[1]);
      if (/^[A-Za-z0-9_:.-]+$/.test(key)) return { name: "tasks", peek: key };
    } catch {
      /* fall through */
    }
    return { name: "tasks", peek: null };
  }
  return { name: "grid" };
}

/** A `/tasks/<key>` deep link as the Tasks page reads it: `/tasks?peek=<key>`. */
export function peekUrl(key: string): string {
  return `/tasks?peek=${encodeURIComponent(key)}`;
}

export function navigate(path: string, replace = false) {
  if (path === location.pathname + location.search) return;
  void navigateUrl(path, { replace });
  window.scrollTo(0, 0);
}

const EVENTS = ["popstate", NAV_EVENT, "fused:urlchange"];

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseRoute(location.pathname, location.search));
  useEffect(() => {
    const on = () => setRoute(parseRoute(location.pathname, location.search));
    for (const ev of EVENTS) window.addEventListener(ev, on);
    return () => {
      for (const ev of EVENTS) window.removeEventListener(ev, on);
    };
  }, []);
  return route;
}

/** An in-page link: a real href (long-press, reload and new-tab all work), a
 *  pushState on a plain click. */
export function Link({ href, onClick, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }) {
  const click = (e: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(e);
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    navigate(href);
  };
  return <a href={href} onClick={click} {...rest} />;
}

/** The title row every lan page shares — the section switch and one "+" (or,
 *  inside a bot's chat, a Back to the list) — so switching tabs never moves
 *  anything: same height, same slots, no counts. */
export function HeaderRow({
  active,
  badge,
  addLabel,
  onAdd,
  addDisabled,
  back,
}: {
  active: Section;
  badge?: number;
  addLabel?: string;
  onAdd?: () => void;
  addDisabled?: boolean;
  /** A page one level in (a bot's chat): the switch gives way to a Back. */
  back?: { href: string; label: string };
}) {
  return (
    <div className="flex h-10 items-center justify-between gap-3 box-content">
      {back ? (
        <Link href={back.href} className="flex h-10 items-center gap-1 rounded-xl pr-3 text-[15px] text-[var(--fg)] active:opacity-70">
          <ChevronLeftIcon className="size-5" />
          {back.label}
        </Link>
      ) : (
        <SectionSwitch active={active} badge={badge} />
      )}
      {onAdd ? (
        <button
          type="button"
          aria-label={addLabel}
          onClick={onAdd}
          disabled={addDisabled}
          className="flex size-10 shrink-0 items-center justify-center rounded-xl border border-[var(--sh-border)] bg-[var(--card)] text-[var(--fg)] active:bg-[var(--row-bg-hover)] disabled:opacity-40"
        >
          <PlusIcon className="size-5" />
        </button>
      ) : null}
    </div>
  );
}

/** The `Apps | Bots | Tasks` segmented switch at the top of every section.
 *  `badge` is the tasks' needs-you count (the Tasks page knows it; the others
 *  leave it out). */
export function SectionSwitch({ active, badge, className }: { active: Section; badge?: number; className?: string }) {
  const item = (key: Section, href: string, label: string, count?: number) => (
    <Link
      key={key}
      href={href}
      aria-current={active === key ? "page" : undefined}
      className={cn(
        "relative flex h-9 items-center rounded-lg px-3 text-[15px] font-medium",
        active === key ? "lan-seg-on" : "text-[var(--fg-muted)]",
      )}
    >
      {label}
      {count ? (
        <span className="ml-1.5 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-[var(--accent)] px-1 text-[11px] font-semibold text-[var(--accent-fg,#fff)]">
          {count > 99 ? "99+" : count}
        </span>
      ) : null}
    </Link>
  );
  return (
    <nav aria-label="Section" className={cn("flex items-center gap-0.5 rounded-xl bg-[var(--card)] p-0.5", className)}>
      {item("apps", "/", "Apps")}
      {item("bots", "/bots", "Bots")}
      {item("tasks", "/tasks", "Tasks", badge)}
    </nav>
  );
}
