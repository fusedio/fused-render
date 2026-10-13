// Wire types of /api/phone/bots (fused_render/bots/phone.py; the contract is the
// phone-bots spec). The event row is the desktop's BotEvent (docs/bots.md §2,
// frontend/src/apps/bots/lib/api.ts) with the fields the phone router keeps —
// copied, not imported: the lan entry never pulls the desktop page's modules.

export type Role =
  | "user"
  | "thought"
  | "action"
  | "approval"
  | "question"
  | "done"
  | "error"
  | "system"
  | "note"
  | "delivery";
export type BotStatus = "idle" | "running" | "waiting" | "paused" | "error";

export interface ReplyRef {
  seq: number;
  role: Role;
  text: string;
}
/** A channel address (docs §10). "phone" is this page. */
export interface Via {
  kind: string;
  addr: string;
  answers?: number;
}
export interface AppRef {
  name: string;
  dir: string;
}
export interface Offer {
  kind: "use" | "build";
  name: string;
  dir: string;
  spec: string;
}
export interface HandoffRef {
  id: string;
  target: string;
  target_name: string;
  task?: string;
  state: string;
}
export interface BuildRef {
  name: string;
  reason: string;
  summary?: string;
}

export interface BotEvent {
  seq: number;
  ts: number;
  role: Role;
  text: string;
  /** action: a short human one-liner. */
  result?: string;
  /** Absolute: /api/phone/bots/<id>/steps/<seq>.jpg */
  thumb?: string;
  /** Cut to 600 chars by the phone router. */
  detail?: unknown;
  options?: string[];
  /** Several options may be ticked; the answer is "A, B" (or "None"). */
  multi?: boolean;
  offer?: Offer;
  app?: AppRef;
  reply?: ReplyRef;
  via?: Via;
  source?: string;
  handoff?: HandoffRef;
  build?: BuildRef;
  link?: { bot: string; seq: number };
  summary?: string;
  /** role "delivery": the event this row records a send of. */
  ref?: number;
  channel?: string;
  addr?: string;
  error?: string;
}

/** The bot's face meta, passed through as the desktop stores it. */
export interface Face {
  shape?: string;
  color?: string;
  icon?: string;
}

export interface LastLine {
  seq: number;
  role: Role;
  text: string;
  ts: number;
}

export interface PhoneBot {
  id: string;
  name: string;
  kind: "super" | "bot";
  status: BotStatus;
  busy: boolean;
  waitingOn: string | null;
  step: number;
  task: string;
  last: LastLine | null;
  seq: number;
  updated: number;
  control: boolean;
  routines: number;
  face?: Face | null;
}

export interface Routine {
  id: string;
  task: string;
  kind: "interval" | "daily" | "once";
  minutes?: number;
  time?: string;
  weekdays?: number[];
  at?: number;
  enabled: boolean;
  next?: number | null;
  last?: number | null;
  last_result?: string;
  last_message?: string;
}

/** PhoneBot plus the chat's fields; `routines` is the list here, not the count. */
export interface BotDetail extends Omit<PhoneBot, "routines"> {
  instructions: string;
  model: string;
  effort: string;
  approval: "ask" | "auto";
  engine?: string;
  max_steps: number | null;
  /** The ONE card whose buttons are live (meta waiting_on while status == "waiting"). */
  waiting_seq: number | null;
  shot: string | null;
  shot_ts?: number | null;
  viewport?: [number, number];
  browser?: { running: boolean; url: string | null; title: string | null };
  routines: Routine[];
  /** Only with detail=1. */
  memory?: string | null;
  skills?: string[];
}
export interface Setup {
  claude?: { found: boolean | null; cached?: boolean };
  owner?: unknown;
  models: string[];
  efforts: string[];
}

export interface BotsList {
  bots: PhoneBot[];
  ts: number;
  setup: Setup;
}

export interface BotPage {
  bot: BotDetail;
  events: BotEvent[];
  seq: number;
  first_seq: number | null;
  ts: number;
}

export interface Preset {
  key: string;
  name: string;
  color?: string;
  order?: number;
  model?: string;
  instructions?: string;
  description?: string;
  skills?: string[];
}

export interface SendFile {
  name: string;
  data: string;
  preview?: { media_type: string; data: string };
}

export interface SendBody {
  text: string;
  reply_to?: number;
  answer?: number;
  files?: SendFile[];
}

export interface SettingsBody {
  name?: string;
  instructions?: string;
  model?: string;
  effort?: string;
  approval?: "ask" | "auto";
  max_steps?: number | null;
  memory?: string;
}

export interface CreateBody {
  name: string;
  preset?: string;
  instructions?: string;
  model?: string;
  effort?: string;
  approval?: "ask" | "auto";
}

export interface RoutineBody {
  op: "add" | "edit" | "delete" | "enable" | "disable" | "run";
  rid?: string;
  text?: string;
  kind?: "interval" | "daily" | "once";
  minutes?: number;
  time?: string;
  weekdays?: number[];
  at?: number;
}
