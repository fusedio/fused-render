// The New task card lives in ./new-task/ — this keeps the import path every
// caller (Scheduled.tsx, App.tsx, the tests) already uses. The pure rules the
// tests import are re-exported from the modules that now own them.
export { default } from "./new-task/NewJobModal";
export * from "./new-task/form-logic";
export * from "./new-task/attachments";
export {
  splitTargetPath,
  targetVerdict,
  PATH_MISSING,
  twoLevelsMissing,
  type TargetVerdict,
} from "./new-task/paths";
