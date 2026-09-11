// Which disclosures are open: the ones the reader opened, and no others
// (T:15163-15229).
//
// Every collapsible in the transcript ships COLLAPSED, the newest and the
// still-streaming one included: a single turn can make twenty tool calls, and a
// transcript that opens bodies on its own MOVES under the reader while the run
// is live. The user's click is the only thing that opens a card, and it STICKS
// across the 400 ms poll, a repair branch and a history replay.
//
// Overrides are keyed rather than held on the element (or in component state),
// because a re-render can discard the node: a tool call is keyed by its
// `tool_use` id — stable across every re-render of that call — and a thinking
// segment, which has no id, by its position inside a numbered container.
//
// ONE MAP PER MOUNT, handed down through a context. A module-global map is what
// the template could afford — one page, one chat — but six compact mounts share
// this module on the cards wall, and there the keys COLLIDE by construction
// (`seq:index`, and `tool:<id>` for the same tool call replayed in two cards):
// same-keyed chips opened in lockstep across the wall, and one card's Back
// wiped the collapse policy of the other five.
import { createContext, useCallback, useContext, useState } from "react";

import type { Segment } from "../protocol/types";

/** One chat's collapse overrides. */
export interface CardPolicy {
  overrides: Map<string, boolean>;
}

export function createCardPolicy(): CardPolicy {
  return { overrides: new Map<string, boolean>() };
}

/** For a component rendered with no provider above it — a unit test, and any
 *  future host that frames one chip on its own. Its own map, so it cannot be
 *  the shared bucket the per-mount ones exist to replace. */
const standalone = createCardPolicy();

const CardPolicyContext = createContext<CardPolicy>(standalone);

export const CardPolicyProvider = CardPolicyContext.Provider;

/** A fresh transcript is a fresh policy: a restored session opens with every
 *  card folded, so an override from the conversation that was on screen before
 *  cannot leak a card open in one the user has never touched. */
export function resetCardPolicy(policy: CardPolicy = standalone): void {
  policy.overrides.clear();
}

/** T:15206-15209 — tools and thinking are keyed differently; see above. */
export function cardKey(seq: number, seg: Segment | null | undefined, i: number): string {
  return seg && seg.kind === "tool" && seg.id ? "tool:" + seg.id : seq + ":" + i;
}

/** The standing policy for one card, plus the toggle that records the reader's
 *  choice. Read straight off the module map so a component re-rendered under a
 *  NEW key resolves to that key's state rather than to stale local state. */
export function useCardOpen(key: string): readonly [boolean, () => void] {
  const policy = useContext(CardPolicyContext);
  const [, bump] = useState(0);
  const open = policy.overrides.get(key) ?? false;
  const toggle = useCallback(() => {
    policy.overrides.set(key, !(policy.overrides.get(key) ?? false));
    bump((n) => n + 1);
  }, [key, policy]);
  return [open, toggle] as const;
}

/** Test seam: what the map currently holds for a key. */
export function cardOverride(key: string, policy: CardPolicy = standalone): boolean | undefined {
  return policy.overrides.get(key);
}
