// The lan entry's tiny pathname router: `/` the grid, `/bots` the bot list,
// `/bots/<id>` a chat. lan.py serves lan.html for all three, so every link is a
// plain <a href> that also works on a full reload; a plain left click is turned
// into history.pushState so the phone never re-downloads the page.
import { useEffect, useState, type AnchorHTMLAttributes, type MouseEvent } from "react";
import { cn } from "@platform/lib/utils";
import { parseRoute, type Route } from "./bots/lib";

const EVENT = "lan:navigate";

export function navigate(path: string, replace = false) {
  if (path === location.pathname + location.search) return;
  if (replace) history.replaceState(null, "", path);
  else history.pushState(null, "", path);
  window.dispatchEvent(new Event(EVENT));
  window.scrollTo(0, 0);
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseRoute(location.pathname));
  useEffect(() => {
    const on = () => setRoute(parseRoute(location.pathname));
    window.addEventListener("popstate", on);
    window.addEventListener(EVENT, on);
    return () => {
      window.removeEventListener("popstate", on);
      window.removeEventListener(EVENT, on);
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

export type Section = "apps" | "bots" | "tasks";

/** The `Apps | Bots | Tasks` segmented switch at the top of the grid and the
 *  lists. `badge` is the tasks' needs-you count (the Tasks page knows it; the
 *  others leave it out). */
export function SectionSwitch({ active, badge, className }: { active: Section; badge?: number; className?: string }) {
  const item = (key: Section, href: string, label: string, count?: number) => (
    <Link
      href={href}
      aria-current={active === key ? "page" : undefined}
      className={cn(
        "flex h-full flex-1 items-center justify-center gap-1.5 rounded-[10px] text-[15px] font-medium transition-colors",
        active === key
          ? "lan-seg-on text-[var(--fg)]"
          : "text-[var(--fg-muted)] active:text-[var(--fg)]",
      )}
    >
      {label}
      {count ? (
        <span
          className="lan-mono flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-[var(--warning)] px-1 text-[11px] font-semibold text-white"
          aria-label={`${count} need you`}
        >
          {count > 99 ? "99+" : count}
        </span>
      ) : null}
    </Link>
  );
  return (
    <nav
      aria-label="Section"
      className={cn("flex h-10 w-full max-w-[330px] gap-1 rounded-xl bg-[color-mix(in_srgb,var(--fg)_8%,transparent)] p-1", className)}
    >
      {item("apps", "/", "Apps")}
      {item("bots", "/bots", "Bots")}
      {item("tasks", "/tasks", "Tasks", badge)}
    </nav>
  );
}
