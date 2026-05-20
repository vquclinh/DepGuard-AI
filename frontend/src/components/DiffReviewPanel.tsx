import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  FileCode2,
  Loader2,
  Terminal,
  X,
  XCircle,
} from "lucide-react";
import {
  applyPreview,
  applyPreviewSelection,
  discardPreview,
  getFileContent,
  type ApplyResponse,
  type ApplySelectionResponse,
  type PreviewFile,
  type PreviewHunk,
  type PreviewResponse,
  type RepairReport,
  type VerificationReport,
} from "@/hooks/useDepGuard";
import { cn } from "@/lib/utils";

type HunkDecisionState = "pending" | "accepted" | "rejected";
type FileReviewState = "pending" | "accepted" | "rejected" | "partial";
type DiffReviewLayout = "overlay" | "embedded";

interface ReviewSummary {
  accepted: number;
  rejected: number;
  pending: number;
}

export interface DiffReviewActivityState {
  preview: PreviewResponse;
  summary: ReviewSummary;
  isApplying: boolean;
  applyResult: ApplyResponse | null;
  applyError: string;
}

interface HunkDecision {
  hunkId: string;
  decision: HunkDecisionState;
}

interface FileDecision {
  filePath: string;
  hunks: Record<string, HunkDecision>;
}

interface DiffReviewPanelProps {
  preview: PreviewResponse;
  onApplied: (result: ApplyResponse) => void;
  onDiscarded: () => void;
  onError: (message: string) => void;
  layout?: DiffReviewLayout;
  onActivityChange?: (activity: DiffReviewActivityState) => void;
  folderPath?: string;
  activeFilePath?: string;
  onFileChange?: (filePath: string) => void;
  onDecision?: (file: string, scope: "file" | "hunk", decision: "accepted" | "rejected") => void;
  onPreviewUpdated?: (preview: PreviewResponse) => void;
}

export function DiffReviewPanel({
  preview,
  onApplied,
  onDiscarded,
  onError,
  layout = "overlay",
  onActivityChange,
  folderPath,
  activeFilePath,
  onFileChange,
  onDecision,
  onPreviewUpdated,
}: DiffReviewPanelProps) {
  const [currentFileIndex, setCurrentFileIndex] = useState(0);
  const [decisions, setDecisions] = useState<Record<string, FileDecision>>(() => initializeDecisions(preview));
  const [isApplying, setIsApplying] = useState(false);
  const [applyResult, setApplyResult] = useState<ApplyResponse | null>(null);
  const [applyError, setApplyError] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const currentFile = preview.files[currentFileIndex];
  const applyDecisions = useMemo(() => buildApplyDecisions(preview, decisions), [decisions, preview]);
  const reviewSummary = useMemo(() => summarizeDecisions(preview, decisions), [preview, decisions]);

  useEffect(() => {
    setDecisions(initializeDecisions(preview));
    setCurrentFileIndex(0);
    setApplyResult(null);
    setApplyError("");
  }, [preview]);

  useEffect(() => {
    if (!activeFilePath) return;
    const idx = preview.files.findIndex(
      (f) => f.file_path === activeFilePath || f.relative_path === activeFilePath
    );
    if (idx >= 0 && idx !== currentFileIndex) {
      setCurrentFileIndex(idx);
      scrollRef.current?.scrollTo({ top: 0, behavior: "smooth" });
    }
  }, [activeFilePath, preview.files]);

  useEffect(() => {
    onActivityChange?.({
      preview,
      summary: reviewSummary,
      isApplying,
      applyResult,
      applyError,
    });
  }, [applyError, applyResult, isApplying, onActivityChange, preview, reviewSummary]);

  const handleSelectionResult = (result: ApplySelectionResponse) => {
    const appliedResult = selectionResultToApplyResponse(result);
    if (result.complete) {
      setApplyResult(appliedResult);
      onApplied(appliedResult);
      return;
    }
    if (result.preview) {
      onPreviewUpdated?.(result.preview);
    }
  };

  const applySelection = async (selectionDecisions: object) => {
    setIsApplying(true);
    setApplyError("");
    setApplyResult(null);
    try {
      const result = await applyPreviewSelection(preview.session_id, selectionDecisions);
      handleSelectionResult(result);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to apply preview selection";
      setDecisions(initializeDecisions(preview));
      setApplyError(message);
      onError(message);
    } finally {
      setIsApplying(false);
    }
  };

  const setHunkDecision = (file: PreviewFile, hunkId: string, decision: HunkDecisionState) => {
    setApplyResult(null);
    setApplyError("");
    setDecisions((current) => ({
      ...current,
      [file.relative_path]: {
        ...current[file.relative_path],
        hunks: {
          ...current[file.relative_path].hunks,
          [hunkId]: { hunkId, decision },
        },
      },
    }));
    if (decision !== "pending") {
      onDecision?.(file.relative_path, "hunk", decision);
      void applySelection(buildHunkSelectionDecision(file, hunkId, decision));
    }
  };

  const setFileDecision = (file: PreviewFile, decision: HunkDecisionState) => {
    setApplyResult(null);
    setApplyError("");
    setDecisions((current) => ({
      ...current,
      [file.relative_path]: {
        ...current[file.relative_path],
        hunks: Object.fromEntries(
          file.hunks.map((hunk) => [hunk.hunk_id, { hunkId: hunk.hunk_id, decision }])
        ),
      },
    }));
    if (decision !== "pending") {
      onDecision?.(file.relative_path, "file", decision);
      void applySelection(buildFileSelectionDecision(file, decision));
    }
  };

  const handleApply = async () => {
    setIsApplying(true);
    setApplyError("");
    setApplyResult(null);
    try {
      const result = await applyPreview(preview.session_id, applyDecisions);
      setApplyResult(result);
      onApplied(result);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to apply preview";
      setApplyError(message);
      onError(message);
    } finally {
      setIsApplying(false);
    }
  };

  const handleClose = async () => {
    if (applyResult) {
      onApplied(applyResult);
      return;
    }

    try {
      await discardPreview(preview.session_id);
      onDiscarded();
    } catch (error) {
      onError(error instanceof Error ? error.message : "Failed to discard preview");
    }
  };

  const goToFile = (index: number) => {
    if (preview.files.length === 0) return;
    const wrapped = ((index % preview.files.length) + preview.files.length) % preview.files.length;
    setCurrentFileIndex(wrapped);
    scrollRef.current?.scrollTo({ top: 0, behavior: "smooth" });
    onFileChange?.(preview.files[wrapped].file_path);
  };

  const content = !currentFile ? (
    <EmptyReviewState onClose={handleClose} />
  ) : (
    <ReviewContent
      preview={preview}
      currentFile={currentFile}
      currentFileIndex={currentFileIndex}
      decisions={decisions}
      isApplying={isApplying}
      applyResult={applyResult}
      scrollRef={scrollRef}
      folderPath={folderPath}
      onApply={handleApply}
      onClose={handleClose}
      onGoToFile={goToFile}
      onSetFileDecision={setFileDecision}
      onSetHunkDecision={setHunkDecision}
    />
  );

  if (layout === "embedded") {
    return <div className="relative flex h-full min-h-0 flex-col overflow-hidden bg-[#101010]">{content}</div>;
  }

  return (
    <div className="fixed inset-0 z-[80] flex overflow-hidden bg-background text-foreground">
      <section className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">{content}</section>
      <div className="hidden w-[360px] shrink-0 border-l bg-card/70 lg:block">
        <DiffReviewActivityPanel
          activity={{ preview, summary: reviewSummary, isApplying, applyResult, applyError }}
        />
      </div>
    </div>
  );
}

function ReviewContent({
  preview,
  currentFile,
  currentFileIndex,
  decisions,
  isApplying,
  applyResult,
  scrollRef,
  folderPath,
  onApply,
  onClose,
  onGoToFile,
  onSetFileDecision,
  onSetHunkDecision,
}: {
  preview: PreviewResponse;
  currentFile: PreviewFile;
  currentFileIndex: number;
  decisions: Record<string, FileDecision>;
  isApplying: boolean;
  applyResult: ApplyResponse | null;
  scrollRef: RefObject<HTMLDivElement>;
  folderPath?: string;
  onApply: () => void;
  onClose: () => void;
  onGoToFile: (index: number) => void;
  onSetFileDecision: (file: PreviewFile, decision: HunkDecisionState) => void;
  onSetHunkDecision: (file: PreviewFile, hunkId: string, decision: HunkDecisionState) => void;
}) {
  const currentFileState = getFileReviewState(currentFile, decisions);

  return (
    <>
      <div className="flex h-11 shrink-0 items-center justify-between border-b bg-card px-3">
        <button onClick={onClose} className="inline-flex h-8 items-center gap-2 rounded-md px-2.5 text-xs font-semibold transition hover:bg-muted">
          <ArrowLeft className="h-4 w-4" />
          {applyResult ? "Done" : "Review Changes"}
        </button>
        <div className="min-w-0 px-3 text-center">
          <div className="truncate text-xs font-semibold">
            {preview.package} {preview.from_version} -&gt; {preview.to_version}
          </div>
          <div className="text-[11px] text-muted-foreground">
            {preview.summary.total_files_changed} files, +{preview.summary.total_additions} / -{preview.summary.total_deletions}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            onClick={onApply}
            disabled={isApplying || Boolean(applyResult)}
            className="inline-flex h-8 items-center gap-2 rounded-md bg-primary px-3 text-xs font-semibold text-primary-foreground transition hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {isApplying ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
            Apply
          </button>
          {!applyResult && (
            <button
              onClick={onClose}
              disabled={isApplying}
              className="inline-flex h-8 items-center gap-2 rounded-md border bg-background px-3 text-xs font-semibold transition hover:bg-muted disabled:opacity-60"
            >
              <X className="h-4 w-4" />
              Discard
            </button>
          )}
        </div>
      </div>

      <div className="flex h-12 shrink-0 items-center justify-between border-b bg-background px-4">
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-2">
            <FileCode2 className="h-4 w-4 shrink-0 text-muted-foreground" />
            <p className="truncate text-sm font-semibold">{currentFile.relative_path}</p>
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">
            +{currentFile.additions} / -{currentFile.deletions} changes, {stateLabel(currentFileState)}
          </p>
        </div>
      </div>

      <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto font-mono text-xs">
        <FullFileDiffViewer
          file={currentFile}
          folderPath={folderPath ?? ""}
          decisions={decisions}
          isApplying={isApplying}
          onSetHunkDecision={onSetHunkDecision}
        />
      </div>

      <FileNavigator
        files={preview.files}
        currentFile={currentFile}
        currentFileIndex={currentFileIndex}
        isApplying={isApplying}
        onGoToFile={onGoToFile}
        onAcceptFile={() => onSetFileDecision(currentFile, "accepted")}
        onRejectFile={() => onSetFileDecision(currentFile, "rejected")}
      />
    </>
  );
}

export function DiffReviewActivityPanel({ activity }: { activity: DiffReviewActivityState | null }) {
  if (!activity) {
    return (
      <div className="flex h-full min-h-0 flex-col overflow-hidden">
        <div className="min-h-0 flex-1 overflow-auto p-3">
          <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
            Click Update to preview changes and watch checker/repair progress here.
          </div>
        </div>
      </div>
    );
  }

  const { preview, isApplying, applyResult, applyError } = activity;
  const verification = applyResult?.repair?.final_verification ?? applyResult?.verification ?? null;
  const repair = applyResult?.repair ?? null;

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <div className="min-h-0 flex-1 overflow-auto p-3">
        <div className="space-y-3 border-l pl-3">
          <ActivityEntry
            title="Prepared review"
            eyebrow="Preview response"
            status="done"
            detail={`${preview.package} ${preview.from_version} -> ${preview.to_version}`}
          >
            <ResponseLines
              lines={[
                `${preview.summary.total_files_changed} file(s) changed`,
                `+${preview.summary.total_additions} additions, -${preview.summary.total_deletions} deletions`,
                ...preview.files.slice(0, 6).map((file) => `${file.relative_path} (+${file.additions}/-${file.deletions})`),
                ...(preview.files.length > 6 ? [`...${preview.files.length - 6} more file(s)`] : []),
              ]}
            />
          </ActivityEntry>
          <ActivityEntry
            title={isApplying ? "Applying accepted changes" : applyResult ? "Applied accepted changes" : applyError ? "Apply failed" : "Waiting for apply"}
            eyebrow="Current work"
            status={isApplying ? "running" : applyResult ? "done" : applyError ? "failed" : "waiting"}
            detail={
              applyResult
                ? `${applyResult.files_accepted.length} accepted, ${applyResult.files_rejected.length} rejected`
                : applyError || "Review blocks, then click Apply Accepted."
            }
          />
          <ActivityEntry
            title={verification ? "Project check finished" : isApplying ? "Running project check" : "Project check pending"}
            eyebrow="Checker"
            status={verification ? verificationStatus(verification) : isApplying ? "running" : "waiting"}
            detail={verification?.message ?? "DepGuard will run the project checker after files are written."}
          >
            {verification && <VerificationDetails verification={verification} />}
          </ActivityEntry>
          <ActivityEntry
            title={repair ? "Repair Agent response" : "Repair Agent pending"}
            eyebrow="Repair"
            status={repair ? repairStatus(repair) : "waiting"}
            detail={repairDescription(repair)}
          >
            {repair && repair.attempts.length > 0 && <RepairDetails repair={repair} />}
          </ActivityEntry>
        </div>
      </div>
    </div>
  );
}

function ActivityEntry({
  title,
  eyebrow,
  status,
  detail,
  children,
}: {
  title: string;
  eyebrow: string;
  status: "done" | "running" | "failed" | "waiting";
  detail: string;
  children?: ReactNode;
}) {
  return (
    <section className="relative rounded-lg border bg-background p-3">
      <span className="absolute -left-[22px] top-4 flex h-4 w-4 items-center justify-center rounded-full bg-card">
        {status === "done" && <CheckCircle2 className="h-4 w-4 text-emerald-500" />}
        {status === "running" && <Loader2 className="h-4 w-4 animate-spin text-sky-400" />}
        {status === "failed" && <XCircle className="h-4 w-4 text-red-500" />}
        {status === "waiting" && <span className="h-3 w-3 rounded-full border bg-background" />}
      </span>
      <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{eyebrow}</p>
      <h3 className="mt-1 text-sm font-semibold">{title}</h3>
      <p className="mt-2 text-xs leading-5 text-muted-foreground">{detail}</p>
      {children && <div className="mt-3">{children}</div>}
    </section>
  );
}

function ResponseLines({ lines }: { lines: string[] }) {
  return (
    <div className="space-y-1 rounded-md border bg-card p-2">
      {lines.map((line, index) => (
        <div key={`${index}-${line}`} className="flex gap-2 text-[11px] leading-4 text-muted-foreground">
          <span className="select-none text-muted-foreground/60">{index + 1}</span>
          <span className="min-w-0 break-words">{line}</span>
        </div>
      ))}
    </div>
  );
}

function EmptyReviewState({ onClose }: { onClose: () => void }) {
  return (
    <div className="flex h-full min-h-0 items-center justify-center bg-background p-6 text-center">
      <div className="rounded-lg border bg-card p-6">
        <p className="text-sm text-muted-foreground">No file changes were generated for this preview.</p>
        <button onClick={onClose} className="mt-4 rounded-md border bg-background px-4 py-2 text-sm font-semibold hover:bg-muted">
          Close
        </button>
      </div>
    </div>
  );
}

function FileNavigator({
  files,
  currentFile,
  currentFileIndex,
  isApplying,
  onGoToFile,
  onAcceptFile,
  onRejectFile,
}: {
  files: PreviewFile[];
  currentFile: PreviewFile;
  currentFileIndex: number;
  isApplying: boolean;
  onGoToFile: (index: number) => void;
  onAcceptFile: () => void;
  onRejectFile: () => void;
}) {
  const [isFileListOpen, setIsFileListOpen] = useState(false);
  const fileCount = files.length;
  const previousIndex = fileCount > 0 ? (currentFileIndex - 1 + fileCount) % fileCount : 0;
  const nextIndex = fileCount > 0 ? (currentFileIndex + 1) % fileCount : 0;
  const previousFile = files[previousIndex] ?? null;
  const nextFile = files[nextIndex] ?? null;

  return (
    <div className="absolute bottom-8 left-1/2 z-20 w-[min(480px,calc(100%-2rem))] -translate-x-1/2 rounded-lg border bg-card/95 p-1 shadow-2xl backdrop-blur">
      {isFileListOpen && (
        <div className="absolute bottom-full left-1/2 mb-1.5 max-h-56 w-[min(440px,calc(100vw-3rem))] -translate-x-1/2 overflow-auto rounded-lg border bg-card p-1.5 shadow-2xl">
          <div className="mb-1 px-2 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            {files.length} changed file{files.length === 1 ? "" : "s"}
          </div>
          <div className="space-y-0.5">
            {files.map((file, index) => (
              <button
                key={file.relative_path}
                onClick={() => {
                  onGoToFile(index);
                  setIsFileListOpen(false);
                }}
                className={cn(
                  "flex h-7 w-full min-w-0 items-center justify-between gap-2 rounded px-2 text-left text-[11px] transition",
                  index === currentFileIndex ? "bg-primary text-primary-foreground" : "hover:bg-muted"
                )}
              >
                <span className="min-w-0 truncate">{file.relative_path}</span>
                <span className="shrink-0 text-[10px] opacity-70">
                  +{file.additions}/−{file.deletions}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}
      <div className="flex items-center gap-1">
        <button
          onClick={() => onGoToFile(previousIndex)}
          disabled={fileCount <= 1}
          title={previousFile ? `Previous: ${previousFile.relative_path}` : "No previous file"}
          className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded border bg-background text-xs transition hover:bg-muted disabled:opacity-40"
        >
          <ChevronLeft className="h-3.5 w-3.5" />
        </button>
        <button
          onClick={() => setIsFileListOpen((current) => !current)}
          className="min-w-0 flex-1 rounded bg-muted px-2 py-1 text-center text-[11px] font-semibold transition hover:bg-muted/80"
          title={currentFile.relative_path}
        >
          <span>{files.length} file{files.length === 1 ? "" : "s"} changed</span>
          <span className="mx-1.5 text-muted-foreground">|</span>
          <span>{currentFileIndex + 1}/{files.length}</span>
        </button>
        <button
          onClick={onAcceptFile}
          disabled={isApplying}
          className="inline-flex h-7 shrink-0 items-center gap-1 rounded border bg-background px-2 text-[11px] font-semibold text-emerald-500 transition hover:bg-muted disabled:opacity-40"
        >
          <Check className="h-3.5 w-3.5" />
          Accept
        </button>
        <button
          onClick={onRejectFile}
          disabled={isApplying}
          className="inline-flex h-7 shrink-0 items-center gap-1 rounded border bg-background px-2 text-[11px] font-semibold text-red-500 transition hover:bg-muted disabled:opacity-40"
        >
          <X className="h-3.5 w-3.5" />
          Reject
        </button>
        <button
          onClick={() => onGoToFile(nextIndex)}
          disabled={fileCount <= 1}
          title={nextFile ? `Next: ${nextFile.relative_path}` : "No next file"}
          className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded border bg-background text-xs transition hover:bg-muted disabled:opacity-40"
        >
          <ChevronRight className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}

interface UnifiedRow {
  type: "gap" | "context" | "deletion" | "addition";
  oldLine: number | null;
  newLine: number | null;
  content: string;
  hunkId: string | null;
}

function buildUnifiedRows(originalLines: string[], hunks: PreviewHunk[]): UnifiedRow[] {
  const rows: UnifiedRow[] = [];
  let oldCursor = 1;
  let newOffset = 0;
  const sorted = [...hunks].sort((a, b) => a.old_start - b.old_start);

  for (const hunk of sorted) {
    // gap lines before this hunk
    for (let ln = oldCursor; ln < hunk.old_start; ln++) {
      rows.push({ type: "gap", oldLine: ln, newLine: ln + newOffset, content: originalLines[ln - 1] ?? "", hunkId: null });
    }
    // hunk changes
    for (const c of hunk.changes) {
      rows.push({ type: c.type, oldLine: c.line_number_old, newLine: c.line_number_new, content: c.content, hunkId: hunk.hunk_id });
    }
    newOffset += hunk.new_lines - hunk.old_lines;
    oldCursor = hunk.old_start + hunk.old_lines;
  }
  // trailing gap lines
  for (let ln = oldCursor; ln <= originalLines.length; ln++) {
    rows.push({ type: "gap", oldLine: ln, newLine: ln + newOffset, content: originalLines[ln - 1] ?? "", hunkId: null });
  }
  return rows;
}

function FullFileDiffViewer({
  file,
  folderPath,
  decisions,
  isApplying,
  onSetHunkDecision,
}: {
  file: PreviewFile;
  folderPath: string;
  decisions: Record<string, FileDecision>;
  isApplying: boolean;
  onSetHunkDecision: (file: PreviewFile, hunkId: string, decision: HunkDecisionState) => void;
}) {
  const [originalLines, setOriginalLines] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const hunkSignature = useMemo(
    () => file.hunks.map((hunk) => `${hunk.hunk_id}:${hunk.old_start}:${hunk.old_lines}:${hunk.new_start}:${hunk.new_lines}`).join("|"),
    [file.hunks]
  );

  useEffect(() => {
    setLoading(true);
    setOriginalLines([]);
    getFileContent(folderPath, file.file_path)
      .then((result) => {
        setOriginalLines(result.content.split("\n"));
      })
      .catch(() => {
        setOriginalLines([]);
      })
      .finally(() => {
        setLoading(false);
      });
  }, [file.file_path, folderPath, hunkSignature]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const rows = buildUnifiedRows(originalLines, file.hunks);

  // Build a lookup map: hunkId -> PreviewHunk
  const hunkMap = new Map<string, PreviewHunk>(file.hunks.map((h) => [h.hunk_id, h]));

  return (
    <div className="overflow-x-auto pb-24">
      {rows.map((row, index) => {
        const prevRow = index > 0 ? rows[index - 1] : null;
        const isHunkStart = row.hunkId !== null && prevRow?.hunkId !== row.hunkId;
        const hunk = row.hunkId !== null ? hunkMap.get(row.hunkId) ?? null : null;
        const decision: HunkDecisionState =
          row.hunkId !== null
            ? (decisions[file.relative_path]?.hunks[row.hunkId]?.decision ?? "pending")
            : "pending";

        if (decision === "accepted" && row.type === "deletion") return null;
        if (decision === "rejected" && row.type === "addition") return null;

        return (
          <div key={`row-${index}`}>
            {isHunkStart && hunk !== null && (
              <div
                className={cn(
                  "sticky top-0 z-10 flex items-center justify-between border-y border-dashed px-3 py-1 backdrop-blur-sm",
                  decision === "accepted" && "border-emerald-500/30 bg-emerald-950/30",
                  decision === "rejected" && "border-red-500/20 bg-red-950/20 opacity-60",
                  decision === "pending" && "border-zinc-700 bg-[#0d1117]/95"
                )}
              >
                <span className="font-sans text-[10px] text-zinc-500">
                  @@ -{hunk.old_start},{hunk.old_lines} +{hunk.new_start},{hunk.new_lines} @@
                </span>
                {decision === "pending" && (
                  <div className="flex gap-2">
                    <button
                      onClick={() => onSetHunkDecision(file, hunk.hunk_id, "accepted")}
                      disabled={isApplying}
                      className="inline-flex h-6 items-center gap-1 rounded border border-emerald-700/40 bg-emerald-950/50 px-2 font-sans text-[10px] font-semibold text-emerald-400 transition hover:bg-emerald-900/50 disabled:opacity-50"
                    >
                      <Check className="h-3 w-3" /> Accept
                    </button>
                    <button
                      onClick={() => onSetHunkDecision(file, hunk.hunk_id, "rejected")}
                      disabled={isApplying}
                      className="inline-flex h-6 items-center gap-1 rounded border border-red-700/40 bg-red-950/50 px-2 font-sans text-[10px] font-semibold text-red-400 transition hover:bg-red-900/50 disabled:opacity-50"
                    >
                      <X className="h-3 w-3" /> Reject
                    </button>
                  </div>
                )}
              </div>
            )}
            <div
              className={cn(
                "grid min-w-max grid-cols-[3rem_3rem_1.25rem_1fr] leading-5",
                (row.type === "gap" || row.type === "context") && "text-zinc-100",
                row.type === "deletion" && decision === "pending" && "bg-red-950/50 text-red-300",
                row.type === "addition" && decision === "pending" && "bg-emerald-950/50 text-emerald-200",
                (row.type === "deletion" || row.type === "addition") && decision !== "pending" && "text-zinc-100"
              )}
            >
              <span className="select-none pr-2 text-right text-[10px] text-zinc-500">
                {(row.type === "gap" || row.type === "context" || row.type === "deletion") && (row.oldLine ?? "")}
              </span>
              <span className="select-none pr-2 text-right text-[10px] text-zinc-500">
                {(row.type === "gap" || row.type === "context" || row.type === "addition") && (row.newLine ?? "")}
              </span>
              <span className="select-none text-center">
                {decision === "pending" && row.type === "deletion" ? "-" : decision === "pending" && row.type === "addition" ? "+" : " "}
              </span>
              <code>{row.content || " "}</code>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function VerificationDetails({ verification }: { verification: VerificationReport }) {
  return (
    <div className="rounded-lg border bg-background p-3">
      <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        <Terminal className="h-4 w-4" />
        Checker Output
      </div>
      <div className="space-y-2">
        {verification.commands.map((command, index) => (
          <div key={`${command.name}-${index}`} className="rounded-md border bg-card p-2">
            <div className="flex items-center justify-between gap-2">
              <p className="truncate text-xs font-semibold">{command.name}</p>
              <span className={cn("shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold", commandTone(command.status))}>
                {command.status}
              </span>
            </div>
            <code className="mt-1 block truncate text-[11px] text-muted-foreground">{command.command}</code>
            {(command.stderr || command.stdout) && (
              <pre className="mt-2 max-h-28 overflow-auto whitespace-pre-wrap rounded bg-background p-2 text-[11px] leading-4 text-muted-foreground">
                {trimOutput(command.stderr || command.stdout || "")}
              </pre>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function RepairDetails({ repair }: { repair: RepairReport }) {
  return (
    <div className="rounded-lg border bg-background p-3">
      <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        <AlertTriangle className="h-4 w-4 text-amber-500" />
        Repair Loop
      </div>
      <div className="space-y-2">
        {repair.attempts.map((attempt) => (
          <div key={attempt.attempt} className="rounded-md border bg-card p-2">
            <div className="flex items-center justify-between">
              <p className="text-xs font-semibold">Attempt {attempt.attempt}</p>
              <span className={cn("rounded-full px-2 py-0.5 text-[10px] font-semibold", commandTone(attempt.status))}>
                {attempt.status}
              </span>
            </div>
            {attempt.files_repaired && attempt.files_repaired.length > 0 && (
              <p className="mt-1 truncate text-[11px] text-muted-foreground">{attempt.files_repaired.join(", ")}</p>
            )}
            {attempt.error && <p className="mt-1 text-[11px] text-red-500">{attempt.error}</p>}
          </div>
        ))}
      </div>
    </div>
  );
}

function buildHunkSelectionDecision(file: PreviewFile, hunkId: string, decision: HunkDecisionState) {
  return {
    [file.relative_path]: {
      file_decision: "partial",
      hunks: {
        [hunkId]: decision === "rejected" ? "reject" : "accept",
      },
    },
  };
}

function buildFileSelectionDecision(file: PreviewFile, decision: HunkDecisionState) {
  const applyDecision = decision === "rejected" ? "reject" : "accept";
  return {
    [file.relative_path]: {
      file_decision: applyDecision,
      hunks: Object.fromEntries(file.hunks.map((hunk) => [hunk.hunk_id, applyDecision])),
    },
  };
}

function selectionResultToApplyResponse(result: ApplySelectionResponse): ApplyResponse {
  return {
    status: result.status,
    files_accepted: result.files_accepted,
    files_rejected: result.files_rejected,
    dependency_file_updated: result.dependency_file_updated,
    verification: null,
    repair: null,
    checkpoint_id: result.checkpoint_id,
  };
}

function initializeDecisions(preview: PreviewResponse): Record<string, FileDecision> {
  return Object.fromEntries(
    preview.files.map((file) => [
      file.relative_path,
      {
        filePath: file.relative_path,
        hunks: Object.fromEntries(
          file.hunks.map((hunk) => [hunk.hunk_id, { hunkId: hunk.hunk_id, decision: "pending" as const }])
        ),
      },
    ])
  );
}

function buildApplyDecisions(preview: PreviewResponse, decisions: Record<string, FileDecision>) {
  return Object.fromEntries(
    preview.files.map((file) => {
      const hunkEntries = file.hunks.map((hunk) => {
        const decision = decisions[file.relative_path]?.hunks[hunk.hunk_id]?.decision ?? "pending";
        return [hunk.hunk_id, decision === "rejected" ? "reject" : "accept"];
      });
      const hunkValues = hunkEntries.map((entry) => entry[1]);
      const fileDecision = hunkValues.every((value) => value === "accept")
        ? "accept"
        : hunkValues.every((value) => value === "reject")
          ? "reject"
          : "partial";

      return [
        file.relative_path,
        {
          file_decision: fileDecision,
          hunks: Object.fromEntries(hunkEntries),
        },
      ];
    })
  );
}

function getFileReviewState(file: PreviewFile, decisions: Record<string, FileDecision>): FileReviewState {
  const values = file.hunks.map((hunk) => decisions[file.relative_path]?.hunks[hunk.hunk_id]?.decision ?? "pending");
  if (values.length === 0 || values.every((value) => value === "pending")) return "pending";
  if (values.every((value) => value === "accepted")) return "accepted";
  if (values.every((value) => value === "rejected")) return "rejected";
  return "partial";
}

function summarizeDecisions(preview: PreviewResponse, decisions: Record<string, FileDecision>): ReviewSummary {
  return preview.files.reduce(
    (summary, file) => {
      file.hunks.forEach((hunk) => {
        const decision = decisions[file.relative_path]?.hunks[hunk.hunk_id]?.decision ?? "pending";
        summary[decision] += 1;
      });
      return summary;
    },
    { accepted: 0, rejected: 0, pending: 0 }
  );
}

function stateLabel(state: FileReviewState) {
  if (state === "accepted") return "accepted";
  if (state === "rejected") return "rejected";
  if (state === "partial") return "partially reviewed";
  return "pending review";
}

function verificationStatus(verification: VerificationReport): "done" | "failed" | "waiting" {
  if (verification.status === "passed" || verification.status === "skipped") return "done";
  return "failed";
}

function repairStatus(repair: RepairReport): "done" | "failed" | "waiting" {
  if (repair.status === "success" || repair.status === "skipped") return "done";
  return "failed";
}

function repairDescription(repair: RepairReport | null) {
  if (!repair) return "If the checker fails, DepGuard can call the Repair Agent and verify again.";
  if (repair.status === "skipped") return "No repair was needed or auto repair is disabled.";
  if (repair.status === "success") return `Repair finished after ${repair.attempts.length} attempt(s).`;
  return `Repair stopped after ${repair.attempts.length} attempt(s).`;
}

function commandTone(status: string) {
  if (status === "passed" || status === "success") return "bg-emerald-500/10 text-emerald-500";
  if (status === "failed") return "bg-red-500/10 text-red-500";
  return "bg-muted text-muted-foreground";
}

function trimOutput(output: string) {
  const trimmed = output.trim();
  return trimmed.length > 1600 ? `${trimmed.slice(0, 1600)}\n...` : trimmed;
}
