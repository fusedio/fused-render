// Which pull requests a task is about. Lives in platform/ because the chat
// header chip (apps/claude, explorer) and the Tasks PR view (shell) both read
// it, and apps may not import the shell.
import type { Task } from "./api";

export type PrRef = { repo: string | null; number: number; url?: string | null };

/** Stable identity of a PR across spellings: `owner/repo#12`, or `?#12` when
 *  only the number is known. Case is kept; callers compare what they were given. */
export function prKey(ref: PrRef): string {
  return `${ref.repo ?? "?"}#${ref.number}`;
}

/** The pull request's GitHub page, or null when its repo is unknown. */
export function prUrl(ref: PrRef): string | null {
  if (ref.url) return ref.url;
  return ref.repo ? `https://github.com/${ref.repo}/pull/${ref.number}` : null;
}

/** The pull request a task's words name — "#1234", "PR 1234" or a GitHub
 *  `/pull/1234` link in its title, description or latest message. */
function prText(task: Task): string {
  return [
    task.title,
    task.description,
    task.last_message?.text ?? "",
    task.last_reply ?? "",
  ].join("\n");
}

export function prOfTask(task: Task): number | null {
  const text = prText(task);
  // Named first ("PR 1203", "PR #1203", a /pull/1203 link), then a bare
  // "#1203" — two digits, no leading zero, so "#2" in a numbered step and "#000" in a
  // colour do not become pull requests. `[ \t]*`, never `\s*`: the fields are
  // joined with newlines, and a title ending in "PR" must not borrow the next
  // line's leading number (review, 2026-10-16).
  const named = /(?:\/pull\/|\bPRs?[ \t]*#?[ \t]*)(\d{1,6})\b/i.exec(text);
  if (named) return Number(named[1]);
  const bare = /(?<![\w#])#([1-9]\d{1,5})\b/.exec(text);
  return bare ? Number(bare[1]) : null;
}

/** Every pull request a task is about, NEWEST FIRST, one per prKey.
 *
 *  The server's list (`task.prs`, every PR URL seen in the transcript, oldest
 *  first) leads; the words of the title, description, latest message and reply
 *  add what it has not seen: every GitHub link, every named PR ("PR 12",
 *  "/pull/12"), and bare "#12" numbers only when nothing is linked or named.
 *  A named or bare number takes the task's folder repo; one whose number is
 *  already known with a repo is skipped rather than repeated as "c/d#12". */
export function prRefsOf(task: Task): PrRef[] {
  const out: PrRef[] = [];
  const seen = new Set<string>();
  const knownNumbers = new Set<number>();
  const add = (ref: PrRef) => {
    const key = prKey(ref).toLowerCase(); // GitHub repo names are case-insensitive
    if (seen.has(key)) return;
    seen.add(key);
    out.push(ref);
    if (ref.repo) knownNumbers.add(ref.number);
  };
  for (const p of task.prs ?? []) add({ repo: p.repo, number: p.number, url: p.url });

  const text = prText(task);
  const folderRepo = (task.repo || "").trim() || null;
  let linkedOrNamed = false;
  for (const m of text.matchAll(/github\.com\/([\w.-]+\/[\w.-]+?)(?:\.git)?\/pull\/(\d{1,6})\b/gi)) {
    linkedOrNamed = true;
    add({ repo: m[1], number: Number(m[2]) });
  }
  const named = [...text.matchAll(/(?:\/pull\/|\bPRs?[ \t]*#?[ \t]*)(\d{1,6})\b/gi)];
  if (named.length) linkedOrNamed = true;
  for (const m of named) {
    const n = Number(m[1]);
    if (!knownNumbers.has(n)) add({ repo: folderRepo, number: n });
  }
  if (!linkedOrNamed) {
    for (const m of text.matchAll(/(?<![\w#])#([1-9]\d{1,5})\b/g)) {
      const n = Number(m[1]);
      if (!knownNumbers.has(n)) add({ repo: folderRepo, number: n });
    }
  }
  return out.reverse();
}
