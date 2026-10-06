# Connect Google Docs
trigger: connect google docs, set up google docs, google docs setup, google docs key, is google docs connected, which google docs can you see

1. Call the tool `docs_status` (app `google-docs-tabs`, no args). It returns `connected`, the service account's `email` and the saved `docs` library.
2. If the tool is not listed under APP TOOLS at all, say so: the Google Docs Tabs app is missing from the Apps folder; the user installs it from Apps › Starter apps. Then `done`.
3. If `connected` is false: `show` the app `google-docs-tabs` as a card and explain the one-time setup in four short lines: enable the Google Docs and Google Drive APIs in Google Cloud Console; create a service account (no roles needed); add a JSON key and paste it into the app's Setup box; share each doc with the account's email as an Editor.
4. Ask the user to tell you when the key is saved, then call `docs_status` again and confirm the `email` it reports.
5. If `connected` is true: report the email and the saved docs (name and link each). If the user named a doc that is not in the library, call `docs_add_doc` with its URL (it verifies the doc opens).
6. A 403 or 404 from any docs tool means the doc is not shared with that email: say exactly which email to add as an Editor, then `done`.
