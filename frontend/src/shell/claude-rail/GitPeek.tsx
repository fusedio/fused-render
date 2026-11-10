// THE GIT CARD OFF THE RAIL'S GIT DOOR (owner, 2026-11-10: "move Git from the
// status bar's chip to a door on the rail, under the task rings — hover
// previews a window A card, a click pins, a click again closes, exactly the
// Tasks/Bots door rules; the card is full window A size, its content the
// bottom Git popup's"). Behind the rail flag, like the door (ClaudeRail
// `navDoors`), and only while the rail it stands beside has a repository:
// the repository gone (a path change, a page without one), the card goes,
// and the door with its hairline.
//
// WHAT IT IS: the Git chip's panel, now GitPanelBody — the branch and its
// tags, Check for updates, the warn line, and the commits filling the card
// (`useRailGit` asks for fifty; the list is the scrolling section). The facts
// are the rail's it stands beside (rail-git-store, keyed by rail; an explorer
// split is two rails on two repositories).
//
// MOUNTED ONCE, IN THE SHELL (App.tsx, beside BotsPeek), for the Tasks card's
// reason: the rail is a page's and comes and goes with it. The chassis is
// window A's (DoorCard): size, grips, timings, bridge and cone, Escape, the
// keyboard's seats, clear of window B. ClaudeRail holds the rules between the
// cards (one window A card at a time; after a click only a click changes
// window A; pane open / pane chat change / a press into the frame close it).
//
// THE HEAD: the × in the top-left corner, "Git", and Open — the page's Git
// panel, only where the page has one; Open and a commit's press close the
// card and open that panel. No room beside the rail: the door's press opens
// the panel directly (DoorCard `onNoRoomPress`).
import { useCallback, useEffect, useState } from "react";
import { X } from "lucide-react";
import { GitPanelBody } from "@shell/GitPanelBody";
import { DoorCard } from "./DoorCard";
import { useFavPeekState } from "./fav-peek-store";
import { gitCardStore, useGitCard, useGitCardRail } from "./git-peek-store";

/** How long a PINNED card waits, its rail gone without git, for the next
 *  rail's repository (a hop: the old rail's entry goes in the commit, the
 *  shell's top rail moves a microtask later, and a page may mount its rail
 *  after a stat). No repository by then: it closes. */
export const GIT_FOLLOW_MS = 600;
import { useRailGitAnyRail, useRailGitDoor, useRailGitMaybe } from "./rail-git-store";

export function GitPeek() {
  // BESIDE THE RAIL THAT OPENED IT (git-peek-store `setGitCardRail`, set by
  // the door before its hover or press reaches the card; an explorer split's
  // other rail), else the top rail where it has git, else the latest rail
  // that has — and that rail's facts.
  const top = useFavPeekState().rail;
  const opener = useGitCardRail();
  const topHasGit = useRailGitMaybe(top);
  const anyRail = useRailGitAnyRail();
  const rail = opener ?? (topHasGit ? top : (anyRail ?? top));
  const git = useRailGitDoor(rail);
  // REGISTERED WHILE ANY RAIL HAS A REPOSITORY (or a first read out) — not
  // only the watched one (Bugbot on #2236: a split whose top rail has none
  // left the other rail's door with no card, its press the panel's).
  const maybe = anyRail !== null;
  // A PINNED CARD FOLLOWS A HOP (QA, 2026-11-10: a folder → file hop in one
  // repository closed it): the moment its rail says "no git" it is held, not
  // closed, for `GIT_FOLLOW_MS` — set during the render, so the chassis never
  // sees one render disabled (its close runs on that). A preview is not held.
  const { pinned } = useGitCard();
  const [was, setWas] = useState(maybe);
  const [holding, setHolding] = useState(false);
  if (was !== maybe) {
    setWas(maybe);
    setHolding(!maybe && pinned);
  }
  useEffect(() => {
    if (!holding) return;
    const t = window.setTimeout(() => setHolding(false), GIT_FOLLOW_MS);
    return () => window.clearTimeout(t);
  }, [holding]);
  const openGit = git?.onOpenGit;
  const noRoom = useCallback(() => openGit?.(), [openGit]);
  return (
    <DoorCard
      store={gitCardStore}
      {...(openGit ? { onNoRoomPress: noRoom } : {})}
      rail={rail}
      className="claude-rail-git-peek"
      ariaLabel="Git"
      enabled={maybe || holding}
      subOpen={false}
    >
      {(api) => {
        const take = openGit
          ? () => {
              api.close();
              openGit();
            }
          : undefined;
        return (
          <>
            <div className="claude-rail-peek-head">
              <div className="claude-rail-peek-row">
                <button ref={api.closeRef} type="button" className="claude-rail-peek-btn claude-rail-peek-close" aria-label="Close Git" title="Close" onClick={api.close}>
                  <X size={14} aria-hidden="true" />
                </button>
                <span className="claude-rail-tasks-title" title="Git">
                  Git
                </span>
                <span className="claude-rail-peek-gap" aria-hidden="true" />
                {take && (
                  <button type="button" className="btn claude-rail-peek-open" title="Open the Git panel" onClick={take}>
                    Open
                  </button>
                )}
              </div>
            </div>
            <div className="claude-rail-peek-body">{git && <GitPanelBody git={git} {...(take ? { onTake: take } : {})} />}</div>
          </>
        );
      }}
    </DoorCard>
  );
}
