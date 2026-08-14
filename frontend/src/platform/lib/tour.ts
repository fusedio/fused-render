// First-run onboarding tour (driver.js). A minimal guided walkthrough of the
// shell chrome for brand-new users. Steps whose target element isn't in the
// DOM at start time are filtered out, so the tour never breaks on panel/prefs
// routes or when an element is conditionally hidden (e.g. embed mode).
import { driver, type DriveStep } from "driver.js";
import "driver.js/dist/driver.css";
import { IS_EMBED } from "@platform/lib/router";

const SEEN_KEY = "fused.tour.seen";

// Every step targets a stable selector already present in the shell chrome.
// Each description is one short, friendly sentence for a first-time user.
const STEPS: DriveStep[] = [
  {
    element: ".sidebar-brand",
    popover: {
      title: "Welcome to Fused Render",
      description: "Browse your local files and render them right here.",
    },
  },
  {
    element: "#home-link",
    popover: {
      title: "Home",
      description: "Search your files, and jump back into apps, Claude sessions and recent files.",
    },
  },
  {
    element: ".sidebar-bookmarks",
    popover: {
      title: "Bookmarks",
      description:
        "Save any view or URL here — drag one bookmark onto another to make a folder.",
    },
  },
  {
    element: ".listing-search",
    popover: {
      title: "Search files",
      description: "Fuzzy-search files in this folder, recursively.",
    },
  },
  {
    element: "#breadcrumb",
    popover: {
      title: "You are here",
      description: "Every view lives in the URL — copy or bookmark it to return.",
    },
  },
  // Targets the listing body, NOT `#split-right-btn`. The tour auto-starts on
  // the explorer's first screen, which is always a folder — and over a folder
  // the crumb bar's layout zone is claimed (listing/folder-chrome.ts), so the
  // split buttons aren't in the DOM and this step was silently filtered out of
  // every first run. `.listing-split` is always there, and the copy now covers
  // both halves of the feature: the pane the folder view opens for itself, and
  // the buttons that appear once a file is open.
  {
    element: ".listing-split",
    popover: {
      title: "Side by side",
      description:
        "Pick a file and it previews right here, beside the list. Open one and the bar offers full split panes.",
    },
  },
  // Was `.bar-overflow`, the crumb bar's path `⋮` — which no longer exists in any
  // explorer bar: its items are the bar's RIGHT-CLICK menu now (see
  // apps/explorer/Breadcrumb). That step had been dead for every first run
  // anyway, since the tour auto-starts on a FOLDER and the folder view had
  // already taken these actions into its own header `⋮`. So it points at the
  // button that is actually on screen, and says the right-click out loud: a menu
  // with no button is a menu nobody finds unless something tells them.
  {
    element: ".listing-head-menu",
    popover: {
      title: "More actions",
      description:
        "New files and folders, reveal in your file manager, copy the path — also on a right-click, here or on the bar above.",
    },
  },
];

function presentSteps(): DriveStep[] {
  return STEPS.filter(
    (s) => typeof s.element === "string" && document.querySelector(s.element)
  );
}

// The one live driver instance. runTour is a no-op while a tour is already on
// screen, so the delayed auto-start can never stack on a manual "?" replay
// (and vice versa).
let active: ReturnType<typeof driver> | null = null;

function runTour(steps: DriveStep[]): void {
  if (active?.isActive()) return;
  const markSeen = () => {
    try {
      localStorage.setItem(SEEN_KEY, "1");
    } catch {
      /* localStorage may be unavailable; tour just replays next time */
    }
  };
  const d = driver({
    showProgress: true,
    allowClose: true,
    steps,
    onDestroyed: () => {
      active = null;
      markSeen();
    },
  });
  active = d;
  d.drive();
}

// Manual replay (footer "?" button): always runs, using whatever steps are
// currently on screen, and marks the tour as seen.
export function startTour(): void {
  const steps = presentSteps();
  if (steps.length === 0) return;
  runTour(steps);
}

// First-run auto-start: only for a fresh, non-embed user with the sidebar
// mounted. Called after paint so the listing/breadcrumb exist. Returns true
// when there is nothing left to do (tour started, already seen, or embed) and
// false when the shell chrome simply isn't on screen yet — the caller retries
// on the next route change, since a first visit can land on a chrome-free
// route ("/" or /apps) that has no tour targets.
export function maybeAutoStartTour(): boolean {
  if (IS_EMBED) return true;
  let seen = false;
  try {
    seen = localStorage.getItem(SEEN_KEY) === "1";
  } catch {
    seen = false;
  }
  if (seen) return true;
  if (!document.querySelector("#sidebar")) return false;
  // The walkthrough is built around the explorer chrome (bookmarks, listing
  // search, breadcrumb, split). A first visit lands on /apps or /explorer,
  // where the global sidebar matches a few early steps — starting there would
  // run a truncated tour AND mark it seen. `.sidebar-bookmarks` no longer
  // separates the routes (the sidebar is global, so it's on every one); the
  // folder listing's split pane is the chrome that only a real fs folder view
  // has. Wait for it; the caller retries on every route change.
  if (!document.querySelector(".listing-split")) return false;
  // A collapsed sidebar is a rail with none of the tour's sidebar targets
  // (.sidebar-brand, #home-link, .sidebar-bookmarks) — starting there
  // would run a truncated walkthrough and mark it seen. The brand row exists
  // only in the expanded frame, so it is the expansion check.
  if (!document.querySelector(".sidebar-brand")) return false;
  const steps = presentSteps();
  if (steps.length === 0) return false;
  runTour(steps);
  return true;
}
