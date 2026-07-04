# Registering log_template.html for the .log extension

fused-render picks a preview template per file extension from the `TEMPLATES`
dict in `fused_render/server.py`. Currently `.log` maps to the generic
`text_template.html`:

```python
TEMPLATES = {
    ...
    ".log": "text_template.html",
    ...
}
```

To make `.log` files open with this new template instead, change that one
line to:

```python
".log": "log_template.html",
```

Steps to install (not performed by this task — server code was left untouched):

1. Copy `log_template.html` and `log_reader.py` into `fused_render/templates/`
   (next to `parquet_template.html` / `parquet_reader.py`).
2. Change the `.log` entry in the `TEMPLATES` dict in `fused_render/server.py`
   from `"text_template.html"` to `"log_template.html"`.
3. Restart `fused-render` so the updated dict is loaded.

No other server code needs to change — `get_template_for_path()` already
resolves `TEMPLATES[ext]` relative to `TEMPLATES_DIR`, and the template pairs
its own `log_reader.py` via a relative `fused.runPython("./log_reader.py", ...)`
call, same pattern as the existing parquet template.
