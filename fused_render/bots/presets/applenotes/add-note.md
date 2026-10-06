# Apple Notes draft a new note
trigger: add a note, create a note, save this to notes, write a note, put in my notes, jot down, new note in

1. If APP TOOLS lists notes_create, use it: `tool` notes_folders to confirm the folder the task names exists (default: the default account's Notes folder). Otherwise goto https://www.icloud.com/notes/, `wait` for the folder list, and use `login` if a sign-in, 2FA or trust page appears.
2. Compose the note from the task: the title, then the body with one paragraph per line; keep the user's wording. Show the user the folder, title and full body and proceed only after approval, since Notes saves at once and there is no undo.
3. After approval `tool` notes_create with title, body and folder (it pauses for approval itself; approve the same text once). On the web instead: click the folder, click the new-note button (the pencil-in-square icon above the editor), `type` the text, `wait` 3 seconds and `read` the note list to confirm the new title shows today's date.
4. If notes_create returns an error about NotesBridge, relay its instructions (System Settings > Privacy & Security > Automation, allow Notes under NotesBridge) and stop; do not retry or fall back to the web without asking.
5. Report the folder and title created and the body as saved. If approval was refused, create nothing and say so.
