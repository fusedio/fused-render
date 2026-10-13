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

/** The `Apps | Bots` segmented switch at the top of the grid and the list. */
export function SectionSwitch({ active, className }: { active: "apps" | "bots"; className?: string }) {
  const item = (key: "apps" | "bots", href: string, label: string) => (
    <Link
      href={href}
      aria-current={active === key ? "page" : undefined}
      className={cn(
        "flex h-full flex-1 items-center justify-center rounded-[10px] text-[15px] font-medium transition-colors",
        active === key
          ? "lan-seg-on text-[var(--fg)]"
          : "text-[var(--fg-muted)] active:text-[var(--fg)]",
      )}
    >
      {label}
    </Link>
  );
  return (
    <nav
      aria-label="Section"
      className={cn("flex h-10 w-full max-w-[260px] gap-1 rounded-xl bg-[color-mix(in_srgb,var(--fg)_8%,transparent)] p-1", className)}
    >
      {item("apps", "/", "Apps")}
      {item("bots", "/bots", "Bots")}
    </nav>
  );
}
