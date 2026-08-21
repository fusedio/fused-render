"""Gate for the `workflow` template — the MCP workflow canvas (SPEC §44 / D401).

`main(path)` decides whether a path is a **workflow document**: a
`<name>.workflow.json` file holding a chain of MCP tool calls the user wired
together on a canvas. The `mcp` mode beside this one *declares* an app's tools;
this one *composes* tools from several apps into one plan and hands that plan to
Claude to execute.

The registry key does most of the gating already. `.workflow.json` is a COMPOUND
key (SPEC CT-3), and `_match_registry` ranks a two-segment key above the
one-segment `.json` — so a file named `triage.workflow.json` resolves here while
every other `.json` keeps the `tree`/`code`/`duckdb` list it always had, with no
reordering of the plain `.json` key at all. That is the same shape `.calls.jsonl`
uses to claim `log_studio` out from under `.jsonl`.

So this gate is deliberately THIN, and the three things it does are the three the
key cannot say:

1. **A mount-backed path is refused, before any read.** Same rule, through the
   same `../shared/appenv.is_mount_backed`, as `git`/`graph`/`mcp` — the panel
   writes the document back on every Save, and a write into a wedged rclone-NFS
   mount is the pattern those gates were written for. If the import fails we
   cannot tell, and "cannot tell" reads as "refuse" (CT-12).
2. **File-only, and bounded.** A workflow is a plan — a few dozen nodes at the
   outside — so a multi-megabyte file with this name is something else and the
   canvas would try to lay all of it out.
3. **A hand-typed `?_mode=workflow` on an unrelated file answers False.** The
   key already routes correctly; this is what stops the mode being reachable on
   a `.parquet` by URL, where the panel would render an empty canvas over a file
   it is about to overwrite with a workflow document.

**The content is deliberately NOT parsed here.** A `.workflow.json` that does not
parse as JSON is exactly the file whose owner most needs this editor, and a gate
that hid the mode for it would leave them with no way back in except the `code`
mode. The panel reads the file itself and renders the parse error as a banner
with the option to start over — MD-11's split again: the gate is the UX, the
backend is the guarantee.

**An EMPTY file is a valid new workflow**, and this is load-bearing. The explorer
has no workflow-specific "new document" affordance and this template does not add
one: the generic New File… prompt (`listing/useFileOps.ts`) creates a zero-byte
file with whatever name the user types, so typing `triage.workflow.json` there is
the whole creation path. The panel treats empty content as `{nodes: [], edges: []}`
rather than as a parse error, which is what makes that path work.

---

## The document

```json
{
  "version": 1,
  "name": "Triage the inbox",
  "nodes": [
    {
      "id": "n1",
      "app": "/home/me/Fused/showcase/open-mail",
      "tool": "search_mail",
      "label": "Find unread",
      "x": 40, "y": 60,
      "inputs": [
        {"name": "query", "source": "literal",  "value": "is:unread"},
        {"name": "limit", "source": "literal",  "value": "20"},
        {"name": "folder", "source": "previous", "value": ""},
        {"name": "attachment", "source": "trigger", "key": "path"}
      ],
      "observedOutput": {"kind": "object", "keys": ["messages", "total"]}
    },
    {
      "id": "n2",
      "kind": "prompt",
      "prompt": "Summarise which accounts have unread mail.",
      "label": "Summarise",
      "x": 700, "y": 90
    }
  ],
  "edges": [
    {"id": "e1", "from": "n1", "to": "n2",
     "condition": "at least one message is from a customer"}
  ]
}
```

`kind` is the ONE discriminator, and its absence is load-bearing: a node with no
`kind` is a `"tool"` node, which is every node in every document written before
prompt steps existed. The two kinds are:

* **`"tool"`** (the default) — `app` + `tool` name a curated MCP tool, and
  `inputs` records which of its parameters this step sets.
* **`"prompt"`** — a REASONING step. `prompt` holds the author's sentence; `app`,
  `tool` and `inputs` are absent, because there is no signature to expose
  parameters from. It compiles to *do this, then call `step_note` with your
  conclusion* against `step_server.py`'s one-tool MCP server, so the step is
  observed from a real tool call like every other one, and its conclusion is a
  tool result a downstream node's `source: "previous"` input reads like every
  other step's output. `run.py`'s docstring and `step_server.py`'s record why
  that shape was chosen over a second progress mechanism.

`inputs` is the part that does not fall out of MCP, and it is why the format
exists at all. A dispatcher entrypoint gives every parameter a default, so its
MCP input schema reports `required: []` and — measured on `open-mail`'s
`send_mail` — **22 properties**. Rendering 22 rows per node is not a canvas, it
is a form nobody fills in. So the AUTHOR chooses which parameters this node
exposes and the document records that choice; everything unlisted is left to the
tool's own default. 
`source` has THREE values, and the third is what makes a workflow runnable by
something other than a person (SPEC WC-11):

* **`"literal"`** — use `value`.
* **`"previous"`** — Claude fills it from the upstream node's output at run time.
* **`"trigger"`** — read it out of the JSON object the RUN WAS STARTED WITH.
  `key` names which key (defaulting to the parameter's own name, because a file
  trigger's `path` landing on a parameter called `path` is the common case and
  writing it twice would be ceremony), and `value` is unused. A run started with
  no matching key is REFUSED at compile time naming the step and the key — never
  run with an empty string, which is how "reply to the invoice that arrived"
  quietly becomes "reply to ''" inside a detached session. The payload comes from
  the panel's "Run with input…" sheet or from a trigger armed against this
  document, and `run.py` renders every value of it as a JSON literal because its
  author may be whoever dropped a file in a watched folder (WC-11b).

An edge with a `condition` is a branch the runner states as a rule; an edge with
none is a plain "then", left to Claude's judgement. `observedOutput` is written
by `run.py` after a run — see its docstring for why it lands here rather than in
the app's `mcp.toml`.

**The document does NOT record whether this workflow is armed.** Arming — the
approval that lets a trigger start a run with nobody watching, and the tool-set
fingerprint that approval is made of — lives in a durable store owned by
`fused_render/workflow_triggers.py`, reached over `/api/workflow-triggers`. Not
merely because a template imports nothing from `fused_render` (SPEC PY-15 /
D166), but because an approval stored in the file the user edits would be an
approval the user could edit, and the fingerprint check exists precisely because
this file changes under an armed workflow (WC-12b).

That check is made by `run.py` and not by the store, and the reason is worth
knowing before editing either: the approved set is handed to `run.py` on the
start call, and it is compared against the same compile whose steps become
`--allowed-tools`. A caller that compiled this file itself, compared, and then
asked for a start would be checking one reading of it and authorizing another —
and this file is hand-editable and saved by a canvas, so a save fits comfortably
in between (WC-12a-i).

Self-contained apart from `../shared/appenv.py`; nothing here imports
fused_render (SPEC PY-15 / D166).
"""

# The document is a PLAN, not data. A workflow with a hundred nodes is already
# past what a canvas can show and past what one Claude session should be handed
# in a single prompt; a file above this is not a plan at all, and laying it out
# would stall the panel on a file the user did not mean to open here.
_MAX_BYTES = 2 * 1024 * 1024

_SUFFIX = ".workflow.json"


def main(path: str) -> bool:
    import os
    import sys

    try:
        # (1) Mount-backed -> refuse, before any stat of the file itself.
        #
        # Through `shared/appenv` (env vars only, stdlib only) rather than by
        # importing fused_render, so the mount rule has ONE home for every
        # template (SPEC PY-15).
        shared = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared")
        # Guarded insert: _run_condition re-execs this module on EVERY stat, so
        # an unconditional insert would grow sys.path without bound.
        if shared not in sys.path:
            sys.path.insert(0, shared)
        try:
            from appenv import is_mount_backed
        except Exception:  # noqa: BLE001 — cannot tell -> refuse (CT-12)
            return False
        if is_mount_backed(path):
            return False

        if not path:
            return False

        # (2) The name, re-checked. The registry key already matched it, but a
        # hand-typed `?_mode=workflow` reaches this gate with any path at all,
        # and the panel's Save would write a workflow document over whatever it
        # was pointed at. A non-empty stem is required for the same reason
        # `_match_registry` requires one: a file literally named
        # `.workflow.json` is a dotfile, not somebody's workflow.
        name = os.path.basename(path)
        if not name.lower().endswith(_SUFFIX) or len(name) <= len(_SUFFIX):
            return False

        # (3) File-only and bounded. `isfile` rather than `not isdir` so a path
        # that does not exist reads as "refuse" — the explorer only offers a
        # mode for something it has just listed.
        st = os.stat(path)
        import stat as _stat
        if not _stat.S_ISREG(st.st_mode):
            return False
        return st.st_size <= _MAX_BYTES
    except Exception:  # noqa: BLE001 — a broken gate must hide, never raise
        return False
