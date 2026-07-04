# Registering .log with this template

fused-render maps extensions to template files in one place:
`fused_render/server.py`, the `TEMPLATES` dict (around line 21).

Currently:

```python
TEMPLATES = {
    ...
    ".log": "text_template.html",
    ...
}
```

To use this new template instead of the generic text viewer, change that
one line's value:

```python
TEMPLATES = {
    ...
    ".log": "log_template.html",
    ...
}
```

That's the only code change needed (`_template_for()` in the same file
already does `os.path.join(TEMPLATES_DIR, name)`, so no other lookup logic
changes). No other server code needs to change.

## Files to drop in

Copy both files into `fused_render/templates/`:

- `log_template.html`
- `log_reader.py`

(`log_template.html` calls `fused.runPython("./log_reader.py", ...)`, a
relative path resolved next to the HTML file, same pattern
`parquet_template.html` uses with `parquet_reader.py` — so both files must
live in the same `templates/` directory.)

## Note

This change was NOT applied as part of this task (server code was
intentionally left untouched, per instructions). Apply the one-line edit
above to activate `.log` preview with this template.
