"""The `canvas_widget` template's one loader (SPEC §53.3 NW-10): the contract
its text keeps. The page is a browser document, so these read the source."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(
    ROOT, "fused_render", "templates", "canvas_widget", "template.html"
)


def _source() -> str:
    with open(TEMPLATE, encoding="utf-8") as fh:
        return fh.read()


def _handler(source: str, message_type: str) -> str:
    """The branch of the message listener that handles `message_type`."""
    start = source.index('data.type === "' + message_type + '"')
    end = source.find("} else if (data.type ===", start)
    if end < 0:  # the listener's last branch
        end = source.index("function loadFrame()", start)
    return source[start:end]


def test_the_loader_is_in_the_static_markup():
    source = _source()
    body = source[source.index("<body>"):source.index("<script>", source.index("<body>"))]
    assert 'id="loader"' in body and 'role="progressbar"' in body
    assert 'id="bar"' in body
    # The strip holds its height from the first paint.
    assert re.search(r'<div class="strip pending" id="strip">', body)


def test_the_frames_ready_does_not_take_the_loader_off():
    ready = _handler(_source(), "fused-embed-node-ready")
    assert "message.hidden = true" not in ready
    assert "showLoading()" in ready
    # ...unless the frame never says how it stands.
    assert "STATE_WAIT_MS" in ready and "if (!stateSeen) reveal()" in ready
    # What the ready has always done stays with it.
    assert "sendSource(true)" in ready and "pendingPush" in ready


def test_a_state_past_loading_takes_it_off_once():
    source = _source()
    state = _handler(source, "fused-embed-node-state")
    assert 'if (status !== "loading") settled = true;' in state
    assert "if (settled && pageReady) reveal();" in state
    # A later "loading" does not cover the frame again.
    assert "showLoading" not in state
    reveal = source[source.index("function reveal()"):source.index("// The canvas's sync engine")]
    assert "if (revealed) return;" in reveal


def test_the_strip_says_nothing_of_loading_while_the_loader_is_up():
    source = _source()
    state = _handler(source, "fused-embed-node-state")
    assert 'const quiet = status === "loading" && !revealed;' in state
    load = source[source.index("function loadFrame()"):source.index("function armReadyWait")]
    assert "setStatus(STATUS_TEXT.loading)" not in load
    assert "revealed = false" in load and "settled = false" in load


def test_the_bar_only_moves_forward():
    source = _source()
    advance = source[source.index("function advance(to)"):source.index("function showLoading()")]
    assert "if (to <= progress) return;" in advance
    steps = re.search(r"const STEP = \{([^}]*)\}", source).group(1)
    values = [float(v) for v in re.findall(r":\s*([0-9.]+)", steps)]
    assert values == sorted(values) and values[-1] < 0.94


def test_another_message_replaces_the_loader():
    source = _source()
    show = source[source.index("function showMessage(build)"):source.index("function drawProgress()")]
    assert show.index("stopLoading()") < show.index("message.replaceChildren()")
