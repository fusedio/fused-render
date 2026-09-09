// THE MCP COMPANION AS A DIALOG. `mcp` (the folder's MCP manifest and tools,
// templates/mcp) was the third companion in the file preview's sidebar beside
// Claude and Git. It left that column when the column's switcher became a tab
// strip (SideChrome's SideTabs): two tabs read at a glance, three start to crowd
// a column that is often 380px wide, and of the three MCP is the one you CONFIGURE
// rather than work alongside — you set the manifest up, you check what it
// publishes, you leave. That is a dialog's shape, and it is the shape App Doctor
// already has on the same kebab (EntryActionsMenu), so the two app-level tools
// open the same way from the same menu.
//
// The content is the same document the sidebar framed: the template rendered
// through /render with `_file` aimed at the FOLDER (the manifest covers the app,
// not the previewed page — lib/dir-mode's whole argument). Preview.tsx builds the
// `src` because it owns the URL shape (`_noopen`, thumb flags); this component
// is the box. A plain iframe, not ChatFrame: only the chat template stamps
// `data-chat-ready`, and a cover revealed by the 8s fallback would be a new wait.
//
// The folder listing's pane still lists MCP as a pane mode — that surface kept
// its dropdown and its three companions — so the template is framed two ways.
import { X } from "lucide-react";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@platform/shadcn/ui/dialog";
import { Button } from "@platform/shadcn/ui/button";

export function McpDialog({
  src,
  folderName,
  onClose,
}: {
  // The mcp template's /render URL, aimed at the app folder.
  src: string;
  folderName: string;
  onClose: () => void;
}) {
  return (
    // Always open while mounted: the caller renders this behind `{open && …}`,
    // exactly as AppDoctorModal is, so the only close it can report is the user's.
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent
        // An explicit height, or the iframe (a replaced element with no intrinsic
        // size) collapses the grid row it sits in to nothing.
        className="grid-rows-[auto_minmax(0,1fr)] gap-3 overflow-hidden p-4 sm:max-w-[760px] h-[80vh]"
        showCloseButton={false}
      >
        <DialogHeader className="gap-1">
          <div className="flex items-start justify-between gap-2">
            <DialogTitle className="font-semibold">MCP config</DialogTitle>
            <DialogClose render={<Button variant="ghost" size="icon-sm" className="-mt-1 -mr-1" />}>
              <X />
              <span className="sr-only">Close</span>
            </DialogClose>
          </div>
          <DialogDescription>
            The MCP tools {folderName} publishes, and how a client connects to them.
          </DialogDescription>
        </DialogHeader>
        <iframe
          className="mcp-dialog-frame"
          src={src}
          title="MCP"
        />
      </DialogContent>
    </Dialog>
  );
}

export default McpDialog;
