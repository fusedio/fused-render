// THE CARD SEAM for specialists (docs/specialists PLAN C.4): `CardStack` asks
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

export interface SpecialistCards {
  /** A card of ours for `row`, or null to let CardStack draw its own. */
  renderCard(row: PermissionRow, actions: CardActions, liveMode?: PermissionMode): ReactNode | null;
}

export const SpecialistCardContext = createContext<SpecialistCards | null>(null);
