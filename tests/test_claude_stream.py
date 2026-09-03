"""What the split view's poll makes of the CLI's stream beyond the reply text.

Two things the page needs and `_poll` used to throw away, because it only ever
looked at `system`, `stream_event` and `result` rows:

  * **Skill invocations.** `out.jsonl` carries a finalized `assistant` row whose
    content holds the whole `tool_use` block, input included. That is the row to
    read: the streamed `content_block_start` for the same call arrives with
    `input: {}` and the argument only turns up as `input_json_delta` fragments
    that would have to be reassembled.
  * **API retries.** The CLI retries an overloaded or rate-limited request on its
    own and says so in `system`/`api_retry` rows. Without them a 529 is
    invisible: the status line sits on "Thinking…" with a frozen token count,
    indistinguishable from a hang.

`_poll` re-reads the whole file every 400 ms, so everything here is about what
one pass over a given file yields — the page owns not rendering the same skill
note twice (that is what the `tool_use` id is for).
"""
import importlib.util
import json
import os
import shutil
import subprocess

import pytest

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")
TEMPLATE = os.path.join(TEMPLATE_DIR, "template.html")


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent():
    return _load("agent")


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "runs" / "run"
    (d / "perm").mkdir(parents=True)
    (d / "appstate").mkdir(parents=True)
    return d


def _poll(agent, run_dir, rows, alive=True):
    """`_poll` over a run whose out.jsonl is exactly `rows`."""
    agent.RUNS = str(run_dir.parent)
    agent._alive = lambda _run_dir: alive
    body = "".join(json.dumps(r) + "\n" if isinstance(r, dict) else r
                   for r in rows)
    (run_dir / "out.jsonl").write_text(body, encoding="utf-8")
    return agent._poll("run")


def _skill_call(skill, id="toolu_1"):
    """The finalized assistant row for one Skill invocation, as the CLI writes
    it (captured from claude 2.1.222)."""
    return {"type": "assistant", "session_id": "s",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": id, "name": "Skill",
                 "input": {"skill": skill}, "caller": {"type": "direct"}}]}}


def _retry(attempt, max_retries=10, status=529, error="overloaded", delay=569):
    return {"type": "system", "subtype": "api_retry", "attempt": attempt,
            "max_retries": max_retries, "retry_delay_ms": delay,
            "error_status": status, "error": error, "session_id": "s"}


def _text(chunk):
    return {"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": chunk}}}


# ------------------------------------------------------------------ skills

def test_a_skill_invocation_is_reported_with_its_name(agent, run_dir):
    data = _poll(agent, run_dir, [_skill_call("fused-render-usage")])
    assert data["skills"] == [{"id": "toolu_1", "skill": "fused-render-usage"}]


def test_several_skills_keep_the_order_they_were_called_in(agent, run_dir):
    """The page renders these as log rows, so order is the reading order."""
    data = _poll(agent, run_dir, [
        _skill_call("fused-render-usage", id="toolu_1"),
        _text("thinking about it"),
        _skill_call("fused-render-authoring", id="toolu_2"),
    ])
    assert [s["skill"] for s in data["skills"]] == [
        "fused-render-usage", "fused-render-authoring"]


def test_a_plugin_namespaced_skill_keeps_its_namespace(agent, run_dir):
    """`fused-render:...` is how a skill loaded from our --plugin-dir plugin is
    named, and the namespace is the interesting half — it says the skill came
    from fused-render rather than from the user's own ~/.claude."""
    data = _poll(agent, run_dir,
                 [_skill_call("fused-render:fused-render-authoring")])
    assert data["skills"][0]["skill"] == "fused-render:fused-render-authoring"


def test_other_tools_are_not_reported_as_skills(agent, run_dir):
    """Only the Skill tool. A log row per Bash call would be a different
    feature and a much noisier one."""
    rows = [{"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_9", "name": "Bash",
         "input": {"command": "ls"}}]}}]
    assert _poll(agent, run_dir, rows)["skills"] == []


def test_a_skill_call_with_no_name_is_dropped_rather_than_reported_blank(
        agent, run_dir):
    """A row we cannot read the skill out of has nothing to say to the user, and
    an empty note row would be worse than no note."""
    rows = [{"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_9", "name": "Skill", "input": {}}]}}]
    assert _poll(agent, run_dir, rows)["skills"] == []


def test_the_streamed_half_of_a_skill_call_is_not_double_counted(agent, run_dir):
    """The same call appears twice in the file — once as the streamed
    content_block_start (input still empty) and once finalized. Reading both
    would report every skill twice."""
    rows = [
        {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_1",
                              "name": "Skill", "input": {}}}},
        _skill_call("fused-render-usage", id="toolu_1"),
    ]
    assert _poll(agent, run_dir, rows)["skills"] == [
        {"id": "toolu_1", "skill": "fused-render-usage"}]


def test_a_malformed_assistant_row_is_skipped_not_fatal(agent, run_dir):
    """Same tolerance the rest of the loop has: a row we cannot read must not
    cost the caller the reply text that came with it."""
    rows = [
        {"type": "assistant", "message": {"content": "not a list"}},
        {"type": "assistant", "message": {"content": ["not a dict"]}},
        {"type": "assistant"},
        _skill_call("fused-render-usage"),
        _text("hello"),
    ]
    data = _poll(agent, run_dir, rows)
    assert data["skills"] == [{"id": "toolu_1", "skill": "fused-render-usage"}]
    assert data["text"] == "hello"


def test_a_half_written_last_line_is_still_tolerated(agent, run_dir):
    """The file is read while the CLI writes it, so the final line is routinely
    a fragment. Pinned because the skill/retry parsing added new json handling
    to a loop whose tolerance is load-bearing."""
    rows = [_skill_call("fused-render-usage"), _text("hi"),
            '{"type": "assist']
    data = _poll(agent, run_dir, rows)
    assert data["skills"] == [{"id": "toolu_1", "skill": "fused-render-usage"}]
    assert data["text"] == "hi"


# ------------------------------------------------------------------ retries

def test_a_retry_is_surfaced_while_it_is_happening(agent, run_dir):
    data = _poll(agent, run_dir, [_retry(1)])
    assert data["retry"] == {"attempt": 1, "max_retries": 10, "delay_ms": 569,
                             "status": 529, "error": "overloaded"}
    assert data["phase"] == "retrying"


def test_the_latest_attempt_is_the_one_reported(agent, run_dir):
    """Ten attempts are ten rows; the page wants "3 of 10", not a list."""
    data = _poll(agent, run_dir, [_retry(1), _retry(2), _retry(3)])
    assert data["retry"]["attempt"] == 3
    assert data["retry_total"] == 3


def test_max_retries_is_read_from_the_row(agent, run_dir):
    """It happens to be 10 today. Hardcoding it would silently misreport
    "3/10" the day the CLI changes its budget."""
    data = _poll(agent, run_dir, [_retry(1, max_retries=4)])
    assert data["retry"]["max_retries"] == 4


def test_content_arriving_after_a_retry_ends_the_retry(agent, run_dir):
    """THE case this has to get right. The retry state is transient: once the
    request goes through, the page must stop saying "retrying" — a banner left
    up for the rest of the turn would be a lie for far longer than it was true.
    Rows are in file order, so text after a retry means the retry is over."""
    data = _poll(agent, run_dir, [_retry(1), _retry(2), _text("it worked")])
    assert data["retry"] is None
    assert data["phase"] != "retrying"
    assert data["text"] == "it worked"


def test_a_retry_after_earlier_content_is_still_live(agent, run_dir):
    """Retries happen mid-turn too — between an assistant message and the next
    request. Earlier text must not make a later retry look finished."""
    data = _poll(agent, run_dir, [_text("first part"), _retry(1)])
    assert data["retry"]["attempt"] == 1
    assert data["phase"] == "retrying"


def test_a_finalized_assistant_row_also_ends_the_retry(agent, run_dir):
    data = _poll(agent, run_dir, [_retry(1), _skill_call("fused-render-usage")])
    assert data["retry"] is None


def test_how_many_retries_happened_survives_the_retry_going_away(agent, run_dir):
    """The live state is cleared on success, but "this turn was retried 4 times"
    is what makes a final failure explainable, so the tally is kept."""
    data = _poll(agent, run_dir, [_retry(1), _retry(2), _retry(3), _retry(4),
                                  _text("ok")])
    assert data["retry"] is None
    assert data["retry_total"] == 4
    assert data["retry_status"] == 529


def test_a_rate_limit_is_reported_as_itself_not_as_an_overload(agent, run_dir):
    """429 and 529 are different news for the user — one is us being throttled,
    the other is the API being swamped — so the status travels rather than a
    baked-in message."""
    data = _poll(agent, run_dir, [_retry(1, status=429, error="rate_limited")])
    assert data["retry"]["status"] == 429
    assert data["retry"]["error"] == "rate_limited"


def test_a_run_with_no_retries_says_so_plainly(agent, run_dir):
    data = _poll(agent, run_dir, [_text("hi")])
    assert data["retry"] is None
    assert data["retry_total"] == 0
    assert data["retry_status"] == 0


def test_a_malformed_retry_row_is_skipped_not_fatal(agent, run_dir):
    rows = [{"type": "system", "subtype": "api_retry", "attempt": "lots"},
            _text("hi")]
    data = _poll(agent, run_dir, rows)
    assert data["text"] == "hi"


# ------------------------------------------------ giving up, on screen

def _failed(text="API Error: 529 Overloaded"):
    return {"type": "result", "session_id": "s", "is_error": True,
            "result": text}


def test_giving_up_after_retries_explains_itself(agent, run_dir):
    """The raw "API Error: 529 Overloaded" reads as a bug in this app. What the
    user needs to know is that the API was busy, that it was already retried,
    and that waiting is the fix."""
    data = _poll(agent, run_dir, [_retry(1), _retry(2), _failed()], alive=False)
    assert "overloaded" in data["error"]
    assert "2 retries" in data["error"]
    assert "again in a moment" in data["error"]


def test_the_original_error_text_is_not_thrown_away(agent, run_dir):
    """It is the only string a bug report can be matched on."""
    data = _poll(agent, run_dir, [_retry(1), _failed()], alive=False)
    assert "API Error: 529 Overloaded" in data["error"]


def test_one_retry_is_not_described_as_one_retries(agent, run_dir):
    data = _poll(agent, run_dir, [_retry(1), _failed()], alive=False)
    assert "1 retry " in data["error"]


def test_a_throttled_run_is_not_described_as_overloaded(agent, run_dir):
    data = _poll(agent, run_dir,
                 [_retry(1, status=429, error="rate_limited"), _failed()],
                 alive=False)
    assert "rate limited" in data["error"]
    assert "overloaded" not in data["error"]


def test_an_ordinary_failure_is_left_exactly_as_it_was(agent, run_dir):
    """No retries means no rewrite: this must not put an API story in front of
    every unrelated error."""
    data = _poll(agent, run_dir, [_failed("no such tool: Frobnicate")],
                 alive=False)
    assert data["error"] == "no such tool: Frobnicate"


def test_a_recovered_retry_does_not_explain_a_later_unrelated_failure(
        agent, run_dir):
    """The bug this replaced: the rewrite keyed off the run's retry TALLY, which
    survives a mid-turn retry that succeeded. A crash, a tool error or an auth
    error arriving later was then dressed up as an API overload and the real
    cause was buried. Only a retry still in flight at the end may explain it."""
    rows = [_retry(1), _retry(2), _text("recovered fine"),
            _failed("Edit failed: string not found in file")]
    data = _poll(agent, run_dir, rows, alive=False)
    assert data["error"] == "Edit failed: string not found in file"
    assert "overloaded" not in data["error"]
    # the tally still rides along for the page — it just cannot decide the wording
    assert data["retry_total"] == 2


def test_a_run_killed_mid_backoff_still_explains_itself(agent, run_dir):
    """No `result` row to move the live retry into `gave_up`, so the abnormal-exit
    path has to read the still-live one."""
    data = _poll(agent, run_dir, [_retry(3)], alive=False)
    assert "overloaded" in data["error"]
    assert "3 retries" in data["error"]


def test_a_clean_run_that_was_retried_reports_no_error_at_all(agent, run_dir):
    """Retries that SUCCEEDED are not a failure. Rewriting on `retry_total`
    alone would invent an error for a turn that worked."""
    rows = [_retry(1), _retry(2), _text("all good"),
            {"type": "result", "session_id": "s", "result": "all good"}]
    data = _poll(agent, run_dir, rows, alive=False)
    assert data["error"] == ""
    assert data["text"] == "all good"


def test_a_retry_row_still_carries_the_session_id(agent, run_dir):
    """api_retry rows are `system` rows, and `system` rows are where the session
    id comes from. Adding a subtype branch must not skip that."""
    rows = [{"type": "system", "subtype": "api_retry", "attempt": 1,
             "max_retries": 10, "retry_delay_ms": 1, "error_status": 529,
             "error": "overloaded", "session_id": "sess-abc"}]
    assert _poll(agent, run_dir, rows)["session_id"] == "sess-abc"


# ------------------------------------------------ the page's own wording, in node

@pytest.fixture
def html():
    return open(TEMPLATE, encoding="utf-8").read()


def _retry_verb(source, retry):
    """Run the page's real `retryVerb` over one live retry payload. Pure, so it
    lifts out on its own — same siting for the node guard as the sibling
    modules'."""
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own status-line wording")
    start = source.index("function retryVerb(")
    fn = source[start:source.index("\nfunction addWorking(", start)]
    script = fn + "\nconsole.log(retryVerb(%s));" % json.dumps(retry)
    # `encoding="utf-8"` is not decorative: `text=True` alone decodes the
    # child's stdout with locale.getpreferredencoding(False), and node always
    # writes its UTF-8 source glyphs (the em dash in "Overloaded — retrying")
    # as UTF-8 bytes regardless of platform. On Windows that locale default is
    # commonly cp1252, which decodes those bytes into mojibake without ever
    # raising — a silent corruption, not a crash, so it slipped past every
    # POSIX run where the locale default already happens to be UTF-8.
    out = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_an_overload_says_overloaded_and_counts_the_attempts(html):
    assert _retry_verb(html, {"attempt": 3, "max_retries": 10, "status": 529}) \
        == "Claude's servers are busy — retrying (3/10)"


def test_a_throttle_is_worded_as_a_throttle(html):
    """Different news, different place to look: 529 clears on its own, 429 is
    about this account's usage."""
    assert _retry_verb(html, {"attempt": 1, "max_retries": 10, "status": 429}) \
        == "Claude is busy — retrying (1/10)"


def test_an_unrecognised_status_still_says_something_true(html):
    verb = _retry_verb(html, {"attempt": 2, "max_retries": 10, "status": 503})
    assert "retrying (2/10)" in verb
    assert "Overloaded" not in verb and "Rate limited" not in verb


def test_no_budget_means_no_invented_denominator(html):
    """`max_retries` is the CLI's to report. "(2/0)" would be worse than "(2)"."""
    assert _retry_verb(html, {"attempt": 2, "max_retries": 0, "status": 529}) \
        == "Claude's servers are busy — retrying (2)"


# ------------------------------------------------------------ the page's wiring

def test_the_prompt_does_not_promise_a_path_the_fallback_may_not_send(agent, tmp_path):
    """appStateFile falls back to an inline `dom` when the write fails, so a
    prompt that states the outline is always at a path would send the model
    hunting for a `dom_path` that was never sent.

    This is the APP FOLDER shape — the only one that mentions `dom_path` at all,
    and the only one that ever did. `_split_system_prompt` takes the target and
    the PANE FLAG now: an ordinary folder gets a different prompt, and the flag is
    resolved once in `_start` rather than re-derived here (a second read is a
    second answer)."""
    app = tmp_path / "proj"
    app.mkdir()
    (app / "index.html").write_text("<p>hi</p>", encoding="utf-8")
    prompt = agent._split_system_prompt(str(app), True)
    assert "inline" in prompt and "dom_path" in prompt


def test_the_page_hands_the_live_retry_to_the_status_line(html):
    """Without the third argument the status line can only say "Thinking…",
    which is the bug this feature exists to fix."""
    assert "w.setStats(tokens, data.phase || \"thinking\", data.retry, data.activity)" in html


def test_the_page_announces_the_skills_the_poll_reports(html):
    assert "noteSkills(data.skills, w)" in html


def test_a_skill_row_is_keyed_on_the_tool_use_id(html):
    """Poll replays every call each tick, so the id is the only thing standing
    between one invocation and a row per 400 ms."""
    start = html.index("function noteSkills(")
    body = html[start:html.index("\nasync function answerAppState(", start)]
    assert "notedSkills.has(call.id)" in body
    assert "notedSkills.add(call.id)" in body


def test_a_skill_name_never_reaches_the_log_as_markup(html):
    """Model-authored text. `addNote` sets textContent; this pins that the skill
    row goes through it rather than growing an innerHTML of its own."""
    start = html.index("function noteSkills(")
    body = html[start:html.index("\nasync function answerAppState(", start)]
    assert "addNote(" in body
    assert "innerHTML" not in body


# ------------------------------------------------ login / plan-limit errors

def test_a_logged_out_claude_explains_the_login_fix(agent, run_dir):
    """The raw CLI text ("Invalid API key · Please run /login") names a fix
    that only makes sense INSIDE claude; the user is looking at fused-render.
    Spell out where to run it and keep the original for bug reports."""
    data = _poll(agent, run_dir,
                 [_failed("Invalid API key · Please run /login")], alive=False)
    assert "isn't logged in" in data["error"]
    assert "/login" in data["error"]
    assert "render.fused.io/#troubleshooting-login" in data["error"]
    assert "Invalid API key" in data["error"]


def test_an_expired_oauth_token_reads_as_a_login_problem(agent, run_dir):
    data = _poll(agent, run_dir,
                 [_failed("OAuth token has expired. Please obtain a new token "
                          "or refresh your existing token.")], alive=False)
    assert "isn't logged in" in data["error"]


def test_a_login_error_on_stderr_is_rewritten_too(agent, run_dir):
    """A logged-out claude sometimes dies without a `result` row; the abnormal
    exit path reads err.log and must tell the same story."""
    (run_dir / "out.jsonl").write_text("", encoding="utf-8")
    (run_dir / "err.log").write_text("Invalid API key · Please run /login\n",
                                     encoding="utf-8")
    agent.RUNS = str(run_dir.parent)
    agent._alive = lambda _run_dir: False
    data = agent._poll("run")
    assert "isn't logged in" in data["error"]


def test_a_plan_limit_error_explains_the_wait(agent, run_dir):
    data = _poll(agent, run_dir,
                 [_failed("Claude AI usage limit reached|1755264000")],
                 alive=False)
    assert "usage limit" in data["error"]
    assert "render.fused.io/#troubleshooting-limit" in data["error"]
    assert "Claude AI usage limit reached" in data["error"]


def test_an_unrelated_error_gains_no_login_story(agent, run_dir):
    data = _poll(agent, run_dir, [_failed("Edit failed: string not found")],
                 alive=False)
    assert data["error"] == "Edit failed: string not found"


def test_a_run_that_died_mid_retry_keeps_the_overload_story(agent, run_dir):
    """A 429 whose text mentions a usage limit, arriving with a retry still in
    flight, is API-health news: the overload rewrite wins and _account_error
    must not re-match inside its parenthesized original."""
    rows = [_retry(2, status=429, error="rate_limited"),
            _failed("Claude AI usage limit reached|1755264000")]
    data = _poll(agent, run_dir, rows, alive=False)
    assert "rate limited" in data["error"]
    assert "Your Claude plan" not in data["error"]


# --------------------------------- when a turn ends but the run does not (D415)
#
# One claude process can run several turns: a turn that started a background
# shell is woken by the harness when the command finishes, and the reply to that
# wake is written after the `result` that closed the first turn. `done` used to
# latch on that first `result`, so the woken turn was reported as a finished one
# and the page showed nothing at all until the next reload.

_DONE = {"type": "result", "session_id": "s", "result": "there"}


def test_a_result_with_nothing_after_it_ends_the_turn(agent, run_dir):
    assert _poll(agent, run_dir, [_text("there"), _DONE])["done"] is True


def test_a_result_the_run_has_spoken_past_is_not_the_end(agent, run_dir):
    """The wake's own rows — hooks, `init`, the reply — all reopen the turn."""
    woken = _poll(agent, run_dir, [
        _text("there"), _DONE,
        {"type": "system", "subtype": "task_notification", "status": "completed",
         "summary": "Background command \"pytest -q\" completed"},
        {"type": "system", "subtype": "init", "session_id": "s"},
        _text("the tests passed"),
    ])
    assert woken["done"] is False
    assert woken["error"] == ""
    assert woken["text"].endswith("the tests passed")


def test_the_second_turns_own_result_ends_the_run_again(agent, run_dir):
    data = _poll(agent, run_dir, [
        _text("there"), _DONE,
        {"type": "system", "subtype": "init", "session_id": "s"},
        _text("the tests passed"),
        {"type": "result", "session_id": "s", "result": "the tests passed"},
    ])
    assert data["done"] is True


def test_a_dead_process_ends_the_run_whatever_the_rows_say(agent, run_dir):
    """The wake that never came: the process is gone, so nothing more can be
    written and the page must not sit on a working line for ever."""
    data = _poll(agent, run_dir, [
        _text("there"), _DONE,
        {"type": "system", "subtype": "init", "session_id": "s"},
    ], alive=False)
    assert data["done"] is True
    # ...and a run that DID finish a turn is not reported as a crash, however
    # abruptly its process went away afterwards.
    assert data["error"] == ""


def test_a_live_run_with_no_result_yet_is_still_running(agent, run_dir):
    assert _poll(agent, run_dir, [_text("thinking about it")])["done"] is False


def test_a_delta_less_turn_shows_its_text_before_the_process_exits(agent, run_dir):
    """The `result` fallback is about the row that carries the text, not about
    the process: an older CLI's reply must not stay blank while the run is
    awake between turns."""
    data = _poll(agent, run_dir, [{"type": "result", "session_id": "s",
                                   "result": "hello"}])
    assert data["text"] == "hello"


# ------------------------------------------------ which models the picker offers

def test_the_picker_offers_a_pinned_fable_51_beside_the_floating_alias(html):
    """`claude --model` takes two shapes — a moving alias ("fable", whatever
    Fable is today) and a pinned full id ("claude-fable-5-1", that exact model)
    — and the picker has to offer both.

    The alias alone is what a long chat cannot be held still on: it advances
    under the user the day the CLI's default does, mid-project, with nothing on
    screen saying so. The pinned entry is the fix, and it leads the list because
    someone opening this menu is usually after a specific model. Pinned first,
    then the alias, is the ORDER asserted here — the value list is what
    `curModel()` validates against, so a value dropped from it is a URL param
    that silently falls back to the default and a pill that blanks."""
    line = next(ln for ln in html.splitlines() if ln.startswith("const MODELS ="))
    assert line == ('const MODELS = ["claude-fable-5-1", "fable", "opus", '
                    '"sonnet", "haiku"];')
    # …and the raw id is never what the user reads. The pill and the menu take
    # their words from MODEL_LABELS, which is only needed for the pinned entry
    # but is written out in full so no value can ever print itself for want of
    # a line there.
    assert '"claude-fable-5-1": "Fable 5.1",' in html
    assert 'fillSelect(el, MODELS, "Model", MODEL_LABELS)' in html


def test_a_pinned_model_round_trips_from_a_transcript_to_the_picker(agent):
    """Detection reads the model off the project's newest transcripts so the
    pill can preselect what the user is actually working in. The page then
    VALIDATES that answer against its own MODELS list and drops anything else —
    so a spelling `_short_model` cannot produce is a preselect that never
    happens, silently.

    The old loop collapsed every id onto a family name, and every pinned id
    contains its own family name: "claude-fable-5-1-20260401" came back as
    "fable", so a chat running the pinned model preselected the floating alias
    and the user's next turn moved off it."""
    assert agent._short_model("claude-fable-5-1-20260401") == "claude-fable-5-1"
    assert agent._short_model("claude-fable-5.1") == "claude-fable-5-1"
    # Unpinned Fable is still the alias — that IS the value the picker offers
    # for it, and the one the user would have chosen.
    assert agent._short_model("claude-fable-5-20260101") == "fable"
    # A longer version number is a DIFFERENT model, not this one. A prefix match
    # would quietly stop being true the day 5.10 ships, which is the exact thing
    # a pinned entry exists to prevent.
    assert agent._short_model("claude-fable-5-10") == "fable"
    # Everything else is untouched: aliases, full ids, already-short names.
    assert agent._short_model("opusplan") == "opus"
    assert agent._short_model("claude-sonnet-4-5-20250929") == "sonnet"
    assert agent._short_model("haiku") == "haiku"
    assert agent._short_model("") == ""
    assert agent._short_model("gpt-4") == ""


# ---------------------------------------- two rules about what the page may do
#                                          to a run the user is watching stream
#
# Both pinned as strings, right here beside the stream the page renders, because
# both are one edit away from coming back and neither has a symptom a stream test
# would notice: the first cost the user the whole turn, the second cost them
# sight of it.


def test_no_key_binding_stops_a_run(html):
    """Escape used to kill a live turn whenever nothing else claimed the key —
    so a reader who reached for it out of habit lost the run to a keystroke never
    aimed at it (Akshil, 2026-09-03). Work in progress is not something a bare
    keypress may throw away. The behavioural half is in
    tests/test_claude_app_state.py; this is the pin that survives that file being
    refactored."""
    assert "stop-run" not in html
    handler = html[html.index("function onEscape(e) {"):]
    assert "stopRun" not in handler[:handler.index("\n}\n")]
    # the stop button itself is untouched — there IS still a way to stop a turn
    assert "async function stopRun() {" in html
    assert "if (activeRun) { stopRun(); return; }" in html


def test_the_transcript_follows_growth_it_was_not_told_about(html):
    """A live turn reaches its final height long after the append that scrolled:
    tool cards expand when their output lands, code blocks grow under the
    highlighter, pictures and artifact iframes take up room only once they load,
    the bubble re-renders as markdown at message end. So growth is OBSERVED
    rather than announced — one ResizeObserver on the scrollport's two children,
    answering with the same follow flag every write site uses. Behaviour in
    tests/test_claude_scroll_follow.py."""
    assert "const followGrowth = new ResizeObserver(followBottom);" in html
    assert ('for (const el of [log, document.getElementById("queue")]) '
            "followGrowth.observe(el);") in html
    # `load` does not bubble, so the picture/iframe backstop must be in capture
    assert 'logwrap.addEventListener("load", followBottom, true);' in html
