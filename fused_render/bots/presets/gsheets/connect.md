# Connect Google Sheets
trigger: connect google sheets, set up google sheets, google sheets setup, google sheets key, is google sheets connected, which spreadsheets can you see

1. Call the tool `sheets_status` (app `google-sheets-tabs`, no args). It returns `connected`, the service account's `email` and the saved `docs` library of spreadsheets.
2. If the tool is not listed under APP TOOLS at all, say so: the Google Sheets Tabs app is missing from the Apps folder; the user installs it from Apps › Starter apps. Then `done`.
3. If `connected` is false: `show` the app `google-sheets-tabs` as a card and explain the one-time setup in four short lines: enable the Google Sheets and Google Drive APIs in Google Cloud Console; create a service account (no roles needed); add a JSON key and paste it into the app's Setup box; share each spreadsheet with the account's email as an Editor.
4. Ask the user to tell you when the key is saved, then call `sheets_status` again and confirm the `email` it reports.
5. If `connected` is true: report the email and the saved spreadsheets (name and link each). If the user named one that is not in the library, call `sheets_add_doc` with its URL (it verifies it opens).
6. A 403 or 404 from any sheets tool means the spreadsheet is not shared with that email: say exactly which email to add as an Editor, then `done`.
