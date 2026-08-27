// The Tasks tour (/tasks): two steps on the page, then the user MAKES a task
// and reads it. The making is real — the follow-up's last step presses Create,
// which creates the task, and a second follow-up picks the walkthrough back up
// over the list the new row landed in. The modal's fields and that row can't be
// first-run steps themselves (neither exists until the user acts), which is what
// the chained `followUp` is for.
import type { Tour } from "./registry";
import { prefillInput } from "./prefill";

export const tasksTour: Tour = {
  id: "tasks",
  title: "Tasks",
  matches: (pathname) => pathname === "/tasks",
  startPath: "/tasks",
  steps: () => [
    {
      element: ".schedule-view-seg",
      popover: {
        title: "Three views",
        description: "See tasks as a list, a board or a calendar.",
      },
    },
    // Stays on LIST: the view the tour started in is the view the created task
    // has to be visible in two follow-ups from now. A step that switched to the
    // calendar left the new row on a page nobody was looking at.
    {
      element: ".schedule-new",
      popover: {
        title: "Create your first task",
        description: "A task is Claude working in a folder you pick. Click here to make one.",
      },
    },
  ],
  followUp: {
    trigger: ".schedule-new",
    steps: () => [
      {
        element: ".new-task-title",
        popover: {
          title: "Name it",
          description: "Type a short title so you spot it in the list — ours is just a sample.",
        },
        // Sample text, not a suggestion the form keeps: prefillInput only fills
        // an empty box, so an edit or a replay never loses the user's words.
        //
        // The ask is filled here too, without a step of its own: a title alone
        // is already saveable (NewJobModal.saveEnabled — the message goes over
        // the wire as the title), but the next step really does create the task,
        // and a task whose whole instruction is its name has nothing to do.
        onEnter: () => {
          prefillInput(".new-task-title", "My first task");
          prefillInput(".new-task-ask", "Summarize the files in this folder into a NOTES.md.");
        },
      },
      {
        element: ".schedule-save",
        popover: {
          title: "Create it",
          description: "Create the task — it'll appear in the list.",
        },
        advanceOn: ".schedule-save",
        actionText: "Create it",
      },
    ],
    // The press above closes the modal and leaves a real task behind, so the
    // walkthrough continues over whichever view the page is showing. Chained
    // rather than folded into the steps above for the same reason the modal
    // needed a follow-up: the row does not exist until the click happens.
    //
    // ONE step per view, not one step: the first tour step points at the view
    // switcher without pinning List, so the user may be reading Board or
    // Calendar by now — and those render cards and chips, not `.tasks-row`.
    // The three are mutually exclusive on screen, so presentSteps keeps exactly
    // the one that is real and the other two drop out.
    followUp: {
      trigger: ".schedule-save",
      steps: () => [
        {
          // List, and BEFORE the row step: that one's action opens the task
          // and leaves the page, so anything about reading the list has to be
          // said first. The ring is the page's whole read-state vocabulary —
          // there is no separate unread mark (tasks.css "Unread") — and a
          // first-time reader has no way to know a hollow ring is a claim
          // (Akshil, 2026-08-27: "if the status ring has a dot in between,
          // that means it is unread ... add that to the tour").
          element: ".tasks-row .tasks-rowmark .schedule-ring",
          popover: {
            title: "Read or unread",
            description:
              "The ring is the task's status. A dot inside it means there's output you haven't seen yet; once you've looked, the ring goes hollow.",
          },
        },
        {
          // List. ROW ONE, not "the new one": rows sort by lane and then time
          // (tasks-lib.sortByLane), so an Upcoming task already on the page can
          // outrank the fresh one. The copy is true of whichever row this is.
          element: ".tasks-row",
          popover: {
            title: "Your tasks",
            description: "Your tasks land in this list. Click one to watch its output.",
          },
          // The row's press is a real <a> stretched over it (`.tasks-rowlink`),
          // so that is what both a real click and the action button spend. A row
          // with nowhere to go (an unsent scheduled entry) has no link, and the
          // step falls back to a plain Next.
          advanceOn: ".tasks-rowlink",
          actionText: "Open it",
        },
        {
          // Board. The card itself is the press (`.schedule-tv-card` is a
          // <button>), so a sibling selector advances what this one spotlights.
          element: ".schedule-tv-board .tasks-card-wrap",
          popover: {
            title: "Your tasks",
            description: "Your tasks land on this board. Click a card to watch its output.",
          },
          advanceOn: ".schedule-tv-board .schedule-tv-card",
          actionText: "Open it",
        },
        {
          // Calendar. A chip is small and opening it goes through a popover,
          // so this step just points at the grid — plain Next, no advanceOn.
          element: ".schedule-cal",
          popover: {
            title: "Your tasks",
            description: "Your task is on this calendar — click its chip to watch the output.",
          },
        },
      ],
    },
  },
};
