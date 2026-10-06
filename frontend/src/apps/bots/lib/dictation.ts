// What the composer says while a dictation is being transcribed.
//
// A transcription is a job (/api/ai/transcribe), and the first one on a machine spends minutes building the speech
// runner's venv and downloading the model before any audio is read. The server narrates that in the job's `detail`
// ("Preparing MLX Whisper — downloading torch (1m40s)…") and points at the model job in `waiting_for`; a bare spinner
// over that stretch reads as a hang (seen 2026-10-04: "the transcriber is not working" was a 300 MB torch download).
// So the composer polls the job anyway and shows this sentence instead of silence.

export interface JobNoteSource {
  detail?: string;
  waiting_for?: string;
}

/** The sentence for a transcription job row (or none yet): the server's own `detail` when it has one, a first-run
 *  install notice when the job is only waiting on a model, else the plain state. */
export function jobNote(rec: JobNoteSource | null | undefined): string {
  const detail = (rec?.detail || "").trim();
  if (detail) return detail;
  if ((rec?.waiting_for || "").startsWith("sys:ai-model:")) return "Preparing the speech model (first time only)…";
  return "Transcribing…";
}
