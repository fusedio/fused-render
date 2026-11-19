// The /specialists URL space (docs/specialists.md), spelled once here
// like apps/bots/routes.ts: the shell's dispatcher, the sidebar and the page
// all ask this module.
//
//   /specialists          the bare door: the page shows the first specialist once the list knows one
//   /specialists/<id>     one specialist's page (ids are uuid4().hex[:8])
//
// An unknown shape (`/specialists/<id>/x`) lands on that specialist's page.

export const SPECIALISTS_PREFIX = "/specialists";

export type SpecialistsRoute = { kind: "list" } | { kind: "specialist"; id: string };

/** Anywhere on the Specialists page. */
export function isSpecialistsPath(pathname: string): boolean {
  return pathname === SPECIALISTS_PREFIX || pathname.startsWith(SPECIALISTS_PREFIX + "/");
}

const decode = (s: string): string => {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
};

/** The page a pathname names. Never throws. */
export function parseSpecialistsPath(pathname: string): SpecialistsRoute {
  if (!isSpecialistsPath(pathname)) return { kind: "list" };
  const [head] = pathname.slice(SPECIALISTS_PREFIX.length).split("/").filter(Boolean);
  if (!head) return { kind: "list" };
  const id = decode(head);
  if (!id || id === "." || id === ".." || id.includes("/")) return { kind: "list" };
  return { kind: "specialist", id };
}

export const specialistsHref = (): string => SPECIALISTS_PREFIX;
export const specialistHref = (id: string): string => `${SPECIALISTS_PREFIX}/${encodeURIComponent(id)}`;
