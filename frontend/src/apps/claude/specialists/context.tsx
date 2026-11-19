// THE CARD SEAM for specialists (docs/specialists.md): `CardStack` asks
// this context first for each permission row, and draws its own card when the
// answer is null. ClaudeChat provides it only while the `specialists` flag is
// on, so with the flag off `CardStack` is exactly what it was.
//
// Two rows are ours:
//   - `mcp__fused_render__specialist_breakout` in a task: the offer card (OfferCard).
//   - `mcp__specialist_services__*` in a specialist's MAIN chat: the ordinary
//     approval card plus "Approve, don't ask again" (ServiceCardExtra).
import { createContext, type ReactNode } from "react";
import type { CardActions } from "../ui/CardStack";
import type { PermissionMode, PermissionRow } from "../protocol/types";

export const OFFER_TOOL = "mcp__fused_render__specialist_breakout";

/** The `specialist` prop of a chat that IS a specialist session. The optional
 *  facts let the thread show the product view of app-written messages (docs/specialists.md §5):
 *  `name` for the words, `brief` for the seed's brief box, and `folder` (the
 *  task's folder) so file links in the replies resolve there, not in the
 *  breakout's private workroom. */
export interface SpecialistSessionProp {
  id: string;
  kind: "main" | "breakout";
  name?: string;
  brief?: { problem: string; tried: string; look_at: string };
  /** The task this breakout came from, for the brief box's heading. */
  taskTitle?: string;
  folder?: string;
}

export interface SpecialistCards {
  /** A card of ours for `row`, or null to let CardStack draw its own. */
  renderCard(row: PermissionRow, actions: CardActions, liveMode?: PermissionMode): ReactNode | null;
}

export const SpecialistCardContext = createContext<SpecialistCards | null>(null);
