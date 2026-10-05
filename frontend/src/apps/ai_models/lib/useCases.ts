// The three text use cases, defined ONCE (SPEC AI-28b): their ids, labels and
// order, plus how a catalog row says which one it belongs to and how a list of
// rows is split by them. Read by the Models page (its three headed sections)
// and the Playground (its three sidebar sections), so the label a person sees
// in one place is the label in the other.
//
// Which use case a model suits is the server's answer (`AiCatalogModel.useCases`,
// curated by hand on a curated row, a name heuristic on any other); this file
// owns only the words and the grouping. Imports nothing but a type.
import type { AiCatalogModel } from "@platform/lib/api";

export type UseCaseId = "writing" | "coding" | "reasoning";

export interface UseCase {
  id: UseCaseId;
  /** The section heading, on the Models page and in the Playground sidebar. */
  label: string;
}

export const USE_CASES: UseCase[] = [
  { id: "writing", label: "Writing & chat" },
  { id: "coding", label: "Coding" },
  { id: "reasoning", label: "Deep reasoning" },
];

const KNOWN = new Set<string>(USE_CASES.map((u) => u.id));

/** The use case a catalog row belongs to: the first of its `useCases` this
 *  client knows, else writing (an older server sends none). */
export function useCaseOf(entry: Pick<AiCatalogModel, "useCases">): UseCaseId {
  return (entry.useCases ?? []).find((id): id is UseCaseId => KNOWN.has(id)) ?? USE_CASES[0].id;
}

export interface UseCaseGroup<T> {
  useCase: UseCase;
  items: T[];
}

/** Split `items` into one group per use case, in USE_CASES order, each keeping
 *  its items' existing order. `idOf` says which use case an item belongs to.
 *  A use case with no items is dropped. */
export function groupByUseCase<T>(items: T[], idOf: (item: T) => UseCaseId): UseCaseGroup<T>[] {
  return USE_CASES.map((useCase) => ({
    useCase,
    items: items.filter((item) => idOf(item) === useCase.id),
  })).filter((g) => g.items.length > 0);
}
