"""Chat views: the HTML pages the MODEL writes so the chat can draw a chart,
a diagram or a sortable table inline instead of describing one (D411).

The whole feature is a file and a link. agent.py tells the session it may write
a small self-contained page into `~/.fused-render/claude-views/<target>/` and
link it on a line of its own; the page turns such a link into an iframe over
`/render?path=` — the same URL the left pane frames. There is no chart grammar,
nothing vendored, and no second renderer, which is what a JSON chart spec would
have cost on both sides.

Three agreements hold it together, and each one is a test below rather than a
comment:

* **`Edit(...)`, not `Write(...)`.** Verified by hand against claude 2.1.238:
  `Edit(//<abs>/**)` allows a *Write* tool call under that directory and refuses
  a sibling, while `Write(//<abs>/**)` matches nothing at all. A run that shipped
  the intuitive spelling would raise a permission card for every chart.
* **one spelling of the directory.** The rule on the spawn line, the paragraph
  in the system prompt and the string the page checks a link against have to be
  the same TEXT (the CLI matches rules as text, and the page compares prefixes),
  which on Windows is only true because everything goes through `_wire_path`.
* **the page refuses before it frames.** Shape (renderMd's link renderer), root
  (agent.py's answer) and existence (a stat) — a replayed transcript's links are
  model-authored text read back off disk, so none of the three is optional.

What is NOT covered here, because node cannot: whether the iframe lays out, the
IntersectionObserver mount/unmount cycle, and the same-origin height
measurement. Those need a browser; the tests here pin the argv, the prompt, the
directory arithmetic, the pruner and the page's own accept/refuse rules.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import time

import pytest

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")
TEMPLATE = os.path.join(TEMPLATE_DIR, "template.html")
VENDOR_DIR = os.path.join(TEMPLATE_DIR, "vendor")


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_views_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent():
    return _load("agent")


@pytest.fixture
def html():
    return open(TEMPLATE, encoding="utf-8").read()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A private `~/.fused-render`. `FUSED_RENDER_HOME_DIR` is what the server
    exports (already branch-resolved) and is what appenv reads first, so the
    tests set that one — clearing it and setting only `FUSED_RENDER_HOME` would
    leave a real server's export winning over the fixture."""
    root = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME_DIR", str(root))
    return root


def _node(fn_names, call, html, prelude=""):
    """Run named top-level functions/consts out of template.html under node.

    Same extraction (and the same deliberate copy rather than a shared harness)
    as tests/test_claude_shots.py and tests/test_claude_app_state.py."""
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own view helpers")
    chunks = []
    for name in fn_names:
        start = html.index(name)
        if name.startswith("function") or name.startswith("async function"):
            end = html.index("\n}\n", start) + 3      # closing brace at column 0
            chunks.append(html[start:end])
            continue
        taken = []
        for line in html[start:].split("\n"):
            taken.append(line)
            if line.split("//")[0].rstrip().endswith(";"):
                break
        chunks.append("\n".join(taken))
    script = prelude + "\n" + "\n".join(chunks) + "\n" + call
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _spawn(agent, monkeypatch, target, message="hi"):
    """Run `_start` against a fake Popen and return the argv it built."""
    seen = {}

    class _Proc:
        pid = 4242

    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(agent.subprocess, "Popen",
                        lambda cmd, **kw: (seen.__setitem__("cmd", cmd), _Proc())[1])
    out = agent._start(str(target), message, "", "", "")
    assert "error" not in out, out
    return seen["cmd"]


def _rules(cmd):
    return cmd[cmd.index("--allowed-tools") + 1].split(",")


def _prompt(cmd):
    return cmd[cmd.index("--append-system-prompt") + 1]


# --------------------------------------------------------------- the rule

def test_the_write_pre_allowance_is_spelled_edit_not_write(agent):
    """THE fact the feature hangs on, verified by hand against claude 2.1.238:
    the CLI keeps every file-editing tool in one `Edit(...)` namespace, so
    `Edit(//<abs>/**)` is what lets a *Write* land without a card and
    `Write(//<abs>/**)` is a rule about nothing. Also the double slash, for the
    same reason `_read_rule` documents: a rule path is read as RELATIVE without
    it, so a single-slash rule matches nothing."""
    assert agent._edit_rule("/home/u/.fused-render/claude-views/x") == \
        "Edit(//home/u/.fused-render/claude-views/x/**)"
    # Windows: backslashes become forward ones, the drive letter survives.
    assert agent._edit_rule(r"C:\Users\a\.fused-render\claude-views\x") == \
        "Edit(//C:/Users/a/.fused-render/claude-views/x/**)"


def test_the_spawn_line_pre_allows_exactly_this_targets_views_dir(
        agent, tmp_path, home, monkeypatch):
    """One directory, named the one way `_wire_path` spells it — and it is THIS
    target's directory, not the root: a run must not be pre-approved to rewrite
    the views of every other chat on the machine."""
    agent.RUNS = str(tmp_path / "runs")
    target = tmp_path / "data.csv"
    target.write_text("a\n", encoding="utf-8")
    cmd = _spawn(agent, monkeypatch, target)
    expected = agent._edit_rule(agent._views_dir_path(str(target)))
    assert expected in _rules(cmd), _rules(cmd)
    assert agent._edit_rule(agent._views_root()) not in _rules(cmd)


def test_no_directory_means_no_rule_and_no_promise(
        agent, tmp_path, home, monkeypatch):
    """A directory that could not be created drops the pre-allowance AND the
    prompt paragraph together. Announcing a directory that does not exist would
    card the model's first write and then leave it blamed for trying."""
    agent.RUNS = str(tmp_path / "runs")
    target = tmp_path / "data.csv"
    target.write_text("a\n", encoding="utf-8")
    monkeypatch.setattr(agent, "_ensure_views_dir", lambda file: "")
    cmd = _spawn(agent, monkeypatch, target)
    assert not [r for r in _rules(cmd) if r.startswith("Edit(")], _rules(cmd)
    assert "claude-views" not in _prompt(cmd)


# ---------------------------------------------------------- the directory

def test_views_live_under_the_apps_own_home_not_the_users_project(
        agent, tmp_path, home):
    """Not the project, because a chat is offered on ANY target: a conversation
    about ~/Downloads/report.csv would otherwise mint a dot-directory into
    Downloads. Under `appenv.home_dir()` so a branch checkout gets its own
    namespace instead of overwriting the baseline's views."""
    target = tmp_path / "downloads" / "report.csv"
    target.parent.mkdir()
    target.write_text("a\n", encoding="utf-8")
    path = agent._views_dir_path(str(target))
    assert path.startswith(str(home / "claude-views") + os.sep), path
    assert str(tmp_path / "downloads") not in path


def test_a_file_and_its_folder_share_one_views_directory(agent, tmp_path, home):
    """Keyed through `_workdir` like everything else keyed on the cwd (the
    ~/.claude/projects munge), so the chat about a file and the chat about its
    folder do not drift into two directories holding two `revenue.html`s."""
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "index.html").write_text("<p>hi</p>", encoding="utf-8")
    assert agent._views_dir_path(str(folder / "index.html")) == \
        agent._views_dir_path(str(folder))


def test_two_targets_do_not_share_a_filename(agent, tmp_path, home):
    """What makes the prompt's "reuse the filename when you revise" safe: one
    chat's revenue.html is a different file from another chat's."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    assert agent._views_dir_path(str(a)) != agent._views_dir_path(str(b))


def test_the_page_is_handed_the_root_and_the_dir_in_wire_spelling(
        agent, tmp_path, home):
    """The page needs the ROOT (that is what it checks a replayed transcript's
    links against) as well as this run's directory, and both in the same
    forward-slash spelling the rule uses — the page compares prefixes as text."""
    target = tmp_path / "data.csv"
    target.write_text("a\n", encoding="utf-8")
    out = agent._views_dir(str(target))
    assert out["root"] == agent._wire_path(str(home / "claude-views"))
    assert out["dir"] == agent._wire_path(agent._views_dir_path(str(target)))
    assert "\\" not in out["dir"]
    assert os.path.isdir(out["dir"])          # created, not just named


def test_an_unmakeable_directory_is_an_error_dict_not_a_raise(
        agent, tmp_path, home, monkeypatch):
    """A card that cannot be built degrades to the plain link the reply already
    rendered — never to a traceback over a working chat."""
    monkeypatch.setattr(agent.os, "makedirs",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    target = tmp_path / "data.csv"
    target.write_text("a\n", encoding="utf-8")
    assert "error" in agent._views_dir(str(target))
    assert agent.main(action="views_dir", file="")["error"]


def test_the_pruner_keeps_the_recent_conversation(agent, tmp_path):
    """Generous where the crop pruner is tight, and BOTH caps apply: a view is
    what a replayed transcript draws, so pruning one turns an answer the user
    scrolled back to into a dead link."""
    d = tmp_path / "views"
    d.mkdir()
    old = d / "ancient.html"
    old.write_text("x", encoding="utf-8")
    os.utime(old, (0, time.time() - agent.VIEWS_TTL - 60))
    fresh = [d / ("v%03d.html" % i) for i in range(agent.VIEWS_KEEP + 5)]
    for i, p in enumerate(fresh):
        p.write_text("x", encoding="utf-8")
        os.utime(p, (0, time.time() - (len(fresh) - i)))
    agent._prune_views(str(d))
    assert not old.exists()                                   # TTL
    assert len(os.listdir(d)) == agent.VIEWS_KEEP             # count cap
    assert fresh[-1].exists() and not fresh[0].exists()       # newest survive
    agent._prune_views(str(tmp_path / "gone"))                # no such dir: quiet


# -------------------------------------------------------------- the prompt

def test_every_target_shape_is_told_about_views(agent, tmp_path, home, monkeypatch):
    """File, app folder and ordinary folder alike: a view is a fact about the
    chat WINDOW, not about the target — the same argument the `fused` CLI note
    makes for riding every shape."""
    agent.RUNS = str(tmp_path / "runs")
    app = tmp_path / "app"
    app.mkdir()
    (app / "index.html").write_text(
        '<html><head><meta name="fused-app" /></head><p>hi</p></html>',
        encoding="utf-8")
    plain = tmp_path / "plain"
    plain.mkdir()
    doc = tmp_path / "doc.csv"
    doc.write_text("a\n", encoding="utf-8")
    for target in (doc, app, plain):
        prompt = _prompt(_spawn(agent, monkeypatch, target))
        assert agent._wire_path(agent._views_dir_path(str(target))) in prompt, target


def test_the_note_says_the_five_things_that_each_guard_a_failure(agent):
    """Not prose polish: each clause is a failure mode. The exemption (a file
    target is told to keep its work inside one file, which otherwise argues
    against the whole feature), the line of its own (an inline link is left
    alone), self-containment (a CDN script is a blank card offline), write-then-
    link (the mount stats the file), and the absolute path (`_child` chdirs to
    the script's own directory, so a relative one resolves inside claude-views)."""
    note = agent._views_note("/h/.fused-render/claude-views/t")
    assert "exempt" in note
    assert "line of its own" in note
    assert "no CDN" in note
    assert "write the file before you link it" in note
    assert "ABSOLUTE path" in note
    # and WHEN, not only how — without it the cheapest way to look helpful is a
    # chart on every reply
    assert "not on every reply" in note
    assert agent._views_note("") == ""


# ---------------------------------------------------------------- the page

def test_the_page_accepts_only_absolute_claude_views_html(html):
    """SHAPE, the first of the page's three refusals — judged in marked's link
    renderer because that is the last place the RAW href exists."""
    got = _node(
        ["function _viewPath("],
        "console.log(JSON.stringify(["
        "  _viewPath('/h/.fused-render/claude-views/t/rev.html'),"
        "  _viewPath('C:/Users/a/.fused-render/claude-views/t/rev.html'),"
        "  _viewPath('file:///h/.fused-render/claude-views/t/rev.html'),"
        "  _viewPath('/h/.fused-render/claude-views/t/a%20b.html'),"
        "  _viewPath('claude-views/t/rev.html'),"
        "  _viewPath('/h/.fused-render/claude-views/t/rev.py'),"
        "  _viewPath('/etc/passwd.html'),"
        "  _viewPath('https://example.com/claude-views/x.html'),"
        "  _viewPath(''),"
        "]));", html)
    assert got[:4] == [
        "/h/.fused-render/claude-views/t/rev.html",
        "C:/Users/a/.fused-render/claude-views/t/rev.html",
        "/h/.fused-render/claude-views/t/rev.html",
        "/h/.fused-render/claude-views/t/a b.html",
    ]
    assert got[4:] == ["", "", "", "", ""]


def test_the_page_refuses_a_path_outside_the_root_it_was_given(html):
    """ROOT, the second refusal. A replayed transcript's links are
    model-authored TEXT read back off disk, so "looks like a view" is not enough
    to open a frame with — and `..` is refused rather than resolved, because
    resolving it here would be a second, weaker copy of the check the directory
    itself already is."""
    root = "/h/.fused-render/claude-views"
    got = _node(
        ["const viewSlash", "function insideViewsRoot("],
        "console.log(JSON.stringify(["
        "  insideViewsRoot('%s/t/rev.html', '%s'),"
        "  insideViewsRoot('%s\\\\t\\\\rev.html', '%s'),"
        "  insideViewsRoot('/tmp/claude-views/evil.html', '%s'),"
        "  insideViewsRoot('%s/t/../../../etc/x.html', '%s'),"
        "  insideViewsRoot('%s/t/rev.html', ''),"
        "]));" % (root, root, root, root, root, root, root, root), html)
    assert got == [True, True, False, False, False]


def test_a_view_link_carries_the_path_as_data_and_a_render_href(html, tmp_path):
    """The shape that survives DOMPurify on every platform: the path travels as
    `data-view` (data-* attributes survive its defaults, which is why none of
    this needed the sanitizer config touched) and the href is the ordinary
    same-origin /render URL — which is also what makes the link do something
    sensible wherever no card is built."""
    if not shutil.which("node"):
        pytest.skip("node is needed to drive the template's own markdown")
    src = open(TEMPLATE, encoding="utf-8").read()
    i = src.index("let _md;  // configured once, lazily")
    j = src.index("pre.appendChild(wrap);\n  });\n}", i) + len(
        "pre.appendChild(wrap);\n  });\n}")
    block = src[i:j]
    marked = open(os.path.join(VENDOR_DIR, "marked.min.js"), encoding="utf-8").read()
    script = marked + "\n" + """
global.DOMPurify = { sanitize: (dirty) => dirty };
const DOMPurify = global.DOMPurify;
global.window = {};
global.mountViewCards = () => {};
""" + block + """
console.log(JSON.stringify({
  view: renderMd("[Revenue](/h/.fused-render/claude-views/t/rev.html)"),
  other: renderMd("[docs](https://example.com/a.html)"),
}));
"""
    harness = tmp_path / "h.mjs"
    harness.write_text(script, encoding="utf-8")
    out = subprocess.run(["node", str(harness)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert 'class="viewlink"' in got["view"]
    assert 'data-view="/h/.fused-render/claude-views/t/rev.html"' in got["view"]
    assert "/render?path=%2Fh%2F.fused-render%2Fclaude-views%2Ft%2Frev.html" in got["view"]
    # `_preview=1`, or every card counts as the user opening an app (D301).
    assert "_preview=1" in got["view"]
    # An ordinary link is untouched — same renderer, no viewlink class.
    assert "viewlink" not in got["other"]
    assert 'href="https://example.com/a.html"' in got["other"]


def test_the_mount_rides_the_one_hook_every_finished_reply_calls(html):
    """attachCodeCopy is the "this text is final now" pass at all eight render
    sites (the typer's finish, each static render, the one pass over a restored
    transcript). The mount is called from inside it rather than wired into each
    site, because one missed site is a reply whose chart never appears."""
    body = html[html.index("function attachCodeCopy(rootEl) {"):]
    body = body[:body.index("\n}\n")]
    assert "mountViewCards(rootEl);" in body


def test_a_failed_root_lookup_is_retried_not_cached_forever(html):
    """A single boot-time hiccup must not cost the session every picture in it:
    the promise is cleared on a bad answer so the next finished reply asks
    again."""
    fn = html[html.index("function viewsRootPath() {"):]
    fn = fn[:fn.index("\n}\n")]
    assert fn.count("viewsRootPromise = null") == 2   # the empty answer, and the throw


def test_a_link_whose_file_is_missing_stays_eligible(html):
    """EXISTENCE, the third refusal — and the marker means MOUNTED, not
    ATTEMPTED. attachCodeCopy fires mid-run as each segment settles, so a link
    the model wrote before the file existed has to still be cardable by the
    whole-reply pass at the end of the run."""
    fn = html[html.index("async function mountViewCards(rootEl) {"):]
    fn = fn[:fn.index("\n}\n")]
    stat_at = fn.index("viewExists(path)")
    mark_at = fn.index('setAttribute("data-carded"')
    assert stat_at < mark_at, "the marker must be set only after the file is found"
    assert "continue" in fn[stat_at:mark_at]
