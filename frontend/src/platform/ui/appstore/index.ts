// App-Store building blocks (Marketplace detail + browse, and the phase-2
// `.fused` install page). Presentational and catalog-agnostic: every piece
// takes plain props/slots. Styles: styles/marketplace.css (`.as-*`).
export { AppIcon, initialsOf, tintIndex, type AppIconProps } from "./AppIcon";
export { ActionPill, ProgressRing, type ActionPillProps, type ActionPillState, type ActionPillSize } from "./ActionPill";
export { InfoStrip, type InfoStripItem } from "./InfoStrip";
export { PreviewStage, Lightbox, type PreviewStageProps } from "./PreviewStage";
export { MarkdownView, renderMarkdown, resolveAgainst, type MarkdownViewProps } from "./MarkdownView";
export { FilesTable, type FileRow, type FilesTableProps } from "./FilesTable";
export { TrustCard, TrustList, type TrustItem, type TrustCardProps } from "./TrustCard";
export {
  DetailLayout,
  DetailHero,
  DetailBody,
  DetailSection,
  InfoList,
  SegmentedTabs,
  MoreMenuButton,
  BackLinkView,
  type BackLink,
  type DetailHeroProps,
  type InfoListRow,
  type SegmentedTab,
} from "./DetailLayout";
export { StoreCard, type StoreCardProps } from "./StoreCard";
export { Delayed, Skel, CardSkeleton, RowSkeleton, DetailSkeleton } from "./Skeletons";
