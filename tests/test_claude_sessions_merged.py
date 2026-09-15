"""One list of past chats, from one store (templates/claude/agent.py
`_sessions`).

The list is a scan of the transcripts already sitting in this cwd's
~/.claude/projects dir (D359 removed the per-file sidecar): whether a chat
started in this page or in a terminal, Claude Code wrote its transcript into
the same project dir, so ONE reader answers for both.

The whole reason this needs no resume path of its own is the fact these tests
exist to protect: a session's home is its cwd's project dir, and the chat
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
import re

import pytest

from fused_render import tasks_store
from tests import _machinery_records as records

#: The native chat's wire vocabulary — the TypeScript half of the tag parity
#: `tasks_store._MACHINERY_STRIP` depends on (03 §4e).
_WIRE_TS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "frontend", "src", "apps", "claude", "protocol", "wire.ts")


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
    rows = agent._sessions(workdir)["sessions"]
    assert [(r["id"], r["preview"]) for r in rows] == [("cli-1", "fix the header")]


def test_newest_activity_first(agent, target):
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-old", [_said("oldest", workdir)], mtime=500)
    _cli_transcript(agent, workdir, "cli-mid", [_said("middle", workdir)], mtime=1000)
    _cli_transcript(agent, workdir, "cli-new", [_said("newest", workdir)], mtime=9000)
    rows = agent._sessions(workdir)["sessions"]
    assert [r["id"] for r in rows] == ["cli-new", "cli-mid", "cli-old"]


def test_a_running_chat_sorts_above_a_newer_finished_one(agent, target, monkeypatch):
    """The Tasks list's rule (LIST_ORDER): what is still doing something sits
    above what is over, and time orders within. A transcript is written in one
    burst when the turn ENDS, so a chat mid-turn carries an OLDER mtime than one
    that finished after it started — by time alone it sat under a finished chat
    while wearing "running" (Akshil, 2026-09-11)."""
    _, workdir = target
    rows = [{"id": "done-4m", "preview": "p", "last_used": 100.0, "running": False},
            {"id": "running-old", "preview": "p", "last_used": 40.0, "running": True},
            {"id": "done-1h", "preview": "p", "last_used": 10.0, "running": False},
            {"id": "running-older", "preview": "p", "last_used": 30.0, "running": True}]
    monkeypatch.setattr(agent, "_cli_sessions", lambda f: list(rows))
    assert [r["id"] for r in agent._sessions(workdir)["sessions"]] == \
        ["running-old", "running-older", "done-4m", "done-1h"]


def test_a_null_last_used_does_not_crash_the_sort(agent, target, monkeypatch):
    """The sort reads `last_used or created_at or 0`; a row missing both — or
    carrying explicit nulls — must fall to 0, not into a None comparison."""
    _, workdir = target
    rows = [{"id": "a", "preview": "p", "last_used": None, "created_at": 42.0},
            {"id": "b", "preview": "p", "last_used": None, "created_at": None}]
    monkeypatch.setattr(agent, "_cli_sessions", lambda f: list(rows))
    assert [r["id"] for r in agent._sessions(workdir)["sessions"]] == ["a", "b"]


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
    rows = agent._sessions(workdir)["sessions"]
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
    assert agent._sessions(workdir)["sessions"][0]["preview"] == "what I actually asked"


def test_block_content_keeps_the_prose_and_drops_the_rest(agent, target):
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [
        {"type": "user", "cwd": workdir, "isSidechain": False, "message": {
            "role": "user", "content": [
                {"type": "image", "source": {}},
                {"type": "text", "text": "look at this screenshot"},
            ]}},
    ])
    assert agent._sessions(workdir)["sessions"][0]["preview"] == "look at this screenshot"


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
    assert agent._sessions(workdir)["sessions"][0]["preview"] == records.PROSE


def test_an_annotation_send_is_named_with_the_note_the_user_wrote(agent, target):
    """Same shape, tag-less: `formatAnnotations` opens with a sentence, not a
    block, so a "<" test never saw this one at all — the row was called "The user
    annotated 1 element in the left previe…"."""
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [
        _said(records.prefixed(records.APP_STATE, records.ANNOTATION,
                               records.ANNOTATED_ASK), workdir),
    ])
    assert agent._sessions(workdir)["sessions"][0]["preview"] == records.ANNOTATED_ASK


def test_an_annotation_only_send_is_named_by_the_note_on_the_pin(agent, target):
    """A send needs no prompt to carry annotations — `annPrefillComposer`'s own
    comment says "the comments ARE the content, so an annotation-only send goes
    out with an EMPTY message" — and every reader here was still built on the
    assumption that words arrive as free text.

    So a chat whose only message was a pin lost more than its title: a preview
    of "" makes `_cli_sessions` DROP the session, so the chat disappeared from
    "Recent chats" altogether, and its snapshot runbox could only call it "chat"
    plus a short session id. Both from the same "". The words were in the record
    the whole time, in the pin's `content`."""
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [
        _said(records.prefixed(records.APP_STATE, records.PANE_SHOT,
                               records.ANNOTATION_NOTED), workdir),
    ])
    sessions = agent._sessions(workdir)["sessions"]
    assert [s["id"] for s in sessions] == ["cli-1"]
    assert sessions[0]["preview"] == records.ANNOTATION_NOTE


def test_a_tagged_annotation_send_is_named_by_the_words_in_its_stanzas(agent, target):
    """The same job over TODAY'S block, whose notes are prose rather than a
    `content` key: a chat whose only message was a walkthrough still gets its
    row, named with what the user said. The stanza shape is what makes that
    readable — the heading line, then the words, with machine prose in italics —
    and this is the test that fails if the writer stops writing it that way."""
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [
        _said(records.prefixed(records.APP_STATE, records.PANE_SHOT,
                               records.ANNOTATION_TAGGED), workdir),
    ])
    sessions = agent._sessions(workdir)["sessions"]
    assert [s["id"] for s in sessions] == ["cli-1"]
    assert sessions[0]["preview"] == records.ANNOTATION_TAGGED_NOTES


def test_the_no_badge_caveat_is_not_mistaken_for_the_users_words(agent):
    """The italics rule earning its place: `_no badge on the overview: …_` is
    written by us, and a row named with our own caveat instead of the note under
    it is exactly the class of bug the untagged block's preamble used to cause."""
    assert "no badge" not in tasks_store.ann_notes(records.ANNOTATION_TAGGED)
    assert tasks_store.ann_notes(records.ANNOTATION_TAGGED) == \
        records.ANNOTATION_TAGGED_NOTES


def test_a_one_word_emphasised_note_still_names_its_row(agent):
    """The machine lines are matched EXACTLY, not as "any wholly-italic line":
    a note whose whole text is `_gone_` is the row's only name, and a rule about
    OUR prose eating it leaves the chat nameless (Bugbot, PR #783)."""
    block = ("<annotations>\npreamble\n\n**A** — `<h1>`\n_gone_\n</annotations>")
    assert tasks_store.ann_notes(block) == "_gone_"
    # …while the real placeholder still yields nothing to name a row with.
    empty = ("<annotations>\npreamble\n\n**A** — `<h1>`\n"
             "_(no words for this spot)_\n</annotations>")
    assert tasks_store.ann_notes(empty) == ""


def test_a_multi_line_note_is_flattened_into_one_title(agent):
    """The writer keeps the user's single line breaks (only a BLANK line is
    reserved, as the stanza boundary), so a title has to join them — a row title
    is one line."""
    block = ("<annotations>\npreamble\n\n**A** — `<p>`  · 0:01\n"
             "first thought\nsecond thought\n</annotations>")
    assert tasks_store.ann_notes(block) == "first thought second thought"


def test_free_text_still_wins_over_the_notes_on_the_pins(agent, target):
    """The pins are a FALLBACK. When the user both pinned and typed, the typed
    words are the title — they are the thing they wrote to be read."""
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [
        _said(records.prefixed(records.APP_STATE, records.ANNOTATION_NOTED,
                               records.ANNOTATED_ASK), workdir),
    ])
    assert agent._sessions(workdir)["sessions"][0]["preview"] == \
        records.ANNOTATED_ASK


def test_several_pins_read_in_the_order_the_walkthrough_was_given(agent):
    """`t` order is the order the user clicked, which is the order the notes
    are meant to be read in — so they are joined, not sampled."""
    preamble = records.ANNOTATION_NOTED.split("\n```json\n")[0]
    two = preamble + "\n```json\n" + json.dumps(
        [{"content": "first note"}, {"content": "then this one"}]) + "\n```"
    assert agent._ann_notes(two) == "first note · then this one"


def test_a_pin_with_no_note_names_nothing_and_the_scan_carries_on(agent, target):
    """The empty answer still has its original job: a pin the user placed and
    wrote nothing on is not a title, and neither is a bare screenshot."""
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [
        _said(records.prefixed(records.APP_STATE, records.ANNOTATION), workdir),
        _said("and now make it green", workdir),
    ])
    assert agent._sessions(workdir)["sessions"][0]["preview"] == \
        "and now make it green"


def test_a_wordless_send_is_skipped_like_any_other_nameless_record(agent, target):
    """The other direction: strip everything and nothing is left, so this row has
    no name and the scan carries on to the record that does."""
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [
        _said(records.prefixed(records.APP_STATE, records.PANE_SHOT), workdir),
        _said("and now make it green", workdir),
    ])
    assert agent._sessions(workdir)["sessions"][0]["preview"] == "and now make it green"


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
    records.ANNOTATION_NOTED,
    records.ANNOTATION_TAGGED,
    records.ANNOTATION_TAGGED + "\n\n" + records.ANNOTATED_ASK,
    records.APP_STATE + "\n\n" + records.PANE_SHOT + "\n\n"
    + records.ANNOTATION_TAGGED + "\n\n" + records.ANNOTATED_ASK,
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


@pytest.mark.parametrize("text", [
    records.ANNOTATION_NOTED,
    records.APP_STATE + "\n\n" + records.ANNOTATION_NOTED,
    records.APP_STATE + "\n\n" + records.PANE_SHOT + "\n\n"
    + records.ANNOTATION_NOTED,
    records.ANNOTATION_NOTED + "\n\n" + records.ANNOTATED_ASK,
    records.ANNOTATION,
    records.APP_STATE,
    records.PROSE,
    "The user annotated 1 element…\n```json\nnot json at all\n```",
    "The user annotated 1 element…\n```json\n{\"content\": \"an object\"}\n```",
    "The user annotated 1 element…\n```json\n[\"a bare string\"]\n```",
    # Today's tagged markdown block, in every position the wire puts it — the
    # reader is a stanza parser now, not `json.loads`, so both copies of it have
    # to agree about prose too.
    records.ANNOTATION_TAGGED,
    records.APP_STATE + "\n\n" + records.ANNOTATION_TAGGED,
    records.APP_STATE + "\n\n" + records.PANE_SHOT + "\n\n"
    + records.ANNOTATION_TAGGED,
    records.ANNOTATION_TAGGED + "\n\n" + records.ANNOTATED_ASK,
    # Malformed stanzas: a block with nothing but its preamble, a heading with
    # no words under it, and a stanza that never opened with `**`. Answers on
    # both sides, not exceptions.
    "<annotations>\nThe user annotated 0 things.\n</annotations>",
    "<annotations>\npreamble\n\n**A** — `<b>`  · 0:01\n</annotations>",
    "<annotations>\npreamble\n\nnot a stanza at all\n</annotations>",
    "<annotations>\n</annotations>",
    # A multi-line note (the composer takes a newline on Shift+Enter), which the
    # writer keeps as single breaks — both readers flatten it to one title line.
    "<annotations>\npreamble\n\n**A** — `<p>`  · 0:01\nfirst thought\n"
    "second thought\n</annotations>",
    # A note that is one emphasised word, and the placeholder it must not be
    # confused with — the machine lines are matched exactly (Bugbot, PR #783).
    "<annotations>\npreamble\n\n**A** — `<h1>`\n_gone_\n</annotations>",
    "<annotations>\npreamble\n\n**A** — `<h1>`\n_(no words for this spot)_\n"
    "</annotations>",
    "",
])
def test_the_two_annotation_readers_agree_exactly(agent, text):
    """The THIRD copy of an annotation rule, pinned like the second: a template
    may not import fused_render (D166), so `agent._ann_notes` and
    `tasks_store.ann_notes` are hand-duplicated, and the family of functions
    they belong to exists because four readers had stopped agreeing about
    machinery. The malformed payloads are in the corpus because a title is not
    worth an exception on either side."""
    assert agent._ann_notes(text) == tasks_store.ann_notes(text)


@pytest.mark.parametrize("text", [
    records.APP_STATE,
    records.APP_STATE + "\n\n" + records.PROSE,
    records.APP_STATE + "\n\n" + records.PANE_SHOT,
    records.PROSE,
    records.ANNOTATION,
    # `entry` wins outright over the url beside it.
    '<live-app-state>\nstate\n{"entry":"/a/chosen.html",'
    '"url":"/render?path=%2Fa%2Fother.html"}\n</live-app-state>',
    # No entry: the url answers, and `_file` beats `path` — a templated
    # preview's url names OUR template in `path` and the user's file in `_file`.
    '<live-app-state>\nstate\n'
    '{"url":"/render?path=%2Ftpl%2Fmap.py&_file=%2Fa%2Freal.parquet"}\n'
    '</live-app-state>',
    '<live-app-state>\nstate\n{"url":"/render?path=%2Fa%2Fonly.html"}\n'
    '</live-app-state>',
    # A block that is not LEADING is not machinery — it may be something a
    # human typed, and it must not name their pane.
    'what does this mean? <live-app-state>\n{"entry":"/a/typed.html"}\n'
    '</live-app-state>',
    # Neither key, an unparseable payload, a payload that is not an object, a
    # non-string url: answers, not exceptions.
    '<live-app-state>\nstate\n{"title":"no file here"}\n</live-app-state>',
    '<live-app-state>\nstate\n{not json at all}\n</live-app-state>',
    '<live-app-state>\nstate\n["a bare list"]\n</live-app-state>',
    '<live-app-state>\nstate\n{"url":42}\n</live-app-state>',
    '<live-app-state>\nno object at all\n</live-app-state>',
    "",
])
def test_the_two_pane_readers_agree_exactly(agent, text):
    """The FOURTH copy of a record rule, pinned like the others: a template may
    not import fused_render (D166), so `agent._pane_file` and
    `tasks_store.pane_file` are hand-duplicated. They decide two different
    things off one block — which file "open this task" lands on, and which
    chats a file is offered — and those two answers disagreeing is a row that
    opens somewhere the list said it would not."""
    assert agent._pane_file(text) == tasks_store.pane_file(text)


def test_the_two_copies_carry_the_same_tag_lists(agent):
    """And the lists themselves, so a tag added to one side is caught even if no
    fixture above happens to exercise it."""
    assert agent._MACHINERY_DROP == tasks_store._MACHINERY_DROP
    assert agent._MACHINERY_STRIP == tasks_store._MACHINERY_STRIP


def test_the_strip_list_names_the_tags_the_chat_actually_writes(agent):
    """The other end of the same duplication, and the one that would fail
    SILENTLY: the STRIP list is only correct because those are the exact tags
    `mergeOutgoing` prepends. Rename a wire tag in `apps/claude/protocol/wire.ts`
    and every reader starts titling rows with it again — with nothing failing,
    because both Python copies would still agree with each other.

    D146's rule (a duplicated constant needs a test, not a comment) already pins
    APP_STATE_TAG; this pins the set, so a third prepended block cannot be added
    to the chat without being classified here too. Read as TEXT because a Python
    test cannot import TypeScript — the same pattern as test_trouble_parity.py."""
    src = open(_WIRE_TS, encoding="utf-8").read()
    order = re.search(r"export\s+const\s+BLOCK_ORDER[^=]*=\s*\[(.*?)\]", src, re.S)
    assert order, "BLOCK_ORDER moved out of wire.ts — this test reads nothing"
    written = set()
    for const in re.findall(r"[A-Z_]+", order.group(1)):
        value = re.search(
            r"""(?m)^\s*export\s+const\s+%s\s*(?::[^=]*)?=\s*['"]([^'"]+)['"]"""
            % const, src)
        assert value, f"{const} is in BLOCK_ORDER but has no literal in wire.ts"
        written.add(value.group(1))
    assert written == set(agent._MACHINERY_STRIP), (
        "the chat prepends %s; the readers strip %s" % (
            sorted(written), sorted(agent._MACHINERY_STRIP)))


def test_the_preview_is_truncated(agent, target):
    """80 chars — a preview is a row title, not the message."""
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [_said("x" * 500, workdir)])
    assert agent._sessions(workdir)["sessions"][0]["preview"] == "x" * 80


def test_a_session_nobody_spoke_in_is_not_a_past_chat(agent, target):
    """A transcript with no user message has nothing to name a row with and
    nothing to resume into — Claude Code leaves these behind for a session that
    opened and closed."""
    file, workdir = target
    _cli_transcript(agent, workdir, "empty", [{"type": "mode", "cwd": workdir}])
    assert agent._sessions(workdir)["sessions"] == []


# --------------------------------------------------------------- the scoping


def test_a_munge_collision_is_rejected_on_the_transcripts_own_cwd(agent, target):
    """`_munge` maps every non-alphanumeric char to '-', so `/a/b-c` and `/a-b/c`
    share a project dir and the dirname cannot be decoded back. The transcript
    says where it belongs; the directory name cannot."""
    file, workdir = target
    _cli_transcript(agent, workdir, "mine", [_said("about this folder", workdir)])
    _cli_transcript(agent, workdir, "theirs",
                    [_said("about some other folder", "/somewhere/else")])
    assert [r["id"] for r in agent._sessions(workdir)["sessions"]] == ["mine"]


def test_a_sibling_folders_sessions_are_not_this_targets(agent, tmp_path, target):
    file, workdir = target
    other = tmp_path / "other"
    other.mkdir()
    _cli_transcript(agent, str(other), "cli-1", [_said("elsewhere", str(other))])
    assert agent._sessions(workdir)["sessions"] == []


def test_a_directory_target_is_its_own_workdir(agent, target):
    """`_workdir` is the one rule files and folders share — a folder target IS
    the cwd — so the same store answers for both, and the folder chat sees
    every chat that folder holds."""
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [_said("about the project", workdir)])
    assert [r["id"] for r in agent._sessions(workdir)["sessions"]] == ["cli-1"]
    # A FILE keys on that very same store — the pane filter is the only thing
    # that separates the two lists, not a second directory. If this ever stops
    # holding, resume and history break long before the list does.
    assert agent._workdir(file) == workdir


# ------------------------------------------------------ scoping to ONE file


def _pane_said(text, cwd, pane):
    """A user row as this page writes one: the app-state block naming the pane,
    then the words."""
    block = ('<live-app-state>\nA snapshot of the preview.\n'
             + json.dumps({"entry": pane}) + '\n</live-app-state>')
    return _said(block + "\n\n" + text, cwd)


def test_a_file_is_offered_only_the_chats_opened_on_it(agent, target):
    """The bug this exists for: three files in one folder shared one pile of
    chats, because `_workdir` collapses a file to its parent before the store
    is ever looked at. Selecting file 1 offered a chat that was entirely about
    file 3."""
    file, workdir = target
    sibling = os.path.join(workdir, "other.html")
    _cli_transcript(agent, workdir, "mine",
                    [_pane_said("about this file", workdir, file)], mtime=2000)
    _cli_transcript(agent, workdir, "theirs",
                    [_pane_said("about the other one", workdir, sibling)],
                    mtime=3000)
    assert [r["id"] for r in agent._sessions(file)["sessions"]] == ["mine"]


def test_a_folder_chat_is_not_offered_on_a_file(agent, target):
    """A terminal session, or a chat started on the folder itself, has no pane
    and is not about any one file. It belongs to the folder and is offered
    there — showing it under every file in the folder is the pile itself."""
    file, workdir = target
    _cli_transcript(agent, workdir, "folder-chat",
                    [_said("what is this project", workdir)])
    assert agent._sessions(file)["sessions"] == []
    assert [r["id"] for r in agent._sessions(workdir)["sessions"]] == ["folder-chat"]


def test_a_folder_is_offered_everything_it_holds(agent, target):
    """The folder keeps the whole pile — file chats included. Nothing is lost
    by the filter, it only moves one level down."""
    file, workdir = target
    _cli_transcript(agent, workdir, "on-a-file",
                    [_pane_said("about this file", workdir, file)], mtime=2000)
    _cli_transcript(agent, workdir, "on-the-folder",
                    [_said("about the project", workdir)], mtime=3000)
    assert [r["id"] for r in agent._sessions(workdir)["sessions"]] == [
        "on-the-folder", "on-a-file"]


def test_the_pane_rides_the_row_so_the_page_can_name_it(agent, target):
    """The page shows that file's name on a folder row and opens the row on it
    — neither is possible if the read stops at the server."""
    file, workdir = target
    _cli_transcript(agent, workdir, "on-a-file",
                    [_pane_said("about this file", workdir, file)], mtime=2000)
    _cli_transcript(agent, workdir, "on-the-folder",
                    [_said("about the project", workdir)], mtime=1000)
    panes = {r["id"]: r["pane"] for r in agent._sessions(workdir)["sessions"]}
    assert panes == {"on-a-file": file, "on-the-folder": ""}


def test_the_pane_is_matched_as_a_path_not_as_text(agent, target):
    """The block records the pane's own url and the target arrives from the
    caller; the two can spell one file differently and still be it."""
    file, workdir = target
    spelled = os.path.join(workdir, ".", os.path.basename(file))
    _cli_transcript(agent, workdir, "mine",
                    [_pane_said("about this file", workdir, spelled)])
    assert [r["id"] for r in agent._sessions(file)["sessions"]] == ["mine"]


def test_a_wordless_pane_send_still_leaves_its_pane_behind(agent, target):
    """The pane is read RAW and kept, so a first send that turns out to be
    nothing but machinery does not cost the row the file it was opened on —
    the record two lines down is titled with words and keeps the pane."""
    file, workdir = target
    _cli_transcript(agent, workdir, "mine", [
        _pane_said("", workdir, file),
        _said("what does this do", workdir),
    ])
    rows = agent._sessions(file)["sessions"]
    assert [(r["id"], r["preview"]) for r in rows] == [("mine", "what does this do")]


def test_a_munge_collision_is_still_rejected_under_the_filter(agent, target):
    """The cwd guard runs first and stays first: a colliding transcript that
    happens to name this very file in its block is still not ours."""
    file, workdir = target
    _cli_transcript(agent, workdir, "theirs",
                    [_pane_said("elsewhere", "/somewhere/else", file)])
    assert agent._sessions(file)["sessions"] == []


def test_no_store_and_no_project_dir_are_both_just_empty(agent, target, tmp_path):
    """A machine that has never run Claude Code, and a folder that has never
    been chatted about, are answers — not the red traceback overlay."""
    _, workdir = target
    assert agent._sessions(workdir)["sessions"] == []
    agent.PROJECTS = str(tmp_path / "gone")
    assert agent._sessions(workdir)["sessions"] == []


def test_an_id_that_cannot_round_trip_as_a_path_is_not_offered(agent, target):
    """The id becomes a URL param and then a path again on resume (`_bad_id`
    guards every such join). A filename we would refuse later must not be
    offered as a row now."""
    file, workdir = target
    _cli_transcript(agent, workdir, ".hidden", [_said("dotfile", workdir)])
    _cli_transcript(agent, workdir, "ok", [_said("fine", workdir)])
    assert [r["id"] for r in agent._sessions(workdir)["sessions"]] == ["ok"]


def test_the_list_is_capped(agent, target):
    file, workdir = target
    for i in range(agent._CLI_SESSION_LIMIT + 12):
        _cli_transcript(agent, workdir, "cli-%03d" % i,
                        [_said("chat %d" % i, workdir)], mtime=1000 + i)
    rows = agent._sessions(workdir)["sessions"]
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
        rows = agent._sessions(workdir)["sessions"]
    finally:
        del agent.open
    assert rows[0]["preview"] == "the question"
    assert sum(1 for p, _ in reads if p == path) == 1, "one read per transcript"


def test_recency_comes_from_the_mtime_under_both_timestamp_keys(agent, target):
    """The page's row renderer reads `last_used || created_at` off a row; mtime
    is the only timestamp a transcript offers for free, so it lands on both."""
    file, workdir = target
    _cli_transcript(agent, workdir, "cli-1", [_said("hi", workdir)], mtime=4242)
    row = agent._sessions(workdir)["sessions"][0]
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
    listed = agent._sessions(workdir)["sessions"][0]["id"]
    turns = agent.main(action="history", file=file, session_id=listed)["turns"]
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["text"] == "what does this file do"


# ------------------------------------------------- the registry's running mark

def _registry_row(agent, sid, cwd, status="busy", pid=None, name="p"):
    d = os.path.join(agent.CLAUDE_DIR, "sessions")
    os.makedirs(d, exist_ok=True)
    row = {"pid": os.getpid() if pid is None else pid, "sessionId": sid, "cwd": cwd}
    if status is not None:
        row["status"] = status
    with open(os.path.join(d, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(row, f)


def test_a_terminal_session_this_folder_holds_is_running(agent, target):
    """Claude Code's own registry (~/.claude/sessions/<pid>.json) knows about a
    `claude` started in a terminal, which `_live_sessions` — this app's own
    runs — never will. busy/shell: running. idle/waiting: not. No status at
    all: a headless `claude -p` that is alive, running while its file exists.
    Another folder's session and a dead pid count for nothing."""
    _file, workdir = target
    _registry_row(agent, "s-busy", workdir, "busy", name="a")
    _registry_row(agent, "s-shell", workdir, "shell", name="b")
    _registry_row(agent, "s-idle", workdir, "idle", name="c")
    _registry_row(agent, "s-waiting", workdir, "waiting", name="d")
    _registry_row(agent, "s-headless", workdir, None, name="e")
    _registry_row(agent, "s-elsewhere", workdir + "-other", "busy", name="f")
    _registry_row(agent, "s-dead", workdir, "busy", pid=2 ** 22 + 4321, name="g")
    assert agent._registry_running(workdir) == {"s-busy", "s-shell", "s-headless"}


def test_registry_running_survives_a_missing_or_broken_registry(agent, target):
    _file, workdir = target
    assert agent._registry_running(workdir) == set()  # no sessions dir at all
    d = os.path.join(agent.CLAUDE_DIR, "sessions")
    os.makedirs(d)
    with open(os.path.join(d, "half.json"), "w") as f:
        f.write("{not json")
    with open(os.path.join(d, "list.json"), "w") as f:
        f.write("[]")
    assert agent._registry_running(workdir) == set()


def test_cli_sessions_read_running_off_the_registry_too(agent, target):
    _file, workdir = target  # the FOLDER lists every chat in its project dir
    sid = "11111111-1111-1111-1111-111111111111"
    _cli_transcript(agent, workdir, sid, [_said("hello", workdir)])
    assert [s["running"] for s in agent._cli_sessions(workdir)] == [False]
    _registry_row(agent, sid, workdir, "busy")
    assert [s["running"] for s in agent._cli_sessions(workdir)] == [True]
