"""What `GET /api/tasks/changes?since=&wait=0` and `GET /api/tasks/pulse`
used to answer, asked of the events bus's `tasks.listing` topic — the long-poll
and the pulse are gone (events bus, phase 5), but every test that pinned the
CHANGE semantics (which rows move, which keys go, which drafts bump) still
reads the same answer, through the same `_changes_answer`, here.

`changes(client, since)` answers `{"generation", "rows", "gone", "drafts"}` for
a window the ring can answer and `{"generation", "full": True}` otherwise —
the old GET's shapes exactly. `pulse(client)` is the listing reduced to the
sidebar's fields, which the pulse endpoint was.
"""
from fused_render import tasks_watch

PULSE_FIELDS = (
    "key", "status", "unread", "last_active", "project", "task_id", "title", "target",
    "session_id", "happened_at", "next_run", "next_run_entry", "next_run_repeats",
    "queue_position", "queue_ahead", "queue_priority", "queue_ahead_session", "queue_waiting",
    "entrypoint",
)


def changes(client, since: int, *, under: str = "", page: str | None = None) -> dict:
    from fused_render.server.events import RETRY
    from fused_render.server.topics import TasksListingTopic
    topic = TasksListingTopic()
    params = topic.validate({"under": under, "page": page})
    answer = topic.delta(params, int(since))
    if answer is None:
        return {"generation": tasks_watch.generation(), "full": True}
    if answer is RETRY:
        # The snapshot builder has not caught up with the change: the old GET
        # answered "nothing yet" at the client's OWN generation, and the bus
        # asks the topic again shortly.
        return {"generation": int(since), "rows": [], "gone": [],
                "drafts": {"changed": [], "gone": []}}
    body, _gen = answer
    return body


def pulse(client) -> list[dict]:
    rows = client.get("/api/tasks").json()["tasks"]
    return [{field: row.get(field) for field in PULSE_FIELDS} for row in rows
            if not _is_draft_row(row)]


def _is_draft_row(row: dict) -> bool:
    """The pulse never carried draft rows (a draft is not a task the sidebar
    counts): `draft:<id>` task drafts and `new:<file>` chat drafts."""
    return row.get("kind") == "draft"
