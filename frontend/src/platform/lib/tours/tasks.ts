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
    // walkthrough continues over the list. Chained rather than folded into the
    // steps above for the same reason the modal needed a follow-up: the row does
    // not exist until the click happens.
    followUp: {
      trigger: ".schedule-save",
      steps: () => [
        {
          // ROW ONE, not "the new one": the list is sorted by lane and then by
          // time (tasks-lib.sortByLane), so an Upcoming task already on the page
          // outranks a task that just started running. The copy is written to be
          // true of whichever row that is.
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
      ],
    },
  },
};
