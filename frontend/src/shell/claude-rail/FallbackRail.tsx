// THE SHELL'S FALLBACK RAIL (owner, 2026-11-05: "pages without a page-level
// rail — home, Bots, Tasks, AI Models, Community, Preferences… — must show the
// rail when there are fav tasks"). The page rails (ClaudeRail on the canvas,
// app and explorer pages) stand where a page has a place; every other page
// gets this one, mounted once in the shell (App.tsx) at #main's right edge:
// the same rail with NO PLACE — no list, no +, no Git, no panel glyph — only
// the Favourites section, and hidden while there are none (ClaudeRail
// `favouritesOnly`, claude-rail-lib `railShown`). The favourite's card
// (FavouritePeek) stands beside it as beside any rail: it registers like one.
//
// ONE REGISTRY CHECK: mounted while favourites are on and no page rail is
// mounted (fav-peek-store `registerPageRail`, claude-rail-lib
// `fallbackRailUp`). The count is said synchronously from the page rails'
// layout phase, so a hop swaps the two in one paint — never both, never a
// frame with neither.
//
// Laid out by claude-rail.css: the shell's `.shell-main-row` wrappers are
// `display: contents` (the page's layout untouched) until this rail stands,
// when they become a row — the page's column, then this 48px rail.
import type { PointerEvent as ReactPointerEvent } from "react";
import { navigateUrl } from "@platform/lib/router";
import { useTaskFavourites } from "@platform/lib/favourites-flag";
import { ClaudeRail, type NewTaskSeed } from "./ClaudeRail";
import { usePageRailCount } from "./fav-peek-store";
import { useCanvasRailPeek } from "./rail-peek-flag";
import { fallbackRailUp, type RailPlace } from "./claude-rail-lib";

/** No place (header): a folder no row lives in, so nothing rides the list
 *  and every favourite's where-chip names its own place. */
const NO_PLACE: RailPlace = { kind: "folder", dir: "\u0000" };
const NO_TASK: NewTaskSeed = { target: null, waiting: null, on: null };
const noop = () => {};
const noPull = (_e: ReactPointerEvent<HTMLDivElement>) => {};

export function FallbackRail() {
  // The favourites switch AND the card's (ClaudeRail `favsOn`): the star
  // lives on the card.
  const favourites = useTaskFavourites();
  const peekOn = useCanvasRailPeek();
  const favsOn = favourites && peekOn;
  const pageRails = usePageRailCount();
  if (!fallbackRailUp({ favsOn, pageRails })) return null;
  return (
    <ClaudeRail
      favouritesOnly
      place={NO_PLACE}
      newTask={NO_TASK}
      activeSessionId={null}
      onOpen={noop}
      onOpenChat={navigateUrl}
      onPullStart={noPull}
    />
  );
}
