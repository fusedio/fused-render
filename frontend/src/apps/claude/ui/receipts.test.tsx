// The receipt a sent turn wears, restored from the wire it was sent on, and the
// two overlays behind it (inventory 03 §C/§D/§E).
//
// The FIXTURE is the point of this suite: one composed message carrying all
// three blocks, read back exactly as a reopened session reads it — so a receipt
// that is visible while the session lasts is provably there when it is reopened.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { Receipts } = await import("./Receipts");
// The dialogs' BODIES: a Base UI dialog portals itself, and this suite has no
// document to portal into — the body is the whole content either way.
const { ShotViewerBody } = await import("./ShotViewer");
const { SentPopBody } = await import("./SentPop");
const { composeOutgoing, formatAnnotations, paneShotBlock, APP_STATE_TAG } = await import(
  "../protocol/wire"
);
type UserTurn = import("../protocol/controller-api").UserTurn;
type Receipt = import("../shots/types").Receipt;
type Viewable = import("./attachApi").Viewable;

/** A turn as a reopened session hands it over: nothing but `raw`. */
const OUTGOING = composeOutgoing("have a look at this", [
  "<" + APP_STATE_TAG + ">\n{}\n</" + APP_STATE_TAG + ">",
  paneShotBlock(
    [
      { kind: "overview", view: "/shots/20260908-over.png", viewNote: "the footer was cut off" },
      { kind: "pane", view: "/shots/20260908-view.png" },
      { kind: "image", view: "/shots/pasted.png", name: "pasted.png" },
      { kind: "file", view: "/home/me/data/rows.csv", name: "rows.csv", size: 4096 },
      { kind: "image", view: null, why: "could not be saved" },
    ],
    "preview",
  ),
  formatAnnotations(
    [
      { label: "A", kind: "element", tag: "button", content: "this is the wrong colour", t: 65 },
      { label: "B", kind: "point", x: 12, y: 40, content: "" },
    ],
    "file",
  ),
]);

const turn: UserTurn = { role: "user", key: "u:1", text: "have a look at this", raw: OUTGOING };

function mount(node: React.ReactElement) {
  let renderer: ReactTestRenderer | undefined;
  act(() => {
    renderer = create(node);
  });
  return renderer!;
}

/** ELEMENT nodes only: `findAll` also matches the composite component whose
 *  props carry the same className, and a composite has no `onClick` to fire. */
function els(root: ReactTestRenderer, className: string) {
  return root.root.findAll(
    (n) => typeof n.type === "string" && n.props.className === className,
  );
}

const panes = (root: ReactTestRenderer) => els(root, "annsum-pane");

function texts(root: ReactTestRenderer, className: string): string[] {
  return root.root
    .findAll((n) => typeof n.type === "string" && n.props.className === className)
    .map((n) => String(n.props.children));
}

test("a restored turn rebuilds every receipt row from its own wire (T:10903)", () => {
  const r = mount(<Receipts turn={turn} paneNoun="preview" onOpenShot={() => {}} probe={async () => false} />);
  expect(texts(r, "annsum-txt")).toEqual([
    "annotated overview attached",
    "screenshot attached",
    "image attached: pasted.png",
    "file attached: rows.csv",
    "no image — could not be saved",
    // The comment rows, with the user's own words (and nothing for the wordless
    // spot — the placeholder is a wire token, not a receipt's line).
    "this is the wrong colour",
    "",
  ]);
  // Pictures are drawn; a file and a refusal are not (an <img> pointed at a .csv
  // is a broken-image glyph, which reads as a bug).
  expect(panes(r).length).toBe(3);
});

test("a pruned copy is said in words, once the probe answers (T:10875)", async () => {
  const asked: Receipt[] = [];
  const r = mount(
    <Receipts
      turn={turn}
      paneNoun="preview"
      onOpenShot={() => {}}
      probe={async (receipt) => {
        asked.push(receipt);
        return true;
      }}
    />,
  );
  await act(async () => {
    await Promise.resolve();
  });
  // Only the rows with a path and nothing to draw have a question at all: the
  // file. A refusal has no path, and a picture's 404 announces itself.
  expect(asked.map((a) => a.view)).toEqual(["/home/me/data/rows.csv"]);
  expect(texts(r, "annsum-gone")).toEqual([" — file pruned"]);
});

test("a live send wears the receipts it was given, not a re-read of the wire", () => {
  const live: UserTurn = {
    ...turn,
    attachments: [
      { kind: "pane", label: "screenshot attached", view: "/shots/x.png", thumb: "blob:live" },
    ],
  };
  const r = mount(<Receipts turn={live} paneNoun="preview" onOpenShot={() => {}} probe={async () => false} />);
  expect(texts(r, "annsum-txt")).toEqual([
    "screenshot attached",
    "this is the wrong colour",
    "",
  ]);
  const img = r.root.findAll((n) => n.type === "img");
  expect(img[0]!.props.src).toBe("blob:live");
});

test("a turn that carried nothing draws no receipt", () => {
  const bare: UserTurn = { role: "user", key: "u:2", text: "hello", raw: "hello" };
  const r = mount(<Receipts turn={bare} paneNoun="preview" onOpenShot={() => {}} probe={async () => false} />);
  expect(r.toJSON()).toBe(null);
});

test("comments ride the message, so every row opens the popup instead (T:11068)", () => {
  let opened = 0;
  const r = mount(
    <Receipts
      turn={turn}
      paneNoun="preview"
      onOpenShot={() => {}}
      onShowSent={() => {
        opened += 1;
      }}
      probe={async () => false}
    />,
  );
  const note = els(r, "annsum-row annsum-note")[0]!;
  expect(note.props.role).toBe("button");
  expect(note.props.tabIndex).toBe(0);
  act(() => note.props.onClick());
  // Space and Enter reach it too: a div with an onclick is invisible to a
  // keyboard (T:11061-11065).
  let prevented = false;
  act(() =>
    note.props.onKeyDown({
      key: " ",
      preventDefault: () => {
        prevented = true;
      },
    }),
  );
  expect(prevented).toBe(true);
  // ... and so does the screenshot thumb, because the picture belongs to the
  // comments here.
  const thumb = panes(r)[0]!;
  act(() => thumb.props.onClick());
  expect(opened).toBe(3);
});

test("a picture-only send keeps the plain viewer on its thumb (T:11075)", () => {
  const pics = composeOutgoing("", [paneShotBlock([{ kind: "pane", view: "/shots/a.png" }], "preview")]);
  const only: UserTurn = { role: "user", key: "u:3", text: "pane screenshot", raw: pics };
  const shots: Viewable[] = [];
  const r = mount(
    <Receipts
      turn={only}
      paneNoun="preview"
      onOpenShot={(s) => shots.push(s)}
      onShowSent={() => {
        throw new Error("a send with no comments must not be wired to the popup");
      }}
      probe={async () => false}
    />,
  );
  const thumb = panes(r)[0]!;
  act(() => thumb.props.onClick());
  expect(shots.length).toBe(1);
  expect(shots[0]!.view).toBe("/shots/a.png");
});

// ---- the viewer ------------------------------------------------------------

const pending: Viewable = {
  kind: "pane",
  view: "/shots/view.png",
  src: "blob:v",
  viewNote: "the map did not render",
  pending: true,
};

test("the viewer shows nothing at all until it is given a picture", () => {
  const r = mount(<ShotViewerBody shot={null} paneNoun="preview" onClose={() => {}} />);
  expect(r.toJSON()).toBe(null);
});

test("the picture toggles fitted ⇄ actual size on click (T:10937)", () => {
  const r = mount(<ShotViewerBody shot={pending} paneNoun="preview" onClose={() => {}} />);
  const box = () => r.root.findByProps({ className: "c-shotview-box" });
  expect(box().props["data-zoom"]).toBe(undefined);
  act(() => r.root.findByProps({ className: "c-shotview-img" }).props.onClick());
  expect(box().props["data-zoom"]).toBe("");
  act(() => r.root.findByProps({ className: "c-shotview-img" }).props.onClick());
  expect(box().props["data-zoom"]).toBe(undefined);
});

test("Discard is offered for a PENDING shot only, and closes with it (T:4392)", () => {
  let discarded = 0;
  let closed = 0;
  const r = mount(
    <ShotViewerBody
      shot={pending}
      paneNoun="preview"
      onClose={() => (closed += 1)}
      onDiscard={() => (discarded += 1)}
    />,
  );
  const drop = () => els(r, "c-pill c-shotview-drop");
  expect(drop().length).toBe(1);
  act(() => drop()[0]!.props.onClick());
  expect(discarded).toBe(1);
  expect(closed).toBe(1);
  // A SENT picture is already in the agent's hands, and a Discard that cannot
  // un-send it would be a lie.
  act(() => {
    r.update(
      <ShotViewerBody
        shot={{ ...pending, pending: false }}
        paneNoun="preview"
        onClose={() => {}}
        onDiscard={() => {}}
      />,
    );
  });
  expect(els(r, "c-pill c-shotview-drop").length).toBe(0);
});

test("the caveat and the path are shown where the pixels are (T:1094, 1084)", () => {
  const r = mount(<ShotViewerBody shot={pending} paneNoun="preview" onClose={() => {}} />);
  expect(texts(r, "c-shotview-note")).toEqual(["the map did not render"]);
  expect(texts(r, "c-shotview-path c-mono")).toEqual(["/shots/view.png"]);
  // A picture says its own name by being shown, so the file stand-in stays away.
  expect(els(r, "c-shotview-name").length).toBe(0);
});

test("a FILE has no pixels, so it gets its name, its size and its own template", async () => {
  const file: Viewable = {
    kind: "file",
    view: "/home/me/data/rows.csv",
    name: "rows.csv",
    size: 4096,
    pending: false,
  };
  const r = mount(<ShotViewerBody shot={file} paneNoun="preview" onClose={() => {}} />);
  expect(texts(r, "c-shotview-name")).toEqual(["rows.csv · 4 KB"]);
  // The line that promises a preview is up while the stat is in flight: a blank
  // box for those seconds reads as a preview that failed (T:4384).
  expect(texts(r, "c-shotview-loading")).toEqual(["loading preview…"]);
  expect(r.root.findAll((n) => n.type === "img").length).toBe(0);
  // No stat answer here (no server): the promise settles to null and the line
  // goes, which is the D616 "no template for this extension" case.
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
});

// ---- "what was sent" -------------------------------------------------------

test("the popup's sections are the wire's, in T's order (T:10973-11040)", () => {
  const r = mount(<SentPopBody outgoing={OUTGOING} paneNoun="preview" />);
  const heads = r.root.findAll((n) => n.type === "h4").map((n) => String(n.props.children));
  expect(heads).toEqual([
    "Overview screenshot (badges mark each comment)",
    // One per other picture, named by what it IS (`shotNoun`).
    "preview screenshot",
    "pasted.png",
    "rows.csv",
    "pasted image",
    "Comments",
    "Exact message the agent received",
  ]);
  // The overview's caveat is prefixed, because there is a picture above it.
  expect(texts(r, "c-sent-caveat")).toEqual(["caveat: the footer was cut off"]);
  // A file is never drawn, and neither is a refusal.
  expect(els(r, "c-sent-shot").length).toBe(3);
});

test("each comment carries its label, its words and its context", () => {
  const r = mount(<SentPopBody outgoing={OUTGOING} paneNoun="preview" />);
  expect(texts(r, "c-sent-note-lbl")).toEqual(["A", "B"]);
  expect(texts(r, "c-sent-note-meta")).toEqual(["<button> · at 1:05", "exact spot (12, 40)"]);
  // A spot the user marked without saying anything still gets a row.
  const rows = els(r, "c-sent-note-row");
  expect(String(rows[1]!.props.children[1].props.children)).toBe("(no words)");
});

test("the exact message is the wire itself, blocks and all", () => {
  const r = mount(<SentPopBody outgoing={OUTGOING} paneNoun="preview" />);
  const pre = r.root.findByProps({ className: "c-sent-wire" });
  expect(String(pre.props.children)).toBe(OUTGOING);
});

test("a send with no blocks still has the one section that always exists", () => {
  const r = mount(<SentPopBody outgoing="just words" />);
  expect(r.root.findAll((n) => n.type === "h4").map((n) => String(n.props.children))).toEqual([
    "Exact message the agent received",
  ]);
});

test("a picture inside the popup opens the viewer over it (T:1147)", () => {
  const shots: Viewable[] = [];
  const r = mount(
    <SentPopBody
      outgoing={OUTGOING}
      paneNoun="preview"
      onOpenShot={(s) => shots.push(s)}
    />,
  );
  const img = els(r, "c-sent-shot")[0]!;
  act(() => img.props.onClick());
  expect(shots.length).toBe(1);
  expect(shots[0]!.view).toBe("/shots/20260908-over.png");
  expect(shots[0]!.pending).toBe(false);
});
