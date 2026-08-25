// The Home tour (/home). Four steps, not six: the search box, the strips as ONE
// region, one card inside them, and the sidebar's bookmarks. Three separate
// strip steps said the same thing three times — a new user is learning that the
// page is rows of cards, not learning each row.
import type { Tour } from "./registry";

export const homeTour: Tour = {
  id: "home",
  title: "Home",
  matches: (pathname) => pathname === "/home",
  // Hold the auto-start while the apps strip is still skeletons: the card step
  // filters placeholders out, so firing now would run without it and mark the
  // tour seen — skipping "Open an app" forever. The caller retries until the
  // strip has settled (real cards or an empty state, either is ready).
  readyWhen: () => !document.querySelector("#home-sec-apps .home-skel-card"),
  startPath: "/home",
  steps: () => [
    {
      element: ".home-hero",
      popover: {
        title: "Search your machine",
        description: "Search any file on your computer by name.",
      },
    },
    {
      // The whole measured column at once (Home's `.home-strips`), so apps,
      // playground, sessions and recents are lit as one region. It only exists
      // while no search is live — a live search unmounts it, and presentSteps
      // drops the step rather than breaking the tour.
      element: ".home-strips",
      popover: {
        title: "Everything you've been using",
        description: "Your apps, AI playground and Claude sessions live here.",
      },
    },
    {
      // The FIRST app card, not the strip: a pointer at the thing to press.
      // Plain Next on purpose — this is a "cards are pressable" aside, and
      // waiting for the click would send the user out of the tour mid-flow.
      // :not(.home-skel-card): while apps are still loading the strip shows
      // skeleton placeholders wearing the same card class, and a step pointed
      // at one spotlights a shimmer instead of an app.
      element: "#home-sec-apps .app-pcard:not(.home-skel-card)",
      popover: {
        title: "Open an app",
        description: "Click a card to open the app.",
      },
    },
    {
      // The heading row, not the whole section: the section flex-grows over the
      // sidebar's free height, so spotlighting it lights a mostly-empty column
      // and reads as pointing at nothing.
      element: ".sidebar-bookmarks .sidebar-heading",
      popover: {
        title: "Bookmarks",
        description: "Save any view here for one-click return.",
      },
    },
  ],
};
