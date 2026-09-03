// "New task" — the /tasks page's own way to create a scheduled message,
// alongside the chat composer's Send now pill. This form serves the
// calendar-first direction, so it has to ask for the folder too.
//
// The layout is Google Calendar's new-event card, copied deliberately (Akshil,
// 2026-08-14): a big borderless title, one where-row, and everything else
// behind a collapsed More options. The trick that keeps it that small is also
// Google's: the REPEAT choices are derived from the picked date-time ("Weekly
// on Monday" because the date IS a Monday), so recurrence needs no fields of
// its own — only "Custom…" reveals a panel.
//
// This file is the orchestrator: the state, the effects and the wiring. The
// rows it renders and the pure rules it applies live beside it in new-task/.
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { ChevronRightIcon, ShieldIcon } from "lucide-react";
import {
  cancelScheduledMessage,
  getClaudeSessionFolders,
  getConfig,
  getTasks,
  listDir,
  scheduleMessage,
  statPath,
  uploadTaskShot,
} from "@platform/lib/api";
import type { RecurrenceRule, ScheduledMessage } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import { isMod } from "@platform/lib/platform";
import { Alert, AlertDescription } from "@platform/shadcn/ui/alert";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { cn } from "@platform/lib/utils";
import { describeRepeats, describeRule, repeatChoicesFor } from "../schedule-lib";
import {
  applyRepeatToggle,
  buildSchedulePayload,
  defaultTargetOf,
  deleteActionFor,
  deleteFailureText,
  deletePress,
  initialAskOf,
  initialRepeatKey,
  initialTitleStateOf,
  keyOfRule,
  learnedSessionOf,
  pastNoteFor,
  permissionLabel,
  saveActionLabel,
  saveBlockedReason,
  saveEnabled,
  sessionTitleOf,
  splitDraft,
  toLocalInput,
  ASK_PLACEHOLDER,
  TITLE_PLACEHOLDER,
} from "./form-logic";
import {
  normPath,
  readRecents,
  rememberRecent,
  splitTargetPath,
  targetVerdict,
  type TargetVerdict,
} from "./paths";
import {
  attachmentKindOf,
  restoredAttachments,
  taskPreviewSrcFor,
  DRAWABLE_MIMES,
  type TaskImage,
} from "./attachments";
import { DAYS, MONTHS } from "./when-lib";
import { AttachmentTiles } from "./AttachmentTiles";
import { AttachmentViewer } from "./AttachmentViewer";
import { CheckRow } from "./CheckRow";
import { ChoiceSelect } from "./ChoiceSelect";
import { CustomRecurrence } from "./CustomRecurrence";
import { ExplorerPanel } from "./ExplorerPanel";
import { FolderField } from "./FolderField";
import { Footer } from "./Footer";
import { IconRow } from "./IconRow";
import { TaskDialog } from "./TaskDialog";
import { WhenRow } from "./WhenRow";

// The APP's recents: the top five folders from the same call the home page's
// Claude Sessions strip makes, newest session first — what a person means by
// "recent" (Akshil, 2026-08-18).
const SESSION_FOLDERS_SHOWN = 5;

export default function NewJobModal({
  initialTime,
  initialTarget,
  initialMessage,
  chatSessionId,
  chatBack,
  editing,
  permissionModes,
  recentTargets,
  planning = false,
  onClose,
  onCreated,
}: {
  // From a calendar slot click, or null from the New task button.
  initialTime: Date | null;
  // From a deep link that ALREADY knows the folder (/tasks?new=1&target=…).
  // Outranks defaultTargetOf(); an Edit outranks both.
  initialTarget?: string | null;
  // The chat composer's handoff: the draft arrives as the task's prose and is
  // SPLIT across the two fields (splitDraft); Save composes it back.
  initialMessage?: string | null;
  // The open conversation, to CONTINUE — a one-off resumes it; a repeat never
  // does, because resuming the same chat every day compounds context forever.
  chatSessionId?: string | null;
  // The chat's own URL, so the form can offer the way back.
  chatBack?: string | null;
  // An existing task to change. The server has no update: saving schedules the
  // replacement first, then withdraws this one — see submit().
  editing?: ScheduledMessage | null;
  permissionModes: string[];
  // Folders existing tasks already point at, newest first — padding for the
  // recents list on a fresh machine.
  recentTargets?: string[];
  // IS THIS CARD BEING USED TO PLAN? True on the calendar and on any opening
  // that arrived with a time; false from the List and the Board. Moves ONE
  // thing: whether the when-row starts on the card's face or folded into More
  // options. `timePicked` below is the separate answer to "is this scheduled".
  planning?: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  // Held in consts because the BASELINE (`initial`) has to be the identical
  // value, or an Edit opens looking dirty and its ✕ arms the close guard on an
  // untouched card (QA 2026-08-14).
  const initialAsk = initialAskOf(editing, initialMessage);
  const draft = splitDraft(initialMessage);
  const [message, setMessage] = useState(initialAsk);

  // ---- Attachments ---------------------------------------------------------
  // THE REF IS THE AUTHORITY, the state is its mirror for rendering (Bugbot,
  // PR #865): Save awaits the uploads and then reads the paths they wrote; a
  // setState updater only reaches `images` on the next RENDER, so a
  // drop-then-immediate-Save read an empty path. `applyImages` writes the ref
  // synchronously, then mirrors.
  const [images, setImages] = useState<TaskImage[]>(() => restoredAttachments(editing));
  const imagesRef = useRef<TaskImage[]>(images);
  // Every `blob:` thumbnail is revoked when the form unmounts (bugbot, #915).
  useEffect(() => () => {
    for (const i of imagesRef.current) if (i.thumb) URL.revokeObjectURL(i.thumb);
  }, []);
  const applyImages = useCallback((fn: (prev: TaskImage[]) => TaskImage[]) => {
    imagesRef.current = fn(imagesRef.current);
    setImages(imagesRef.current);
  }, []);
  // Keys for images attached in THIS session, clear of the edit-seeded 0..n.
  const imageKey = useRef(1000);
  // Every in-flight UPLOAD, so Save can await the stragglers. Registered
  // SYNCHRONOUSLY with the chip it belongs to (Bugbot, PR #865).
  const pendingRef = useRef<Set<Promise<void>>>(new Set());
  const attachFiles = useCallback((files: FileList | File[] | null) => {
    const picked = [...(files ?? [])];
    if (!picked.length) return;
    picked.forEach((file) => {
      const key = imageKey.current++;
      // The chip's kind is a GUESS until the upload answers, and the guess is
      // only "can this engine draw it". MIME first (a pasted screenshot has no
      // filename), extension second (a drop off a NAS often has no type).
      const drawable = DRAWABLE_MIMES.has(file.type)
        || (!!file.name && attachmentKindOf(file.name) === "image");
      // `blob:` rather than a FileReader's data URL: no read to await before
      // the chip appears and no base64 copy held in memory.
      const thumb = drawable ? URL.createObjectURL(file) : null;
      applyImages((prev) => [...prev, {
        key,
        path: "",
        kind: drawable ? "image" : "file",
        name: file.name || "attachment",
        thumb,
      }]);
      const pending: Promise<void> = uploadTaskShot(file)
        .then((up) => applyImages((prev) => prev.map((i) => {
          if (i.key !== key) return i;
          // The server's `kind` is trusted only where the PATH it stored can be
          // drawn: a `.tif` whose transcode failed comes back `kind: "image"`
          // on the original bytes, and an <img> of those is an empty box
          // (bugbot, #915). The blob thumb is kept where the browser drew one.
          const kind = up.kind === "image" && attachmentKindOf(up.path) === "image"
            ? "image" : (i.thumb ? "image" : "file");
          return { ...i, path: up.path, kind };
        })))
        .catch((e) => {
          // A failed upload takes its chip with it — an attachment on the card
          // that would not reach the task is the lie to avoid.
          if (thumb) URL.revokeObjectURL(thumb);
          applyImages((prev) => prev.filter((i) => i.key !== key));
          setError((e as Error).message || "attachment upload failed");
        })
        .finally(() => pendingRef.current.delete(pending));
      pendingRef.current.add(pending);
    });
  }, [applyImages]);

  // The open attachment, if any. The viewer holds a KEY and reads the entry
  // out of `images` on every render: an upload finishes AFTER the click that
  // opened its chip, and a snapshot taken at click time would sit on
  // "uploading…" for ever (bugbot, #915).
  const [viewerKey, setViewerKey] = useState<number | null>(null);
  const viewer = useMemo<TaskImage | null>(
    () => (viewerKey === null ? null : images.find((i) => i.key === viewerKey) ?? null),
    [images, viewerKey]);
  const [viewerZoom, setViewerZoom] = useState(false);
  const [previewSrc, setPreviewSrc] = useState<string | null>(null);
  const [previewWait, setPreviewWait] = useState(false);
  const [frameLoaded, setFrameLoaded] = useState(false);
  const closeViewer = useCallback(() => setViewerKey(null), []);
  useEffect(() => {
    setPreviewSrc(null);
    setFrameLoaded(false);
    if (!viewer || viewer.kind !== "file" || !viewer.path) {
      setPreviewWait(false);
      return;
    }
    // Identity, not a path compare: a late answer must not frame the previous
    // file.
    let live = true;
    setPreviewWait(true);
    statPath(viewer.path)
      .then((st) => { if (live) setPreviewSrc(taskPreviewSrcFor(st, viewer.path)); })
      .catch(() => { if (live) setPreviewSrc(null); })
      .finally(() => { if (live) setPreviewWait(false); });
    return () => { live = false; };
    // The three fields the stat depends on, not `viewer` itself: the object is
    // re-derived on every `images` change, and a re-stat of an unchanged file
    // would blank a frame that was showing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewer?.key, viewer?.kind, viewer?.path]);

  // ONE paste handler for both text fields; only a FILE paste is intercepted —
  // ordinary text pastes stay exactly what they were. ANY file kind (D618).
  const pasteFiles = useCallback(
    (e: React.ClipboardEvent) => {
      const files = [...e.clipboardData.items]
        .filter((i) => i.kind === "file")
        .map((i) => i.getAsFile())
        .filter((f): f is File => !!f);
      if (files.length) {
        e.preventDefault();
        attachFiles(files);
      }
    },
    [attachFiles],
  );

  // ---- Title, target, when -------------------------------------------------
  // The FIRST field on the card, and REQUIRED (Akshil, 2026-08-17). Only the
  // synchronous half of the precedence: a usable stored title, else the chat
  // draft's first line, else blank until the /api/tasks lookup answers. Which
  // value the field opens on and whether the lookup may run are one decision,
  // taken once — see initialTitleStateOf.
  const nameSession = (editing?.session_id || chatSessionId) ?? "";
  const { title: derivedTitle, lookupSession: titleLookup } =
    initialTitleStateOf(editing, nameSession, draft.title);
  const [title, setTitle] = useState(derivedTitle);
  const [target, setTarget] = useState(editing?.target ?? initialTarget ?? "");
  // ONE date-time drives everything: a one-off runs at it, and every derived
  // repeat choice reads its parts — Google's model. THE DEFAULT IS NOW (Akshil,
  // 2026-08-18): a task typed into this card is overwhelmingly one to RUN.
  const [when, setWhen] = useState(() =>
    toLocalInput(editing?.due ? new Date(editing.due) : (initialTime ?? new Date())),
  );
  // DID ANYBODY PICK THIS TIME? Set by the date grid, the time field and the
  // Repeat tick — the three controls that mean "I have an opinion about when".
  // A card left on its default from the List/Board is a task to RUN, and the
  // calendar knows not to draw it (Akshil, 2026-08-23).
  const [timePicked, setTimePicked] = useState(
    () => (editing ? !editing.immediate : planning),
  );
  const [moreOpen, setMoreOpen] = useState(planning);
  const [repeat, setRepeat] = useState<string>(() => initialRepeatKey(editing));
  const [customRule, setCustomRule] = useState<RecurrenceRule | null>(() =>
    editing?.rule && keyOfRule(editing.rule, new Date(editing.due)) === "custom"
      ? editing.rule
      : null,
  );
  // Repeat is a CHECKBOX, and the dropdown only exists while it is ticked
  // (design §6). Editing a repeating task opens ticked.
  const [repeatOn, setRepeatOn] = useState(() => initialRepeatKey(editing) !== "none");
  const [newTaskEachRun, setNewTaskEachRun] = useState(
    () => editing?.new_task_each_run ?? false,
  );
  const legacyCron = editing?.repeats ?? "";
  const learnedSession = learnedSessionOf(editing);
  const [recurOpen, setRecurOpen] = useState(false);
  const repeatBefore = useRef(repeat);
  const [permission, setPermission] = useState(editing?.permission_mode || "auto");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  // Which of the two dropdown verbs opened the panel: Browse lands on the
  // listing, "+ New folder" lands on the listing WITH the naming row typing.
  const [pickerNaming, setPickerNaming] = useState(false);
  const openPicker = (naming = false) => {
    setPickerNaming(naming);
    // Displacing an open Custom panel runs its CANCEL semantics, not a bare
    // close: "Custom…" chosen but never Done'd would otherwise strand the
    // select on a choice with no rule (Bugbot, PR #548).
    setRecurOpen((open) => {
      if (open && !customRule)
        setRepeat((r) => (r === "custom" ? repeatBefore.current : r));
      return false;
    });
    setPicking(true);
  };
  const closePicker = () => setPicking(false);
  const closeRecur = () => setRecurOpen(false);
  const openRecur = () => {
    setPicking(false);
    setRecurOpen(true);
  };
  // The Repeat tick. Unticking puts the key back to "none" AND drops the
  // custom rule, so the rule the form submits really is gone rather than
  // merely hidden. The flag under it goes with it.
  const toggleRepeat = (on: boolean) => {
    const next = applyRepeatToggle(on, { repeat, customRule });
    setRepeat(next.repeat);
    setCustomRule(next.customRule);
    setRepeatOn(on);
    if (on) setTimePicked(true);
    if (!on) {
      setNewTaskEachRun(false);
      if (recurOpen) closeRecur();
    }
  };
  const [home, setHome] = useState("");

  // ---- Recents: three tiers, in this order ---------------------------------
  //  1. the APP's recents (session folders), 2. this form's own localStorage
  //  memory, 3. the folders existing tasks point at. Tier 2 is RE-READ every
  //  time the list opens: it changes while the card is up (Browse writes the
  //  folder you pick).
  const [recentsOpen, setRecentsOpen] = useState(false);
  const [recents, setRecents] = useState<string[]>([]);
  const [sessionFolders, setSessionFolders] = useState<string[]>([]);
  useEffect(() => {
    let alive = true;
    getClaudeSessionFolders().then(
      (r) => {
        if (!alive) return;
        setSessionFolders(
          r.folders.slice(0, SESSION_FOLDERS_SHOWN).map((f) => normPath(f.path)),
        );
      },
      () => {},
    );
    return () => {
      alive = false;
    };
  }, []);
  const readRecentList = useCallback(() => {
    const seen = new Set<string>();
    return [
      ...sessionFolders,
      ...readRecents(),
      ...(recentTargets ?? []),
    ].filter((p) => {
      if (!p || seen.has(p)) return false;
      seen.add(p);
      return true;
    });
  }, [recentTargets, sessionFolders]);
  const openRecents = useCallback(() => {
    setRecents(readRecentList());
    setRecentsOpen(true);
  }, [readRecentList]);
  const closeRecents = useCallback(() => setRecentsOpen(false), []);

  // Early path validation (Akshil, 2026-08-16): a beat after typing stops, ask
  // the server whether the path exists. A folder answers listDir directly; a
  // FILE fails it, so the parent is listed and the basename looked up — a file
  // target is legal, and so is ONE folder that isn't there yet (targetVerdict).
  const [pathError, setPathError] = useState<string | null>(null);
  const [newFolder, setNewFolder] = useState<string | null>(null);
  useEffect(() => {
    const p = target.trim();
    if (!p) {
      setPathError(null);
      setNewFolder(null);
      return;
    }
    let stale = false;
    // The last verdict stays on screen until the next one resolves, so the
    // note does not blink off and back on between keystrokes.
    const settle = (v: TargetVerdict) => {
      if (stale) return;
      setPathError(v.kind === "bad" ? v.text : null);
      setNewFolder(v.kind === "new-folder" ? v.name : null);
    };
    const timer = window.setTimeout(() => {
      listDir(p).then(
        () => settle({ kind: "ok" }),
        () => {
          const { parent } = splitTargetPath(p);
          listDir(parent).then(
            (r) => settle(targetVerdict(p, r.entries.map((e) => e.name))),
            () => settle(targetVerdict(p, null)),
          );
        },
      );
    }, 400);
    return () => {
      stale = true;
      window.clearTimeout(timer);
    };
  }, [target]);

  const askRef = useRef<HTMLTextAreaElement>(null);
  const titleRef = useRef<HTMLInputElement>(null);
  const pathRef = useRef<HTMLInputElement>(null);
  const suppressOpen = useRef(false);

  // The default target, filled once the server says where home is — and only
  // into a still-empty field. The BASELINE moves with it: a default the user
  // did not type must not read as dirty (QA 2026-08-14).
  useEffect(() => {
    getConfig().then(
      (c) => {
        setHome(c.home);
        if (!editing) {
          const fallback = defaultTargetOf(c);
          setTarget((prev) => (prev === "" ? fallback : prev));
          setInitial((prev) =>
            prev.target === "" ? { ...prev, target: fallback } : prev,
          );
        }
      },
      () => undefined,
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // What the form OPENED with. The dirty guard must fire on "the user typed
  // something", not "the fields are non-empty".
  const [initial, setInitial] = useState(() => ({
    target: editing?.target ?? initialTarget ?? "",
    message: initialAsk,
    title,
    when,
    repeat,
    repeatOn,
    newTaskEachRun,
    customRule: JSON.stringify(customRule),
    permission,
    // Paths only: a fresh attach reads as dirty from the moment it lands.
    images: (editing?.images ?? []).join("\n"),
  }));
  // EVERY field the form can lose arms the guard (Bugbot, PR #538).
  const dirty =
    target !== initial.target ||
    message !== initial.message ||
    title !== initial.title ||
    when !== initial.when ||
    repeat !== initial.repeat ||
    repeatOn !== initial.repeatOn ||
    newTaskEachRun !== initial.newTaskEachRun ||
    JSON.stringify(customRule) !== initial.customRule ||
    permission !== initial.permission ||
    images.map((i) => i.path || "pending").join("\n") !== initial.images;

  const picked = useMemo(() => new Date(when), [when]);
  const pickedOk = !Number.isNaN(picked.getTime());

  // Ids so the lines this form prints are ATTACHED to the controls they are
  // about (audit 2026-08-16).
  const pastHintId = useId();
  const pathErrorId = useId();
  const newFolderId = useId();
  const threadHintId = useId();

  const dateLabel = pickedOk
    ? `${DAYS[picked.getDay()]}, ${MONTHS[picked.getMonth()]} ${picked.getDate()}` +
      (picked.getFullYear() === new Date().getFullYear() ? "" : `, ${picked.getFullYear()}`)
    : "Pick a date";

  // Both setters mark the time as PICKED — marked even when the new value
  // equals the old one: reopening the grid and clicking today is still an
  // answer to the question.
  const setDatePart = (d: Date) => {
    const t = pickedOk ? picked : new Date();
    setWhen(toLocalInput(new Date(
      d.getFullYear(), d.getMonth(), d.getDate(), t.getHours(), t.getMinutes(),
    )));
    setTimePicked(true);
  };
  const setTimePart = (h: number, m: number) => {
    const d = pickedOk ? picked : new Date();
    setWhen(toLocalInput(new Date(
      d.getFullYear(), d.getMonth(), d.getDate(), h, m,
    )));
    setTimePicked(true);
  };

  // The structured rule the current choice means; null for a one-off (and for
  // "cron", whose legacy line is submitted verbatim instead).
  const choices = useMemo(
    () => repeatChoicesFor(pickedOk ? picked : new Date()),
    [picked, pickedOk],
  );
  const rule: RecurrenceRule | null = useMemo(() => {
    if (!repeatOn) return null;
    if (repeat === "custom") return customRule;
    if (repeat === "cron" || repeat === "none") return null;
    return choices.find((c) => c.key === repeat)?.rule ?? null;
  }, [repeatOn, repeat, customRule, choices]);

  // Back to chat honours the SAME two-step dirty guard as the ✕ (Bugbot,
  // PR #548). First click re-labels the button for 2s; the second within that
  // window really leaves.
  const [backConfirm, setBackConfirm] = useState(false);
  const backTimer = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (backTimer.current !== null) window.clearTimeout(backTimer.current);
    },
    [],
  );
  const backToChat = () => {
    if (!chatBack) return;
    if (dirty && !backConfirm) {
      setBackConfirm(true);
      if (backTimer.current !== null) window.clearTimeout(backTimer.current);
      backTimer.current = window.setTimeout(() => setBackConfirm(false), 2000);
      return;
    }
    navigateUrl(chatBack);
  };

  // The replacement was created but the original could not be withdrawn: the
  // one state where pressing Save again would mint a THIRD copy.
  const [replaced, setReplaced] = useState(false);

  // ---- The session's own name, prefilled into Title -------------------------
  // Steps 2 and 3 of the precedence (sessionTitleOf), from /api/tasks: a
  // session IS a task there, so the row keyed on it carries the resolved title
  // AND its provenance. Only ever replaces the SYNCHRONOUS title, and `initial`
  // moves with it so the prefill does not read as dirty.
  useEffect(() => {
    if (!titleLookup) return;
    let alive = true;
    getTasks()
      .then(({ tasks }) => {
        const resolved = sessionTitleOf(tasks, titleLookup);
        if (!alive || !resolved) return;
        setTitle((prev) => (prev === derivedTitle ? resolved : prev));
        setInitial((prev) =>
          prev.title === derivedTitle ? { ...prev, title: resolved } : prev,
        );
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [titleLookup, derivedTitle]);

  // ---- Delete --------------------------------------------------------------
  // Only when EDITING, and only for something the server will actually
  // withdraw — see deleteActionFor. Two presses, 2s window.
  const del = deleteActionFor(editing);
  const [delConfirm, setDelConfirm] = useState(false);
  const delTimer = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (delTimer.current !== null) window.clearTimeout(delTimer.current);
    },
    [],
  );
  const remove = async () => {
    const press = deletePress(del, delConfirm);
    if (press === null || del === null) return;
    if (press.do === "arm") {
      setDelConfirm(true);
      if (delTimer.current !== null) window.clearTimeout(delTimer.current);
      delTimer.current = window.setTimeout(() => setDelConfirm(false), 2000);
      return;
    }
    if (delTimer.current !== null) window.clearTimeout(delTimer.current);
    setDelConfirm(false);
    setBusy(true);
    setError(null);
    try {
      // A TEMPLATE id here is the whole point: the server cancels the rule AND
      // its materialized next run, which is what "stop this recurring job"
      // means.
      await cancelScheduledMessage(press.id);
      onCreated();
      onClose();
    } catch (e) {
      // A 404 is not a failure: the entry really is gone, so the page is
      // re-read and the modal stays open only long enough to say why.
      if ((e as { status?: number }).status === 404) onCreated();
      setBusy(false);
      setError(deleteFailureText(e, del.series));
    }
  };

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      // A drop the instant before Save must still make the task: wait out the
      // in-flight uploads, then read the paths off the ref.
      await Promise.all([...pendingRef.current]);
      await scheduleMessage(
        buildSchedulePayload({
          target,
          message,
          title,
          when,
          rule,
          repeat,
          legacyCron,
          permission,
          // The two sources are kept APART: the task's OWN thread (learned)
          // survives a repeat, a CHAT's does not.
          sessionId: (!learnedSession && editing?.session_id) || chatSessionId || "",
          learnedSessionId: learnedSession,
          newTaskEachRun,
          replacesEntryId: editing?.id ?? "",
          timePicked,
          images: imagesRef.current.map((i) => i.path).filter(Boolean),
          attachments: imagesRef.current
            .filter((i) => i.path)
            .map((i) => ({ path: i.path, name: i.name, kind: i.kind })),
        }),
      );
      rememberRecent(target);
      if (editing) {
        // Replacement first, THEN withdraw — a failed create must never leave
        // the user with neither task. A 404 here is the fine race; anything
        // ELSE is old and new both armed, and is said out loud.
        try {
          await cancelScheduledMessage(editing.id);
        } catch (e) {
          if ((e as Error & { status?: number }).status !== 404) {
            onCreated();
            setReplaced(true);
            setBusy(false);
            setError(
              "The new task is saved, but the old one couldn't be withdrawn — " +
              "cancel it from the list so it doesn't also run.",
            );
            return;
          }
        }
      }
      onCreated();
      onClose();
    } catch (e) {
      // The server's 400s are written for a human — show them verbatim.
      setError((e as Error).message);
      setBusy(false);
    }
  };

  // A past time is not refused (design §9); what is left is a NOTE saying
  // which of the two things happens — see pastNoteFor.
  const pastNote = pastNoteFor(pickedOk ? picked : null, repeatOn, rule, new Date());

  const gate = {
    message,
    title,
    target,
    pathError,
    repeatOn,
    repeat,
    customRule,
    legacyCron,
    pickedOk,
    replaced,
  };
  const ready = saveEnabled(gate);
  const actionLabel = saveActionLabel(pickedOk ? picked : null, repeatOn, new Date());

  // What a press does when the form is not ready: say the first thing that is
  // missing and put the caret in it.
  const trySubmit = () => {
    const blocked = saveBlockedReason(gate);
    if (!blocked) {
      submit();
      return;
    }
    setError(blocked.text);
    const el = blocked.field === "title" ? titleRef.current
      : blocked.field === "target" ? pathRef.current
        : null;
    el?.focus();
  };

  const side = picking ? (
    <ExplorerPanel
      // Keyed by which verb opened it, so "+ New folder" always arrives
      // naming even when it displaces a Browse panel.
      key={pickerNaming ? "naming" : "browse"}
      startNaming={pickerNaming}
      start={target.trim() || home || "/"}
      onPick={(p) => {
        setTarget(p);
        rememberRecent(p);
      }}
      // A folder NAMED in the picker hands focus back to the field — WITHOUT
      // popping the list over the form (Akshil, 2026-08-20).
      onName={() => {
        suppressOpen.current = true;
        window.setTimeout(() => pathRef.current?.focus(), 0);
      }}
      onClose={closePicker}
    />
  ) : recurOpen ? (
    <CustomRecurrence
      initial={customRule}
      anchor={pickedOk ? picked : new Date()}
      onDone={(r) => {
        setCustomRule(r);
        closeRecur();
      }}
      onCancel={() => {
        closeRecur();
        // No rule was committed: fall back — but only if the select still
        // says "Custom…" (Bugbot, PR #548).
        if (!customRule)
          setRepeat((r) => (r === "custom" ? repeatBefore.current : r));
      }}
    />
  ) : null;

  const repeatValue =
    repeat === "custom" && customRule
      ? describeRule(customRule, pickedOk ? picked : new Date())
      : repeat === "cron"
        ? describeRepeats(legacyCron)
        : choices.find((c) => c.key === repeat)?.label ?? "Does not repeat";

  return (
    <TaskDialog
      title={editing ? "Edit task" : "New task"}
      onClose={onClose}
      busy={busy}
      dirty={dirty}
      initialFocus={titleRef}
      side={side}
      // ⌘↩ / Ctrl+Enter SUBMITS, from any field (Akshil, 2026-08-27). Plain
      // Enter has a job in every box here, so the commit needs the modifier.
      // Left alone inside the folder explorer, whose own Enter picks a row.
      onKeyDown={(e) => {
        if (e.key !== "Enter" || !isMod(e) || busy) return;
        if ((e.target as HTMLElement).closest("[data-new-task-explorer]")) return;
        e.preventDefault();
        trySubmit();
      }}
      footer={
        <Footer
          del={del}
          delArmed={delConfirm}
          onDelete={remove}
          chatBack={chatBack}
          backArmed={backConfirm}
          onBack={backToChat}
          busy={busy}
          ready={ready}
          actionLabel={actionLabel}
          onSubmit={trySubmit}
        />
      }
    >
      <div className="flex flex-col gap-2">
        {/* ONE WRITING SURFACE, not two controls (Akshil, 2026-08-17): the
            title and the description share a single borderless area, the title
            set large and the description quieter beneath it. The wrapper is the
            drop target — the part of the card that reads as "the message". */}
        <div
          className="flex flex-col gap-1 pb-1"
          onDragOver={(e) => {
            if ([...e.dataTransfer.items].some((i) => i.kind === "file")) {
              e.preventDefault();
            }
          }}
          onDrop={(e) => {
            if (e.dataTransfer.files?.length) {
              e.preventDefault();
              attachFiles(e.dataTransfer.files);
            }
          }}
        >
          {/* TITLE FIRST, and the prominent one. REQUIRED — it is both the
              task's name and the first line of what Claude is sent
              (composeTaskMessage). An <input>, not a textarea: one line that
              never wraps. `new-task-title` is a DOM hook for the tasks tour. */}
          <input
            ref={titleRef}
            type="text"
            className="new-task-title w-full truncate border-0 bg-transparent px-0 py-1 text-lg font-semibold outline-none placeholder:text-muted-foreground/70"
            aria-label="What should Claude do?"
            aria-required="true"
            onPaste={pasteFiles}
            placeholder={TITLE_PLACEHOLDER}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            // ENTER MOVES DOWN, into the instructions (Akshil, 2026-08-27). It
            // does NOT submit. An IME composition's Enter is left alone, and a
            // MODIFIED Enter is the form's Save chord — it bubbles untouched.
            onKeyDown={(e) => {
              if (e.key !== "Enter" || e.nativeEvent.isComposing || isMod(e)) return;
              e.preventDefault();
              askRef.current?.focus();
            }}
          />
          {/* …and the ADDITIONAL INSTRUCTIONS second: OPTIONAL, the body under
              the title. Grows with the text (field-sizing) up to a cap, then
              scrolls. `new-task-ask` is a DOM hook for the tasks tour. */}
          <Textarea
            ref={askRef}
            className="new-task-ask min-h-14 max-h-56 resize-none rounded-none border-0 bg-transparent px-0 py-0.5 shadow-none focus-visible:ring-0 dark:bg-transparent"
            rows={2}
            aria-label="Additional instructions"
            placeholder={ASK_PLACEHOLDER}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            onPaste={pasteFiles}
          />
          <AttachmentTiles
            images={images}
            onOpen={(img) => {
              setViewerKey(img.key);
              setViewerZoom(false);
            }}
            onRemove={(img) => {
              if (img.thumb) URL.revokeObjectURL(img.thumb);
              applyImages((prev) => prev.filter((i) => i.key !== img.key));
              setViewerKey((k) => (k === img.key ? null : k));
            }}
          />
          {viewer && (
            <AttachmentViewer
              viewer={viewer}
              zoom={viewerZoom}
              onToggleZoom={() => setViewerZoom((z) => !z)}
              previewSrc={previewSrc}
              previewWait={previewWait}
              frameLoaded={frameLoaded}
              onFrameLoad={() => setFrameLoaded(true)}
              onClose={closeViewer}
            />
          )}
        </div>

        <FolderField
          target={target}
          onChange={setTarget}
          pathRef={pathRef}
          pathError={pathError}
          pathErrorId={pathErrorId}
          newFolder={newFolder}
          newFolderId={newFolderId}
          recents={recents}
          open={recentsOpen}
          onOpen={openRecents}
          onClose={closeRecents}
          suppressOpen={suppressOpen}
          onBrowse={() => openPicker()}
          onNewFolder={() => openPicker(true)}
        />

        {/* WHEN, and everything that hangs off it, lives behind a disclosure
            (Akshil, 2026-08-23) — open from the start when the card is being
            used to plan, folded away otherwise. State rather than a bare `open`
            attribute: `<details>` keeps its own openness in the DOM. */}
        <details
          className="group/more"
          open={moreOpen}
          onToggle={(e) => setMoreOpen(e.currentTarget.open)}
        >
          <summary className="flex w-fit cursor-pointer list-none items-center gap-1 rounded-md py-1 pr-1 text-xs text-muted-foreground select-none hover:text-foreground [&::-webkit-details-marker]:hidden">
            <ChevronRightIcon
              aria-hidden="true"
              className="size-3.5 transition-transform group-open/more:rotate-90 motion-reduce:transition-none"
            />
            More options
          </summary>
          <div className="flex flex-col gap-2 pt-1">
            <WhenRow
              picked={picked}
              pickedOk={pickedOk}
              dateLabel={dateLabel}
              describedBy={pastNote ? pastHintId : undefined}
              onDate={setDatePart}
              onTime={setTimePart}
              repeatOn={repeatOn}
              onRepeat={toggleRepeat}
            />
            {/* Not a refusal: past-due work is queued and runs when the app next
                opens (design §9). `role="status"` because ticking Repeat rewrites
                this line in place. */}
            {pastNote && (
              <IconRow>
                <p id={pastHintId} className="text-xs text-muted-foreground" role="status">
                  {pastNote}
                </p>
              </IconRow>
            )}
            {repeatOn && (
              <IconRow>
                <div className="flex flex-col gap-1.5">
                  <div className="flex flex-wrap items-center gap-3">
                    <ChoiceSelect
                      ariaLabel="Repeats"
                      value={repeat}
                      className="max-w-64"
                      options={[
                        // "Does not repeat" is gone from the menu: the tick IS
                        // that answer now.
                        ...choices
                          .filter((c) => c.key !== "none")
                          .map((c) =>
                            c.key === "custom" && repeat === "custom" && customRule
                              ? { key: "custom", label: repeatValue }
                              : { key: c.key, label: c.label },
                          ),
                        // Legacy cron templates keep their line under a key of
                        // their own — editing one must not rewrite the rule.
                        ...(legacyCron ? [{ key: "cron", label: describeRepeats(legacyCron) }] : []),
                      ]}
                      onPick={(v) => {
                        if (v === "custom") {
                          // The panel answers what "Custom…" means; the choice
                          // only commits once Done says so.
                          repeatBefore.current = repeat;
                          openRecur();
                          setRepeat("custom");
                        } else {
                          setRepeat(v);
                          if (recurOpen) closeRecur();
                        }
                      }}
                    />
                    {/* The opt-OUT: a task IS a session, so every run lands in
                        its own thread by default; tick this and each run mints
                        a fresh task. "FRESH", not "New" (Akshil, 2026-08-18). */}
                    <CheckRow
                      label="Fresh task each run"
                      checked={newTaskEachRun}
                      onChange={setNewTaskEachRun}
                      describedBy={threadHintId}
                    />
                  </div>
                  {/* The thread this repeat writes into, said out loud. */}
                  <p id={threadHintId} className="text-xs text-muted-foreground">
                    {newTaskEachRun
                      ? "Each run starts a new chat."
                      : learnedSession
                        ? "Every run adds to the chat this task has already started."
                        : "Every run adds to the same chat."}
                  </p>
                </div>
              </IconRow>
            )}
            <IconRow icon={<ShieldIcon />}>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <ChoiceSelect
                  ariaLabel="Permissions"
                  value={permission}
                  // The KEY is what gets submitted; the label is only how the
                  // mode is said.
                  options={(permissionModes.length ? permissionModes : ["auto"]).map((m) => ({
                    key: m,
                    label: permissionLabel(m),
                  }))}
                  onPick={setPermission}
                />
                <span className="text-xs text-muted-foreground">
                  The task runs unattended. Auto approves safe actions and holds the rest.
                </span>
              </div>
            </IconRow>
          </div>
        </details>

        {error && (
          <Alert variant="destructive" className={cn("py-1.5")}>
            <AlertDescription className="text-xs">{error}</AlertDescription>
          </Alert>
        )}
      </div>
    </TaskDialog>
  );
}
