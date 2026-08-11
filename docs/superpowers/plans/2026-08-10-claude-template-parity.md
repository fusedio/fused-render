# Claude Template Rendering Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the claude chat template's transcript to functional parity with interactive Claude Code: real markdown, a visible tool timeline (diffs, outputs, todos), AskUserQuestion cards, plan-mode approval, thinking blocks, and images.

**Architecture:** The backend (`agent.py`) already re-parses the whole `out.jsonl` on every poll; we extend that parse to emit an ordered `segments` array (text / thinking / tool) alongside the existing `text` field, and the frontend (`template.html`) renders segments instead of one text blob. Questions and plan approval reuse the existing file-mediated permission channel (`perm/*.req.json` / `.res.json`) — no new IPC. Markdown moves from the hand-rolled `renderMd()` to vendored marked + DOMPurify + highlight.js, keeping the mid-stream tolerance the typer depends on.

**Tech Stack:** Python 3 stdlib (agent.py), single-file HTML/JS template, vendored `marked` (v12+), `dompurify` (v3), `highlight.js` (v11, common-languages build), pytest + node-probe tests (existing pattern in `tests/test_claude_permission_bridge.py` and `tests/test_annotate_template.py::_node`).

## Global Constraints

- Worktree: `/Users/akshilthumar/Desktop/fused/fused-render-worktrees/claude-template-view`, branch `agent/20260810-claude-template-parity` (rename current branch or keep; do not touch main checkout).
- **Security invariants from D161 (DECISIONS.md:510) are law:** permission cards never truncate and never drop input keys ("every input key is either rendered in the summary or named in the dump"); anything not the exact string `allow` fails closed; the decision file is a one-way latch; request ids are minted by us, never the CLI's `tool_use_id`.
- No live stdin to the CLI after spawn (fresh-process-per-call, detached). All interaction rides the poll + perm-file channel. Do NOT introduce `canUseTool`/stream-json control responses.
- Model-authored text is hostile: everything rendered from claude output goes through DOMPurify or `textContent`. No `innerHTML` of unsanitized strings.
- Polling stays at 400ms; `poll` must remain a pure re-read (no state mutation besides existing latch/expiry writes).
- Keep `data.text` in the poll payload unchanged (error paths, history preview, token estimate at template.html:4971 depend on it).
- Vendored libs: exact pinned versions, committed under `fused_render/templates/claude/vendor/`, loaded relatively like `tableau/template.html:364` (`loadScript("vendor/x.min.js")`) — the template dir is served per SPEC §0; no CDN at runtime.
- Quality gate before every commit: `pre-commit run --files <changed>` and the test suite for touched areas. Frontend of the shell is NOT touched by this plan (template.html is served raw, no build step).
- Update `DECISIONS.md` (new D-number, one table row per major behavior change) and `docs/CLAUDE-TEMPLATE-POC.md` §6 ("Only text turns render" is superseded) in the task that changes the behavior, not in a docs-only pass.

## Reference map (read these before starting your task)

- `fused_render/templates/claude/agent.py` — spawn line ~1274-1332 (`--disallowed-tools AskUserQuestion,ExitPlanMode` at ~1284); poll parse loop ~1560-1650; `_history()` ~2158-2210; `_decide()` ~1014-1062; `WHOLE_TOOL_GRANTABLE` ~1033.
- `fused_render/templates/claude/template.html` — `renderMd()` 4435-4470; `makeTyper()` 4473-4515; permission summarizer ~4140-4300 (`summarizePermission`, `buildPermCard`, `permChoices`); `pollLoop()` 4957-5030; send/composer ~5048+.
- `fused_render/templates/claude/permission_server.py` — `_handle_approve` ~254, `_permission_result` ~196.
- `tests/test_claude_permission_bridge.py` — stdio JSON-RPC harness, wire-contract pinning style.
- `tests/test_annotate_template.py:71` — `_node()` probe harness for template JS (extract function source, run under node).

---

### Task 1: Real markdown — vendored marked + DOMPurify + highlight.js

**Files:**
- Create: `fused_render/templates/claude/vendor/marked.min.js` (marked v12.x UMD)
- Create: `fused_render/templates/claude/vendor/purify.min.js` (DOMPurify 3.x)
- Create: `fused_render/templates/claude/vendor/highlight.min.js` (highlight.js 11.x common build)
- Create: `fused_render/templates/claude/vendor/hljs.css` (github + github-dark themes merged under `@media (prefers-color-scheme: dark)`)
- Modify: `fused_render/templates/claude/template.html:4434-4470` (replace `renderMd`)
- Test: `tests/test_claude_template_markdown.py`

**Interfaces:**
- Produces: `renderMd(text) -> html string` — same name/signature, all existing call sites (4489, 4496, 5005, and any history render) keep working. Must stay tolerant of an **unclosed fence mid-stream** (typer calls it on partial text every frame).
- Produces: `attachCodeCopy(rootEl)` — walks `rootEl.querySelectorAll("pre")`, adds a copy button; called by the typer's `finish()` and by static renders.

- [ ] **Step 1: Fetch and commit the vendored libs.** Download pinned versions (`marked@12`, `dompurify@3`, `highlight.js@11` common bundle + github/github-dark css) with curl, strip sourcemap comments, commit as-is. Record exact versions in a `vendor/VERSIONS` one-liner file.

- [ ] **Step 2: Write failing node-probe tests** in `tests/test_claude_template_markdown.py`, reusing the `_node` harness pattern from `tests/test_annotate_template.py`. The probe loads the three vendor files plus the extracted `renderMd` source into a JSDOM-free node context (DOMPurify needs a window: probe uses the template's own fallback — see Step 3 — so sanitize assertions run the browser path only where possible; where not, assert the pre-sanitize marked config). Cases:

```python
def test_tables_render_as_html_tables(node_probe):
    html = node_probe.render_md("| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<table" in html and "<td>1</td>" in html

def test_raw_html_is_neutralized(node_probe):
    html = node_probe.render_md('hello <img src=x onerror=alert(1)> world')
    assert "onerror" not in html

def test_unclosed_fence_mid_stream_still_renders(node_probe):
    html = node_probe.render_md("before\n```py\nprint(1)")
    assert "<code" in html and "print(1)" in html

def test_fenced_code_gets_language_class_for_hljs(node_probe):
    html = node_probe.render_md("```python\nx = 1\n```")
    assert 'language-python' in html or 'hljs' in html

def test_links_open_in_new_tab(node_probe):
    html = node_probe.render_md("[x](https://example.com)")
    assert 'target="_blank"' in html and 'rel="noopener' in html
```

- [ ] **Step 3: Replace `renderMd`** at template.html:4435. Load libs at boot next to the template's other script setup (same relative-path style as `tableau/template.html:364`). New implementation:

```js
// renderMd: marked + DOMPurify, tolerant of an unclosed fence mid-stream.
let _md;  // configured once, lazily
function _mdSetup() {
  if (_md) return _md;
  marked.use({
    gfm: true, breaks: true,
    renderer: {
      link({ href, text }) {
        const h = DOMPurify.sanitize(href || "");
        return `<a href="${h}" target="_blank" rel="noopener noreferrer">${text}</a>`;
      },
    },
  });
  _md = (t) => DOMPurify.sanitize(marked.parse(t), {
    FORBID_TAGS: ["style", "form", "input", "iframe"],
    ADD_ATTR: ["target", "rel"],
  });
  return _md;
}
function renderMd(text) {
  // Mid-stream tolerance: an odd number of ``` means a fence is still open —
  // close it so marked doesn't swallow the tail as one giant code block edge case.
  const fences = (text.match(/```/g) || []).length;
  if (fences % 2 === 1) text += "\n```";
  let html = _mdSetup()(text);
  if (window.hljs) {
    const box = document.createElement("div");
    box.innerHTML = html;
    box.querySelectorAll("pre code").forEach((el) => { try { hljs.highlightElement(el); } catch {} });
    html = box.innerHTML;
  }
  return html;
}
function attachCodeCopy(rootEl) {
  rootEl.querySelectorAll("pre").forEach((pre) => {
    if (pre.querySelector(".copybtn")) return;
    const b = document.createElement("button");
    b.className = "copybtn"; b.textContent = "copy"; b.type = "button";
    b.onclick = () => { navigator.clipboard.writeText(pre.querySelector("code")?.textContent || pre.textContent); b.textContent = "copied"; setTimeout(() => (b.textContent = "copy"), 1200); };
    pre.appendChild(b);
  });
}
```

Also: call `attachCodeCopy(bodyEl.parentElement)` inside `makeTyper.finish()`'s resolve path (template.html:4509) and after the static render at 5005. Add `.copybtn` CSS (absolute top-right of `pre`, visible on hover) near the existing `--pre-bg` rules (~line 850). Include `vendor/hljs.css` via a `<link>`; per-frame `highlightElement` cost is bounded by the typer's 36-frame drain — if profiling shows jank on long code, highlight only in `finish()`.

- [ ] **Step 4: Run tests; verify pass.** `pytest tests/test_claude_template_markdown.py -v`.

- [ ] **Step 5: Live check.** Server already running on port 8791 (task `bwmi6efuv`). Open `http://127.0.0.1:8791/embed/<worktree>/README.md?_mode=claude`, send "print a markdown table comparing 3 sorting algorithms, and a python code block". Confirm table + highlighted code + copy button while streaming stays smooth.

- [ ] **Step 6: Commit** `feat(claude): real markdown — vendored marked+DOMPurify+hljs, tables, copy buttons`.

---

### Task 2: Segmented transcript in agent.py (text / thinking / tool / result)

**Files:**
- Modify: `fused_render/templates/claude/agent.py` poll parse loop (~1560-1650) and `_history()` (~2158-2210)
- Test: `tests/test_claude_agent_segments.py`

**Interfaces:**
- Consumes: `out.jsonl` rows already parsed per poll (`stream_event`, `assistant`, `user`, `result`).
- Produces: poll payload gains `segments: list[dict]`, ordered, of:
  - `{"kind": "text", "text": str}` — streamed text (grows in place while composing)
  - `{"kind": "thinking", "text": str}` — accumulated thinking deltas for one block
  - `{"kind": "tool", "id": str, "name": str, "input": dict, "status": "running"|"ok"|"error", "output": str|None, "images": [{"media_type": str, "data": str}]}` — one per finalized `tool_use`, joined to its `tool_result` by `tool_use_id`; `output` capped at 4000 chars with a `"… (+N chars)"` tail (display cap, NOT a permission surface — cards stay untruncated per D161)
- Produces: `_history()` turns gain the same `segments` (rebuilt from the transcript's `assistant`/`user` rows; thinking omitted there if not persisted).
- `data.text` unchanged: still the joined text of all text segments.

- [ ] **Step 1: Write failing pytest** with a fixture `out.jsonl` (hand-built rows, same shapes the CLI emits):

```python
def _rows():
    return [
        {"type": "system", "session_id": "s1"},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Let me edit."}}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Let me edit."},
            {"type": "tool_use", "id": "tu1", "name": "Edit", "input": {"file_path": "/a.py", "old_string": "x=1", "new_string": "x=2"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": [{"type": "text", "text": "ok"}]}]}},
        {"type": "stream_event", "event": {"type": "message_stop"}},
        {"type": "result", "result": "Let me edit.", "session_id": "s1"},
    ]

def test_segments_order_and_tool_join(run_dir_with(_rows())):
    data = poll(run_dir)
    kinds = [s["kind"] for s in data["segments"]]
    assert kinds == ["thinking", "text", "tool"]
    tool = data["segments"][2]
    assert tool["name"] == "Edit" and tool["status"] == "ok" and tool["output"] == "ok"

def test_tool_without_result_yet_is_running(...):  # drop the user row
def test_image_blocks_in_tool_result_are_captured(...):  # content: [{"type":"image","source":{"type":"base64","media_type":"image/png","data":"AAAA"}}]
def test_output_capped_at_4000_chars_with_tail(...)
def test_text_field_still_joins_text_segments(...)
def test_history_turns_carry_segments(...)
```

Import/parse style: follow how `tests/test_claude_permission_bridge.py` loads `agent.py` (module import via file path). Run: expect FAIL (`segments` missing).

- [ ] **Step 2: Implement in the poll loop.** Maintain `segments = []` alongside the existing accumulators. Rules:
  - `thinking_delta` (agent.py:1600): append to a trailing `{"kind":"thinking"}` segment (create if the tail isn't one), keep setting `phase = "thinking"`.
  - `text_delta` (agent.py:1594): append to a trailing `{"kind":"text"}` segment; keep the existing `text_parts` logic untouched.
  - `assistant` rows (agent.py:1587): for each `tool_use` block, append a tool segment (`status:"running"`, `output:None`) keyed by block `id`, and index it in `by_tool_id` dict. (Assistant rows are finalized messages — inputs are complete; do NOT try to assemble streamed `input_json_delta`.)
  - `user` rows (new branch): for each `tool_result` block, look up `by_tool_id[tool_use_id]`; set `status` (`"error"` if `is_error` else `"ok"`), `output` = concatenated text blocks (cap 4000 + tail note), `images` = list of base64 image blocks (cap: skip images > 2 MB base64, note in output instead).
  - Skip synthetic rows the transcript marks `isMeta`/`isSidechain` (same guard `_history()` uses at agent.py:2182).
  - Strip the `mcp__fused_approvals__app_state` tool from segments (it's plumbing, already surfaced as a one-line note by the frontend) — but ONLY that exact name; every other MCP tool renders.
  - Add `data["segments"] = segments` to the poll return next to `text`.
- [ ] **Step 3: Extend `_history()`** — same assistant/user walk over the persisted transcript, producing `segments` per turn (text blocks + tool blocks + results; no thinking if the transcript doesn't carry it). Reuse one shared helper `_segments_from_rows(rows)` so poll and history cannot drift.
- [ ] **Step 4: Run tests; verify pass.** `pytest tests/test_claude_agent_segments.py -v`, plus the existing suite: `pytest tests/test_claude_permission_bridge.py tests/ -k claude -v`.
- [ ] **Step 5: Commit** `feat(claude): poll and history return ordered transcript segments (text/thinking/tool)`.

---

### Task 3: Frontend transcript — render segments (tool chips, diffs, thinking, images)

**Files:**
- Modify: `fused_render/templates/claude/template.html` — `pollLoop()` (4957-5030), new `renderSegments()` + per-tool renderers near `renderMd`, CSS near the perm-card styles, history render path
- Test: `tests/test_claude_template_segments.py` (node probes)

**Interfaces:**
- Consumes: `data.segments` from Task 2; `renderMd`/`attachCodeCopy` from Task 1; the existing Edit `-`/`+` diff formatting from `summarizePermission` (template.html:4165-4169) — extract it into `formatEditDiff(input) -> string` shared by both card and chip.
- Produces: `renderSegments(container, segments, {typer})` — idempotent per poll: segments render once by index, the trailing text segment streams through the existing typer, tool segments update their `status`/`output` in place on later polls.

- [ ] **Step 1: Write failing node probes**: `formatEditDiff` produces `- old` / `+ new` lines; `toolChipSummary({name:"Bash", input:{command:"ls -la"}})` → `"$ ls -la"`; TodoWrite input renders one `☐`/`☑` line per todo; unknown tool falls back to full-JSON body (no key dropped — reuse the leftover-dump helper the perm card uses); a tool with `status:"error"` renders its output. Run: FAIL.
- [ ] **Step 2: Implement renderers.** One collapsed-by-default `<details class="toolchip">` per tool segment:
  - summary line: icon + tool name + one-liner (`Bash` → the command's first line; `Read/Glob/Grep` → path/pattern; `Edit/Write` → file path + `±N` line counts; `Task` → subagent description; `TodoWrite` → `n/m done`; `WebFetch/WebSearch` → url/query; unknown → name).
  - body: `Edit` → `formatEditDiff` in a `<pre class="diff">` with `-`/`+` line coloring (CSS classes, not inline styles); `Write` → file path + content `<pre>`; `Bash` → command `<pre>` + output `<pre>` (output only when present); `TodoWrite` → checklist; generic → pretty-printed input JSON + output. All via `textContent`/escaped nodes — tool inputs/outputs are NOT markdown.
  - `status`: spinner glyph while `"running"`, ✓/✗ after; `images` → `<img src="data:{media_type};base64,{data}">` capped `max-width:100%`.
  - `thinking` segments → `<details class="thinking"><summary>Thought for a moment</summary>` + `renderMd(text)`, collapsed.
  - **Open-by-default exception:** `Edit`/`Write` chips render open — the whole point is seeing code changes without a click.
- [ ] **Step 3: Wire into `pollLoop`.** Replace the single-bubble logic (4978-4985): keep `w` (working line) and the typer, but reply becomes a segment container; each poll calls `renderSegments(container, data.segments, {typer})`; typer targets the last text segment's body; `data.done` path calls `typer.finish` then `attachCodeCopy`. Keep the `end.keepText` error path working on `data.text` as today. Apply the same `renderSegments` to `_history()` turns on boot (find where history turns are rendered — the assistant text path — and pass `turn.segments` when present, falling back to text-only for old transcripts).
- [ ] **Step 4: Run node probes + full claude test files.** Expect PASS.
- [ ] **Step 5: Live check** on :8791 — prompt: "read fused_render/templates/claude/app.py, then add a comment to the top of /tmp/scratch_demo.py after creating it, and keep a todo list while you work". Verify: Read chip, Edit/Write open diffs, TodoWrite checklist ticking, Bash output, thinking block, statuses flip ✓.
- [ ] **Step 6: Update docs** — DECISIONS.md new row ("transcript renders segments; POC §6 'only text turns render' superseded"), POC §6 note. **Commit** `feat(claude): tool timeline in the transcript — diffs, outputs, todos, thinking`.

---

### Task 4: AskUserQuestion — spike the wire, then build the question card

**Files:**
- Modify: `fused_render/templates/claude/agent.py:1284` (`--disallowed-tools`), `_decide()` (~1014)
- Modify: `fused_render/templates/claude/permission_server.py` `_permission_result` (~196)
- Modify: `fused_render/templates/claude/template.html` (`buildPermCard` area ~4219)
- Test: extend `tests/test_claude_permission_bridge.py` + node probe in `tests/test_claude_template_segments.py`

**Interfaces:**
- Consumes: the approve tool already receives `{tool_name: "AskUserQuestion", input: {questions: [{question, header, options: [{label, description}], multiSelect}]}}` once the tool is un-disallowed.
- Produces: `decide` accepts `{decision: "allow", answers: {<question>: [<label>, ...]}}` for AskUserQuestion requests; the server returns whatever the spike proves the CLI honors.

- [ ] **Step 1 — SPIKE (do this before writing any UI):** un-disallow locally and pin the wire. Run headless claude by hand from a scratch dir:

```bash
claude -p "Use the AskUserQuestion tool to ask me whether I prefer A or B, then tell me which I picked." \
  --output-format stream-json --verbose \
  --mcp-config <spike mcp.json pointing at permission_server.py> \
  --permission-prompt-tool mcp__fused_approvals__approve
```

**SPIKE COMPLETE (2026-08-10, CLI 2.1.226) — proven wire, build exactly this:**
- The answer rides `behavior:"allow"` with `updatedInput` = original input **plus a top-level `answers` object keyed by the exact question text**, value = the chosen option's `label`. (`answers`/`annotations`/`response` are declared AskUserQuestion input fields in the CLI.) Verified end-to-end: tool_result reads `Your questions have been answered: "Alpha or Beta?"="Beta"` and the model acts on it.
- Plain `allow` (no answers) → tool_result `The user did not answer the questions.` — useless. `deny`+message delivers text but as `is_error:true` — do not use.
- **Blocking prerequisite:** `permission_server.py::_permission_result` hardcodes `"updatedInput": tool_input` and never reads `updatedInput` from the decision file — the answers path REQUIRES extending the decision-file schema so `_decide()`-written `answers` reach `updatedInput`. Keep fail-closed: an `answers` payload on any tool other than AskUserQuestion is ignored (original input passes through), malformed answers → deny.
- Full transcripts + req.json payloads: see the wire-spike FINDINGS.md (scratchpad, session 99545adb) — copy relevant payloads into the bridge test as fixtures.
- [ ] **Step 2: Write failing bridge test** (`tests/test_claude_permission_bridge.py` style): a parked AskUserQuestion request answered via `decide` with `answers` produces exactly the wire shape the spike proved, and anything else fails closed (deny). Also: AskUserQuestion is never grantable session-wide (`WHOLE_TOOL_GRANTABLE` must not include it) and never auto-approved by `acceptEdits`/`auto` mode... verify mode `auto` still parks it (spike confirms; if `auto` bypasses the prompt tool for it, keep the tool disallowed in `auto` mode spawns and note why).
- [ ] **Step 3: Remove `AskUserQuestion` from `--disallowed-tools`** (keep `ExitPlanMode` until Task 5). Implement the `answers` path in `_decide()` + `_permission_result()` per spike.
- [ ] **Step 4: Question card UI.** In the perm-card builder, branch on `tool_name === "AskUserQuestion"`: render `input.questions` as a card per question — header chip, question text (`textContent`), one button per option (label bold, description small), multiSelect → checkboxes + submit. Clicking sends `decide` with `answers`; card collapses to "You chose: X". Style like a perm card but accent-colored — it is a question, not a permission. Node probe: options render, an option label containing `<script>` stays inert, answer payload shape matches Step 2's test.
- [ ] **Step 5: Live check** on :8791 — "ask me a question with two options before doing anything". Answer it; confirm the model's next text references the choice.
- [ ] **Step 6: Update DECISIONS.md** (supersede the D161 sentence disallowing AskUserQuestion, cite the spike result). **Commit** `feat(claude): AskUserQuestion renders as an answerable question card`.

---

### Task 5: Plan mode — ExitPlanMode approval card + "Plan first" picker option

**Files:**
- Modify: `fused_render/templates/claude/agent.py` (drop `ExitPlanMode` from disallowed; accept `permission: "plan"` → `--permission-mode plan`)
- Modify: `fused_render/templates/claude/template.html` (permission picker ~1348-1350; plan card in the perm-card builder)
- Test: extend bridge test + node probe

- [ ] **Step 1: SPIKE COMPLETE (2026-08-10, CLI 2.1.226):** ExitPlanMode arrives at approve with `input.plan` (markdown). Plain `{"decision":"allow"}` suffices — the CLI exits plan mode itself (emits `system/status permissionMode:"default"`), tool_result reads `User has approved your plan…`, execution proceeds. Optional `setMode` (e.g. `acceptEdits`) in the allow only changes which mode the session lands in — send it when the picker sits on a looser mode. `deny`+message keeps planning. Payloads in wire-spike FINDINGS.md.
- [ ] **Step 2: Failing tests:** bridge — ExitPlanMode allow carries the proven payload, deny keeps fail-closed; unknown `permission` param still falls back to strictest (existing behavior, extend the existing test).
- [ ] **Step 3: Implement.** Add `plan` to the accepted permission-mode values (spawn arg + the picker `<option>` labeled "Plan first"); drop `ExitPlanMode` from `--disallowed-tools`; plan card renders `input.plan` through `renderMd` (it IS markdown) inside a bordered card with **Approve plan** / **Keep planning** buttons; approve → allow per spike; keep planning → deny with message `"Revise the plan — the user wants changes."` plus an optional free-text note (small textarea) appended to that message.
- [ ] **Step 4: Run tests, live check** ("plan a refactor of condition.py, don't touch anything until I approve") — verify plan card, approve, then edits proceed and card history reads correctly.
- [ ] **Step 5: DECISIONS.md row + commit** `feat(claude): plan mode — ExitPlanMode approval card and a Plan-first permission option`.

---

### Task 6: End-to-end verification + PR

- [ ] **Step 1:** Full suite: `pytest tests/ -k "claude" -v` and `pre-commit run --files $(git diff --name-only main...)`.
- [ ] **Step 2:** Playwright pass against :8791 (or `playwright-feature-tester` agent): one conversation exercising md table + code copy + Edit diff + TodoWrite + question card + plan card; screenshot each.
- [ ] **Step 3:** `git push -u origin <branch>`, `gh pr create` (reviewer per repo convention), then loop: CI + bugbot findings → fix → push until green (CLAUDE.md step 6).

---

## Self-review notes

- Spec coverage: complaints 1 (md) → Task 1; 3 (code changes) → Tasks 2+3; 2 (questions) → Task 4; P1 extras (thinking, images, todos, outputs) → Tasks 2+3; plan mode → Task 5. Slash commands/@-mentions: out of scope (recorded).
- The one genuine unknown (question/plan answer wire) is front-loaded as spikes with a hard stop before UI work.
- Type consistency: `segments` schema defined once in Task 2 and consumed verbatim in Task 3; `formatEditDiff`/`attachCodeCopy`/`renderMd` names consistent across tasks.
