// Path helpers for the New task card: normalisation, the localStorage recents
// the folder field reads, the split-and-verdict logic behind "what did the
// user type", and the breadcrumb trail the explorer panel draws. Pure — no DOM,
// no React — so new-task-form.test.ts can assert every decision.

// ---- Recent paths --------------------------------------------------------
// The path field's dropdown offers the last folders the user actually used,
// newest first, five shown. It draws on two sources — the app-wide one the home
// page reads (see the section below) and this form's own memory of folders
// picked in the browser or saved on a task, which is what the rest of THIS
// section is. That second half lives in localStorage so "the folder I always
// schedule against" survives reloads.
// try/catch throughout: storage can be denied (private mode), and a corrupt
// value must read as "no recents", never crash the modal (Bugbot, PR #538
// pattern).
export const RECENTS_KEY = "fused-render:recent-paths";
export const RECENTS_SHOWN = 5;

export function readRecents(): string[] {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(RECENTS_KEY) ?? "[]");
    return Array.isArray(parsed)
      ? parsed.filter((p): p is string => typeof p === "string" && p !== "")
      : [];
  } catch {
    return [];
  }
}

export function rememberRecent(path: string) {
  const p = path.trim();
  if (!p) return;
  try {
    const next = [p, ...readRecents().filter((r) => r !== p)].slice(0, 8);
    localStorage.setItem(RECENTS_KEY, JSON.stringify(next));
  } catch {
    // Storage denied — recents just don't persist.
  }
}

// Forward slashes throughout, including for Windows drive paths — the same
// normalization every other shell caller applies to `/api/config` values,
// whose `home` is a raw expanduser and arrives with backslashes there. The
// server accepts either separator; the PICKER's own string surgery (up(),
// joins) only understands one.
export const normPath = (p: string) => p.replace(/\\/g, "/");

// A path split into the folder it lives in and its last segment. Drive roots
// keep their slash — bare "C:" reads as cwd-relative elsewhere in the shell,
// not as the root (the same trap the picker's climb fixed in PR #548).
export function splitTargetPath(path: string): { parent: string; base: string } {
  const norm = normPath(path).trim().replace(/\/+$/, "");
  const cut = norm.lastIndexOf("/");
  const parent = cut > 0 ? norm.slice(0, cut) : "/";
  return {
    parent: /^[A-Za-z]:$/.test(parent) ? parent + "/" : parent,
    base: norm.slice(cut + 1),
  };
}

// ---- What the typed path IS ---------------------------------------------------
// Three answers, not two (Akshil, 2026-08-20): a path can also be a folder that
// does not exist YET. Standing in `.../fused/` and typing `ABC1` is how a person
// says "run this in a new folder called ABC1", and the form used to answer that
// with a red line refusing to save.
//
// ONE new segment, and only one. Its parent has to be somewhere the user can
// point at, because "make the folder I named" and "build me a tree I typed" are
// different asks and only the first is one a typo cannot cause. `/a/new1/new2`
// with no `new1` is the second, and it is refused with the reason.
//
// Pure so the decision can be asserted without a DOM (new-task-form.test.ts);
// the effect below only feeds it what the two listDir calls came back with.
export type TargetVerdict =
  | { kind: "ok" }
  // `name` is the segment that will be created; `parent` is where.
  | { kind: "new-folder"; name: string; parent: string }
  | { kind: "bad"; text: string };

export const PATH_MISSING = "This folder or file doesn't exist";

export function twoLevelsMissing(parent: string): string {
  return `Only one new folder can be created — ${parent} doesn't exist either`;
}

// `parentNames` is the parent folder's entry names, or null when the PARENT
// itself could not be listed — which is the two-missing-levels case.
export function targetVerdict(
  path: string,
  parentNames: string[] | null,
): TargetVerdict {
  const { parent, base } = splitTargetPath(path);
  // "." and ".." name a folder that already exists by definition, so reaching
  // here with one of them means the path was junk rather than a new name.
  if (!base || base === "." || base === "..") {
    return { kind: "bad", text: PATH_MISSING };
  }
  if (parentNames === null) return { kind: "bad", text: twoLevelsMissing(parent) };
  // The parent lists and already holds this name: a FILE target, which is legal
  // — a task can run against a file. (A folder would never have got this far;
  // listing it directly is what the caller tries first.)
  if (parentNames.includes(base)) return { kind: "ok" };
  return { kind: "new-folder", name: base, parent };
}

export interface Crumb {
  name: string;
  path: string;
}

// The path as clickable crumbs: every ancestor is one tap away, which is what
// the old single "up" chevron made people hunt for (Akshil, 2026-08-15 — "not
// intuitive"). Root renders as "/" (or "C:/"), each segment jumps there.
export function crumbsOf(path: string): Crumb[] {
  const trimmed = path.replace(/\/+$/, "");
  const drive = trimmed.match(/^[A-Za-z]:/)?.[0];
  const rootPath = drive ? drive + "/" : "/";
  const rest = (drive ? trimmed.slice(drive.length) : trimmed)
    .split("/")
    .filter(Boolean);
  const out = [{ name: drive ?? "/", path: rootPath }];
  let acc = drive ?? "";
  for (const seg of rest) {
    acc += "/" + seg;
    out.push({ name: seg, path: acc });
  }
  return out;
}

// A real path is deeper than a 460px panel is wide, and the trail used to wrap
// onto three lines — which moved the filter, the listing and the foot down with
// it, so the panel's whole geometry hung off how long the current path happened
// to be (audit 2026-08-16). Past four segments the middle collapses to one "…",
// which is NOT a control: there is no single folder it could stand for.
const CRUMBS_SHOWN = 4;

export function collapseCrumbs(crumbs: Crumb[]): (Crumb | null)[] {
  if (crumbs.length <= CRUMBS_SHOWN) return crumbs;
  return [crumbs[0], null, crumbs[crumbs.length - 2], crumbs[crumbs.length - 1]];
}
