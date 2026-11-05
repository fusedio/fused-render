// Wire types of /api/phone/tasks (fused_render/server/routers/phone_tasks.py;
// the contract is docs/phone-tasks.md §2-3). Copied, not imported: the lan
// entry never pulls the desktop page's modules.

export type TaskStatus = "upcoming" | "queued" | "in_progress" | "needs_attention" | "blocked" | "done" | "archived";

export interface PhoneTask {
  key: string;
  task_id: string;
  title: string;
  title_source: string;
  status: TaskStatus | string;
  failed: boolean;
  blocked_reason: string;
  attention: { tool: string; summary: string } | null;
  live: boolean;
  unread: number;
  starred: boolean;
  /** Tilde'd folder. */
  project: string;
  project_label: string;
  target_label: string;
  /** The grid's `/render?path=…` door when the folder is an app, "" otherwise. */
  entry_url: string;
  repo: string;
  prs: { repo: string; number: number; url: string }[];
  backend: string;
  model: string;
  last_message: { role: string; text: string; at: number | null } | null;
  last_reply: string;
  started: number | null;
  last_active: number | null;
  happened_at: number | null;
  message_count: number;
  next_run: number | null;
  next_run_repeats: boolean;
  queue_position: number | null;
  queue_ahead_title: string;
  entry_id: string;
  bot: string;
}

export interface Project {
  path: string;
  label: string;
  count: number;
}

export interface TasksListData {
  tasks: PhoneTask[];
  total: number;
  generation: string;
  projects: Project[];
  attention: number;
  ts: number;
}

export type Unchanged = { unchanged: true; generation: string };

export type PhoneTurn =
  | { i: number; id: string; kind: "user"; text: string; at: number | null }
  | { i: number; id: string; kind: "assistant"; text: string; at: number | null }
  | {
      i: number;
      id: string;
      kind: "tool";
      name: string;
      summary: string;
      status: "running" | "ok" | "error";
      detail: string;
      at: number | null;
    }
  | { i: number; id: string; kind: "system"; text: string; tone: string; at: number | null };

export interface QuestionOption {
  label: string;
  description: string;
}
export interface Question {
  header: string;
  question: string;
  multiSelect: boolean;
  options: QuestionOption[];
}

export interface PhoneCard {
  request_id: string;
  kind: "permission" | "question" | "plan";
  tool: string;
  summary: string;
  detail: string;
  risk: { severity: string; summary: string } | null;
  verdict: "allow" | "deny" | "expired" | "held" | "pending";
  held: boolean;
  created_at: number;
  questions: Question[] | null;
  answers: Record<string, string>;
}

export interface Scheduled {
  entry_id: string;
  text: string;
  due: string | number | null;
  state: string;
  repeats: string;
}

export interface TaskDetail {
  task: PhoneTask;
  turns: PhoneTurn[];
  /** Index after the last turn sent (the next `since`). */
  cursor: number;
  /** Index of the first turn sent. */
  first: number;
  total: number;
  /** The server could not continue from the phone's cursor: `turns` is a fresh tail. */
  reset: boolean;
  live: boolean;
  run_id: string;
  cards: PhoneCard[];
  card: PhoneCard | null;
  scheduled: Scheduled[];
  ts: number;
}

export interface CreateResult {
  key: string;
  entry_id: string;
  task_id: string;
}

export interface NewAppResult extends CreateResult {
  app: { name: string; path: string; url: string };
  task_error?: string;
}
