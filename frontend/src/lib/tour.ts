// First-run onboarding tour (driver.js). A minimal guided walkthrough of the
// shell chrome for brand-new users. Steps whose target element isn't in the
// DOM at start time are filtered out, so the tour never breaks on panel/prefs
// routes or when an element is conditionally hidden (e.g. embed mode).
import { driver, type DriveStep } from "driver.js";
import "driver.js/dist/driver.css";
import { IS_EMBED } from "./router";

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
    element: "#fused-link",
    popover: {
      title: "Fused workspace",
      description: "Your Fused folder — example views and data live in here.",
    },
  },
  {
    element: ".sidebar-bookmarks",
    popover: {
      title: "Bookmarks",
      description: "Save any view or URL here — drag one bookmark onto another to make a folder.",
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
  {
    element: ".split-dir",
    popover: {
      title: "Split view",
      description: "Open two panes side by side — like code next to its render.",
    },
  },
  {
    element: ".reveal-btn",
    popover: {
      title: "Open in Finder",
      description: "Reveal the current folder or file in your file manager.",
    },
  },
];

function presentSteps(): DriveStep[] {
  return STEPS.filter((s) => typeof s.element === "string" && document.querySelector(s.element));
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
// mounted. Called after first paint so the listing/breadcrumb exist.
export function maybeAutoStartTour(): void {
  if (IS_EMBED) return;
  let seen = false;
  try {
    seen = localStorage.getItem(SEEN_KEY) === "1";
  } catch {
    seen = false;
  }
  if (seen) return;
  if (!document.querySelector("#sidebar")) return;
  const steps = presentSteps();
  if (steps.length === 0) return;
  runTour(steps);
}
