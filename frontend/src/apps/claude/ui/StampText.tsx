// A STAMP'S WORDS, with its NUMBERS at full weight and the words beside them
// lighter (Akshil, 2026-10-06: "apart from numbers, make the font-weight
// thinner for other words"). `41m ago` → the `41m` is the fact, `ago` is the
// grammar; `4 Oct, 22:42` keeps its digits and lightens the month. One split,
// shared by the message stamp (Turn) and the chip stamp (ToolChip), so the two
// lanes read as one register.
import { Fragment } from "react";

/** Digit runs — with the unit glued to them (`41m`, `22:42`, `2026`) — stay at
 *  the stamp's weight; everything else wears `.stamp-word`. */
export function StampText({ text }: { text: string }) {
  const parts = text.split(/(\d[\d:.]*[a-z]?)/g).filter((p) => p !== "");
  return (
    <>
      {parts.map((p, i) =>
        /^\d/.test(p) ? <Fragment key={i}>{p}</Fragment> : <span key={i} className="stamp-word">{p}</span>,
      )}
    </>
  );
}
