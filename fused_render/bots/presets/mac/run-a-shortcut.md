# Run or find a Shortcut
trigger: run my shortcut, run the shortcut, shortcuts app, which shortcuts do i have, log water, focus mode, do not disturb, something only a shortcut can do

1. `tool` mac_shortcuts, with `name` as a filter when the task names one. Pick the match by name and keep its UUID.
2. If no shortcut fits, say so: Shortcuts cannot be created from here. Describe the one or two actions the user should add in Shortcuts.app (for CLI use it must end in "Stop and Output", not "Show Result"; Script actions need Shortcuts > Settings > Advanced > Allow Running Scripts), then stop.
3. Show the user the shortcut name and the input you will feed it, and proceed only after approval unless the task asked to run it in so many words.
4. `tool` mac_run_shortcut with the UUID as `name`, `input` text if any, `output_type` public.plain-text (or public.json for structured results).
5. rc 124 means it did not finish in 30 s: it shows a result, asks for input, or waits on a first-run permission prompt. Tell the user to open Shortcuts.app, run it once by hand, and change its last action to Stop and Output.
6. Report the output verbatim and the shortcut name.
