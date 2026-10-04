/** "TASK-004" printed as "T004" — the short form every surface prints
 * (Akshil, 2026-09-16). Display only: the stored id, the API field, `?peek=`
 * URLs and the search index keep the long form, so nothing on disk or on the
 * wire changes. Lives in platform/ so shell/, apps/ and platform/ui can all
 * print the one spelling. */
export function shortTaskId(id: string | null | undefined): string {
  return (id || "").replace(/^TASK-/, "T");
}
