# Apple Notes add lines to an existing note
trigger: add to my note, append to note, add this to the, put this in my note, add to my list, add a line to

1. Find the note: `tool` notes_search with the name from the task (or notes_recent if the task says "my latest note"). If several notes match, show the user their titles, folders and modified dates and ask which one; never guess.
2. `tool` notes_read on the chosen id and show the user the note's last few lines, so the addition lands where they expect.
3. Compose the text to append from the task, one paragraph per line, keeping the user's wording. Show the note title, folder and the exact lines to add; proceed only after approval, since Notes saves at once and there is no undo.
4. After approval `tool` notes_append with the note's id and text (it pauses for approval itself). If it returns candidates instead of ok, the title was ambiguous: pass the id. If it says the note is locked, ask the user to unlock it in Notes and retry once they confirm. On a NotesBridge permission error relay its instructions and stop.
5. `tool` notes_read again and quote the appended lines as saved. If approval was refused, change nothing and say so.
