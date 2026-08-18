"""One list of past chats, from one store (templates/claude/agent.py
`_sessions`).

The list is a scan of the transcripts already sitting in this cwd's
~/.claude/projects dir (D335 removed the per-file sidecar): whether a chat
started in this page or in a terminal, Claude Code wrote its transcript into
the same project dir, so ONE reader answers for both.

The whole reason this needs no resume path of its own is the fact these tests
exist to protect: a session's home is its cwd's project dir, and the template
keys on EXACTLY the same dir (`_munge(_workdir(file))`), so a transcript found
this way is already where `_history` reads and where `--resume` looks from.
Break that identity and the rows still render while nothing opens.

Reads of a transcript must stay cheap — this runs on every home-view paint — so
the title comes out of the file's head and the recency out of its mtime, never
out of a parse of the whole (multi-MB) thing.

The agent module is exec'd standalone, the way the fused engine execs it and the
way every other agent test here loads it (a template may not import
fused_render — SPEC PY-15 / D166).
"""
import importlib.util
import json
import os

import pytest

from fused_render import tasks_store
from tests import _machinery_records as records


def _load_agent():
    path = os.path.join("fused_render", "templates", "claude", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    # Pinned to a tmp dir so no test ever reads or writes the developer's real
    # store.
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    mod = _load_agent()
    projects = tmp_path / "claude" / "projects"
    projects.mkdir(parents=True)
    monkeypatch.setattr(mod, "CLAUDE_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(mod, "PROJECTS", str(projects))
    return mod


@pytest.fixture()
def target(tmp_path):
    """A file target and its workdir — the file's parent, which is the cwd
    everything keys on (`_workdir`)."""
    d = tmp_path / "proj"
    d.mkdir()
    f = d / "index.html"
    f.write_text("<html></html>")
    return str(f), str(d)


def _cli_transcript(agent, workdir, session_id, rows, mtime=None):
    """A transcript where the CLI would have put it: the project dir for
    `workdir`, named by session id."""
    proj = os.path.join(agent.PROJECTS, agent._munge(workdir))
    os.makedirs(proj, exist_ok=True)
    path = os.path.join(proj, session_id + ".jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _said(text, cwd, **extra):
    """A user row as Claude Code writes one."""
    row = {"type": "user", "cwd": cwd, "isSidechain": False,
           "message": {"role": "user", "content": text}}
    row.update(extra)
    return row


# ----------------------------------------------------------------- the list


def test_a_terminal_session_shows_up_in_this_files_past_chats(agent, target):
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [_said("fix the header", workdir)])
    rows = agent._sessions(file)["sessions"]
    assert [(r["id"], r["preview"]) for r in rows] == [("cli-1", "fix the header")]


def test_newest_activity_first(agent, target):
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-old", [_said("oldest", workdir)], mtime=500)
    _cli_transcript(agent, workdir, "cli-mid", [_said("middle", workdir)], mtime=1000)
    _cli_transcript(agent, workdir, "cli-new", [_said("newest", workdir)], mtime=9000)
    rows = agent._sessions(file)["sessions"]
    assert [r["id"] for r in rows] == ["cli-new", "cli-mid", "cli-old"]


def test_a_null_last_used_does_not_crash_the_sort(agent, target, monkeypatch):
    """The sort reads `last_used or created_at or 0`; a row missing both — or
    carrying explicit nulls — must fall to 0, not into a None comparison."""
    file, _ = target
    rows = [{"id": "a", "preview": "p", "last_used": None, "created_at": 42.0},
            {"id": "b", "preview": "p", "last_used": None, "created_at": None}]
    monkeypatch.setattr(agent, "_cli_sessions", lambda f: list(rows))
    assert [r["id"] for r in agent._sessions(file)["sessions"]] == ["a", "b"]


# ------------------------------------------------- what the row is named with


def test_the_title_is_the_first_thing_the_user_actually_said(agent, target):
    """Claude Code writes its SessionStart hook output, mode rows and a
    file-history snapshot ahead of the first prompt — none of which the user
    typed, and the first of which can be a whole skill file."""
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [
        {"type": "mode", "mode": "normal"},
        {"type": "file-history-snapshot", "cwd": workdir, "snapshot": {}},
        _said("the real question", workdir),
        _said("a follow-up nobody titles a row with", workdir),
    ])
    rows = agent._sessions(file)["sessions"]
    assert rows[0]["preview"] == "the real question"


def test_rows_the_user_did_not_write_are_not_titles(agent, target):
    """isMeta is the local-command caveat Claude Code writes FOR the user;
    isSidechain is a subagent's prompt; a "<" opener is a slash command's
    envelope or this template's own app-state block."""
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [
        _said("<local-command-caveat>Caveat: …</local-command-caveat>",
              workdir, isMeta=True),
        _said("go and research this", workdir, isSidechain=True),
        _said("<command-name>/clear</command-name>", workdir),
        _said("what I actually asked", workdir),
    ])
    assert agent._sessions(file)["sessions"][0]["preview"] == "what I actually asked"


def test_block_content_keeps_the_prose_and_drops_the_rest(agent, target):
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [
        {"type": "user", "cwd": workdir, "isSidechain": False, "message": {
            "role": "user", "content": [
                {"type": "image", "source": {}},
                {"type": "text", "text": "look at this screenshot"},
            ]}},
    ])
    assert agent._sessions(file)["sessions"][0]["preview"] == "look at this screenshot"


def test_a_prepended_block_does_not_cost_the_row_its_name(agent, target):
    """The `startswith("<")` skip this replaces was too blunt by exactly one
    case, and it was the case this page CAUSES: `composeOutgoing` puts the
    app-state block and the pane shots in FRONT of what the user typed, so the
    only message in a session could open with "<" and still be the user's own
    words. The row went nameless (and, for the same reason in the server's
    reader, the Tasks list dropped the message entirely)."""
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [
        _said(records.prefixed(records.APP_STATE, records.PANE_SHOT,
                               records.PROSE), workdir),
    ])
    assert agent._sessions(file)["sessions"][0]["preview"] == records.PROSE


def test_an_annotation_send_is_named_with_the_note_the_user_wrote(agent, target):
    """Same shape, tag-less: `formatAnnotations` opens with a sentence, not a
    block, so a "<" test never saw this one at all — the row was called "The user
    annotated 1 element in the left previe…"."""
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [
        _said(records.prefixed(records.APP_STATE, records.ANNOTATION,
                               records.ANNOTATED_ASK), workdir),
    ])
    assert agent._sessions(file)["sessions"][0]["preview"] == records.ANNOTATED_ASK


def test_a_wordless_send_is_skipped_like_any_other_nameless_record(agent, target):
    """The other direction: strip everything and nothing is left, so this row has
    no name and the scan carries on to the record that does."""
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [
        _said(records.prefixed(records.APP_STATE, records.PANE_SHOT), workdir),
        _said("and now make it green", workdir),
    ])
    assert agent._sessions(file)["sessions"][0]["preview"] == "and now make it green"


@pytest.mark.parametrize("text", [
    records.APP_STATE + "\n\n" + records.PROSE,
    records.APP_STATE + "\n\n" + records.ANNOTATION + "\n\n" + records.ANNOTATED_ASK,
    records.APP_STATE + "\n\n" + records.PANE_SHOT,
    records.TASK_NOTIFICATION,
    records.TASK_NOTIFICATION_HALF_WRITTEN,
    records.SLASH_COMMAND,
    records.SLASH_COMMAND_ARGS,
    records.LOCAL_COMMAND_STDOUT,
    records.BASH_ENVELOPE,
    records.ANNOTATION,
    "now ship it <system-reminder>be careful</system-reminder>",
    "<div class=\"card\">Order now</div> why does this render twice?",
    "",
])
def test_the_templates_stripper_and_the_servers_agree_exactly(agent, text):
    """A template may not import fused_render (SPEC PY-15 / D166), so this rule
    has TWO copies — `tasks_store.strip_machinery` and the mirror in agent.py —
    and the whole point of the exercise was that four readers had stopped
    agreeing. Pinned over the corpus rather than by convention, the same way
    D253/D301 pin their other unavoidable duplicates: if one copy learns a tag,
    this fails until the other does."""
    assert agent._strip_machinery(text) == tasks_store.strip_machinery(text)


def test_the_two_copies_carry_the_same_tag_lists(agent):
    """And the lists themselves, so a tag added to one side is caught even if no
    fixture above happens to exercise it."""
    assert agent._MACHINERY_DROP == tasks_store._MACHINERY_DROP
    assert agent._MACHINERY_STRIP == tasks_store._MACHINERY_STRIP


def test_the_strip_list_names_the_tags_the_page_actually_writes(agent, template):
    """The other end of the same duplication, and the one that would fail
    SILENTLY: the STRIP list is only correct because those are the exact tags
    `composeOutgoing` prepends. Rename a wire tag in the page and every reader
    starts titling rows with it again — with nothing failing, because both Python
    copies would still agree with each other.

    D146's rule (a duplicated constant needs a test, not a comment) already pins
    APP_STATE_TAG; this pins the pair as a SET, so a third prepended block cannot
    be added to the page without being classified here too."""
    written = set()
    for line in template.splitlines():
        for const in ("APP_STATE_TAG", "PANE_SHOT_TAG"):
            prefix = "const %s = \"" % const
            if line.strip().startswith(prefix):
                written.add(line.strip()[len(prefix):].split('"')[0])
    assert written == set(agent._MACHINERY_STRIP), (
        "the page prepends %s; the readers strip %s" % (
            sorted(written), sorted(agent._MACHINERY_STRIP)))


def test_the_preview_is_truncated(agent, target):
    """80 chars — a preview is a row title, not the message."""
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [_said("x" * 500, workdir)])
    assert agent._sessions(file)["sessions"][0]["preview"] == "x" * 80


def test_a_session_nobody_spoke_in_is_not_a_past_chat(agent, target):
    """A transcript with no user message has nothing to name a row with and
    nothing to resume into — Claude Code leaves these behind for a session that
    opened and closed."""
    file, workdir = target
    _cli_transcript(agent, workdir, "empty", [{"type": "mode", "cwd": workdir}])
    assert agent._sessions(file)["sessions"] == []


# --------------------------------------------------------------- the scoping


def test_a_munge_collision_is_rejected_on_the_transcripts_own_cwd(agent, target):
    """`_munge` maps every non-alphanumeric char to '-', so `/a/b-c` and `/a-b/c`
    share a project dir and the dirname cannot be decoded back. The transcript
    says where it belongs; the directory name cannot."""
    file, workdir = target
    _cli_transcript(agent, workdir, "mine", [_said("about this folder", workdir)])
    _cli_transcript(agent, workdir, "theirs",
                    [_said("about some other folder", "/somewhere/else")])
    assert [r["id"] for r in agent._sessions(file)["sessions"]] == ["mine"]


def test_a_sibling_folders_sessions_are_not_this_targets(agent, tmp_path, target):
    file, workdir = target
    other = tmp_path / "other"
    other.mkdir()
    _cli_transcript(agent, str(other), "cli-1", [_said("elsewhere", str(other))])
    assert agent._sessions(file)["sessions"] == []


def test_a_directory_target_is_its_own_workdir(agent, target):
    """`_workdir` is the one rule files and folders share — a folder target IS
    the cwd — so the same store answers for both, and the folder chat sees the
    sessions its files' chats see."""
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [_said("about the project", workdir)])
    assert [r["id"] for r in agent._sessions(workdir)["sessions"]] == ["cli-1"]
    assert [r["id"] for r in agent._sessions(file)["sessions"]] == ["cli-1"]


def test_no_store_and_no_project_dir_are_both_just_empty(agent, target, tmp_path):
    """A machine that has never run Claude Code, and a folder that has never
    been chatted about, are answers — not the red traceback overlay."""
    file, _ = target
    assert agent._sessions(file)["sessions"] == []
    agent.PROJECTS = str(tmp_path / "gone")
    assert agent._sessions(file)["sessions"] == []


def test_an_id_that_cannot_round_trip_as_a_path_is_not_offered(agent, target):
    """The id becomes a URL param and then a path again on resume (`_bad_id`
    guards every such join). A filename we would refuse later must not be
    offered as a row now."""
    file, workdir = target
    _cli_transcript(agent, workdir, ".hidden", [_said("dotfile", workdir)])
    _cli_transcript(agent, workdir, "ok", [_said("fine", workdir)])
    assert [r["id"] for r in agent._sessions(file)["sessions"]] == ["ok"]


def test_the_list_is_capped(agent, target):
    file, workdir = target
    for i in range(agent._CLI_SESSION_LIMIT + 12):
        _cli_transcript(agent, workdir, "cli-%03d" % i,
                        [_said("chat %d" % i, workdir)], mtime=1000 + i)
    rows = agent._sessions(file)["sessions"]
    assert len(rows) == agent._CLI_SESSION_LIMIT
    # and it is the NEWEST that survive the cap, not whatever os.listdir said
    assert rows[0]["id"] == "cli-041"


# ------------------------------------------------------------ the cheap read


def test_the_title_read_does_not_parse_the_whole_transcript(agent, target):
    """The read happens on every home-view paint, over transcripts that run to
    megabytes. Only the head is touched, so a huge tail costs nothing — and a
    line straddling the cut is dropped rather than reported as corrupt (we are
    the ones who truncated it)."""
    file, workdir = target
    path = _cli_transcript(agent, workdir, "cli-1", [_said("the question", workdir)])
    with open(path, "a", encoding="utf-8") as f:
        for i in range(4000):
            f.write(json.dumps({"type": "assistant", "cwd": workdir,
                                "message": {"content": "y" * 200}}) + "\n")
    assert os.path.getsize(path) > agent._CLI_HEAD_BYTES
    reads = []
    real_open = open

    def counting_open(p, *a, **kw):
        reads.append((p, kw.get("mode") or (a[0] if a else "r")))
        return real_open(p, *a, **kw)

    agent.open = counting_open
    try:
        rows = agent._sessions(file)["sessions"]
    finally:
        del agent.open
    assert rows[0]["preview"] == "the question"
    assert sum(1 for p, _ in reads if p == path) == 1, "one read per transcript"


def test_recency_comes_from_the_mtime_under_both_timestamp_keys(agent, target):
    """The page's row renderer reads `last_used || created_at` off a row; mtime
    is the only timestamp a transcript offers for free, so it lands on both."""
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [_said("hi", workdir)], mtime=4242)
    row = agent._sessions(file)["sessions"][0]
    assert row["last_used"] == 4242
    assert row["created_at"] == 4242


# ------------------------------------------------------- opening one of these


def test_a_terminal_session_replays_through_the_ordinary_history_action(agent, target):
    """The claim the whole feature rests on: a listed transcript is ALREADY
    where `_history` looks, so the row needs no reader of its own. If this
    breaks, the rows render and clicking one shows an empty conversation."""
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [
        _said("what does this file do", workdir),
        {"type": "assistant", "cwd": workdir, "isSidechain": False,
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "It renders a page."}]}},
    ])
    listed = agent._sessions(file)["sessions"][0]["id"]
    turns = agent.main(action="history", file=file, session_id=listed)["turns"]
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["text"] == "what does this file do"


# ------------------------------------------------------ what the page renders


@pytest.fixture(scope="module")
def template():
    with open(os.path.join("fused_render", "templates", "claude", "template.html"),
              encoding="utf-8") as f:
        return f.read()


def test_one_row_shape_and_one_list(template):
    """One row renderer, one loop over the one list."""
    assert template.count("function addChatRow(") == 1
    # one call site, in the one loop over the list (the second hit on the
    # signature is the definition itself)
    assert template.count("addChatRow(list, s)") == 2


def test_the_row_click_does_not_branch_on_provenance(template):
    """No branch on where a session came from in the open path — that is the
    whole payoff of the page and the CLI sharing a project dir. A special case
    here would be the first sign the identity in agent.py `_cli_sessions` had
    been broken."""
    body = template[template.index("function addChatRow("):]
    body = body[:body.index("\n}")]
    open_fn = body[body.index("const open ="):]
    assert "source" not in open_fn
    assert 'fused.params.set("session_id", s.id)' in open_fn
    assert "loadHistory(s.id)" in open_fn
