// The run's task registry, by id, for the tool chips that link to a record
// (SPEC T-5). A context rather than a prop threaded through Transcript → Turn →
// SegmentView → ToolChip: those are memoized on their segments, and a task's
// usage moving must re-render the chip header that shows it — not every turn.
// Only `ChipTaskInfo` (a linked chip's header extras) reads it.
import { createContext, useContext } from "react";

import type { TaskRecord } from "../protocol/types";

const EMPTY: ReadonlyMap<string, TaskRecord> = new Map();

export const TasksContext = createContext<ReadonlyMap<string, TaskRecord>>(EMPTY);

export function useTaskIndex(): ReadonlyMap<string, TaskRecord> {
  return useContext(TasksContext);
}
