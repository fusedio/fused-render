// Calendar vocabulary shared by the when-row, the recurrence panel and the
// card's own labels.
export const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
export const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

// "8:30pm" — Google's compact clock wording, used by the field and its list.
export function fmtTime(h: number, m: number): string {
  const ap = h < 12 ? "am" : "pm";
  const hh = h % 12 === 0 ? 12 : h % 12;
  return `${hh}:${String(m).padStart(2, "0")}${ap}`;
}

// Parse what a person types into a time field: "8", "8:30", "8:30pm", "20:15".
// null = not a time; the field then falls back to what it had.
export function parseTime(text: string): { h: number; m: number } | null {
  const m = text.trim().toLowerCase().match(/^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$/);
  if (!m) return null;
  let h = Number(m[1]);
  const mins = Number(m[2] ?? 0);
  if (mins > 59) return null;
  if (m[3] === "pm" && h < 12) h += 12;
  if (m[3] === "am" && h === 12) h = 0;
  if (m[3] && Number(m[1]) > 12) return null;
  return h > 23 ? null : { h, m: mins };
}
