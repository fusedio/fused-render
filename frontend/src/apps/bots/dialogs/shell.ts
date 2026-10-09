// The one shadcn dialog shell every bots-page dialog wears (owner, 2026-10-09: "all are same size as the settings
// modal, use same shadcn style language and organisation principles"). The surface per theme is .bots-dialog in
// bots.css; the ring, the type and every control are stock shadcn. DIALOG_SIZE is the Settings box, 880 wide and
// min(760px, 90vh) tall, FIXED: a list filling or a search emptying never resizes the dialog. The page dialogs
// (Settings, Browsers, the New bot picker and form, Usage) take it; the two pop-overs (Confirm, the build question) are
// content-height on the same skin. A dialog that needs a third size is a review flag.
export const DIALOG_CLASS = "bots-dialog gap-0 overflow-hidden p-0";
export const DIALOG_SIZE = "flex h-[min(760px,90vh)] flex-col sm:max-w-[880px]";
export const HEADER_CLASS = "shrink-0 gap-1 px-6 pt-5 pb-4";
export const FOOTER_CLASS = "mx-0 mb-0 shrink-0 rounded-b-xl border-t border-foreground/10 bg-foreground/[0.03] px-6 py-4";
/** A list card inside a dialog (the Browsers cards, a playbook, a routine). */
export const CARD_CLASS = "flex flex-col gap-3 rounded-xl border border-foreground/10 bg-foreground/[0.02] p-4";
