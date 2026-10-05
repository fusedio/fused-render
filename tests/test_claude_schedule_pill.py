"""A scheduled task's ran message links to its session.

The composer used to schedule on its own — the "Send now" pill, deleted
2026-08-16 — and this file was its suite. The chat-side watcher that put a
server-spawned turn on screen lived in the retired iframe chat page; what is left
here is the link invariant in the Tasks list, which outlived both.
"""
import os



def test_the_page_links_a_ran_message_to_its_session():
    """This page can say a message ran; only the transcript knows what it DID. So a
    row whose watcher captured a session id hands the reader straight to that
    conversation — and a row without one simply offers no link rather than pointing
    at nothing.

    The invariant is about the LINK, not the file, so it has followed the rows
    twice: out of Scheduled.tsx into ScheduleTaskViews.tsx (the flow-app tree +
    kanban port, 2026-08-16), and then into tasks-lib.ts when the page moved to
    the task/thread/message model (2026-08-17). `taskHref` is where the gate
    lives now: a task carries `session_id` (empty until its first run), and the
    href is null rather than a URL while it is empty.

    A message adds a second half — the anchor that scrolls the transcript to
    that turn — and it inherits the same gate by construction, because it is
    built from `taskHref` and returns null whenever that does."""
    with open(os.path.join("frontend", "src", "shell", "tasks-lib.ts"),
              encoding="utf-8") as f:
        lib = f.read()
    assert "explorerUrl(" in lib
    assert "task.session_id" in lib, "the link must be gated on having an id"
    assert "return null" in lib, "no id must mean no link, not a link to nothing"
    # The message link is the task link plus an anchor, so it cannot acquire a
    # session id the task does not have.
    assert "const base = taskHref(task)" in lib
    assert "if (!base) return null" in lib

    with open(os.path.join("frontend", "src", "shell", "ScheduleTaskViews.tsx"),
              encoding="utf-8") as f:
        page = f.read()
    # the shell's own navigation, not a full page load
    assert "navigateUrl(" in page
