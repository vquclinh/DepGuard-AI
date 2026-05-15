import { useEffect, useMemo, useRef, useState } from "react";
import type { RefObject } from "react";
import {
  Activity,
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
  discardPreview,
  type ApplyResponse,
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
}

export function DiffReviewPanel({
  preview,
  onApplied,
  onDiscarded,
  onError,
  layout = "overlay",
  onActivityChange,
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
    onActivityChange?.({
      preview,
      summary: reviewSummary,
      isApplying,
      applyResult,
      applyError,
    });
  }, [applyError, applyResult, isApplying, onActivityChange, preview, reviewSummary]);

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
  };

  const handleApply = async () => {
    setIsApplying(true);
    setApplyError("");
    setApplyResult(null);
    try {
      const result = await applyPreview(preview.session_id, applyDecisions);
      setApplyResult(result);
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
    setCurrentFileIndex(Math.max(0, Math.min(preview.files.length - 1, index)));
    scrollRef.current?.scrollTo({ top: 0, behavior: "smooth" });
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
            Apply Accepted
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
        <div className="flex shrink-0 items-center gap-2">
          <button
            onClick={() => onSetFileDecision(currentFile, "accepted")}
            className="inline-flex h-8 items-center gap-2 rounded-md border bg-background px-3 text-xs font-semibold text-emerald-500 transition hover:bg-muted"
          >
            <Check className="h-4 w-4" />
            Accept File
          </button>
          <button
            onClick={() => onSetFileDecision(currentFile, "rejected")}
            className="inline-flex h-8 items-center gap-2 rounded-md border bg-background px-3 text-xs font-semibold text-red-500 transition hover:bg-muted"
          >
            <X className="h-4 w-4" />
            Reject File
          </button>
        </div>
      </div>

      <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto px-4 pb-24 pt-4 font-mono text-xs">
        <div className="space-y-5">
          {currentFile.hunks.map((hunk) => (
            <HunkBlock
              key={hunk.hunk_id}
              hunk={hunk}
              decision={decisions[currentFile.relative_path]?.hunks[hunk.hunk_id]?.decision ?? "pending"}
              onAccept={() => onSetHunkDecision(currentFile, hunk.hunk_id, "accepted")}
              onReject={() => onSetHunkDecision(currentFile, hunk.hunk_id, "rejected")}
            />
          ))}
        </div>
      </div>

      <FileNavigator
        files={preview.files}
        currentFileIndex={currentFileIndex}
        onGoToFile={onGoToFile}
      />
    </>
  );
}

export function DiffReviewActivityPanel({ activity }: { activity: DiffReviewActivityState | null }) {
  if (!activity) {
    return (
      <div className="flex h-full min-h-0 flex-col overflow-hidden">
        <div className="flex h-10 shrink-0 items-center gap-2 border-b px-3">
          <Activity className="h-4 w-4" />
          <h2 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Progress</h2>
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-3">
          <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
            Click Update to preview changes and watch checker/repair progress here.
          </div>
        </div>
      </div>
    );
  }

  const { preview, summary, isApplying, applyResult, applyError } = activity;
  const verification = applyResult?.repair?.final_verification ?? applyResult?.verification ?? null;
  const repair = applyResult?.repair ?? null;

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <div className="flex h-10 shrink-0 items-center gap-2 border-b px-3">
        <Activity className="h-4 w-4" />
        <h2 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Progress</h2>
      </div>
      <div className="min-h-0 flex-1 space-y-3 overflow-auto p-3">
        <div className="rounded-lg border bg-background p-3">
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Review</p>
          <div className="mt-3 grid grid-cols-3 gap-2 text-center">
            <Metric label="Accepted" value={summary.accepted} tone="emerald" />
            <Metric label="Rejected" value={summary.rejected} tone="red" />
            <Metric label="Pending" value={summary.pending} tone="zinc" />
          </div>
        </div>

        <PipelineStep
          title="Preview"
          status="done"
          description={`${preview.summary.total_files_changed} file(s) prepared for ${preview.package}.`}
        />
        <PipelineStep
          title="Apply accepted changes"
          status={isApplying ? "running" : applyResult ? "done" : applyError ? "failed" : "waiting"}
          description={
            applyResult
              ? `${applyResult.files_accepted.length} file(s) accepted, ${applyResult.files_rejected.length} rejected.`
              : applyError || "Waiting for Apply Accepted."
          }
        />
        <PipelineStep
          title="Run project check"
          status={verification ? verificationStatus(verification) : isApplying ? "running" : "waiting"}
          description={verification?.message ?? "DepGuard runs commands like cargo check, pytest, or npm test after apply."}
        />
        <PipelineStep
          title="Repair Agent"
          status={repair ? repairStatus(repair) : "waiting"}
          description={repairDescription(repair)}
        />

        {verification && <VerificationDetails verification={verification} />}
        {repair && repair.attempts.length > 0 && <RepairDetails repair={repair} />}
      </div>
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
  currentFileIndex,
  onGoToFile,
}: {
  files: PreviewFile[];
  currentFileIndex: number;
  onGoToFile: (index: number) => void;
}) {
  return (
    <div className="absolute bottom-4 left-1/2 z-20 w-[min(760px,calc(100%-2rem))] -translate-x-1/2 rounded-xl border bg-card/95 p-2 shadow-2xl backdrop-blur">
      <div className="flex items-center gap-2">
        <button
          onClick={() => onGoToFile(currentFileIndex - 1)}
          disabled={currentFileIndex === 0}
          className="inline-flex h-8 shrink-0 items-center gap-1 rounded-md border bg-background px-2 text-xs font-semibold transition hover:bg-muted disabled:opacity-40"
        >
          <ChevronLeft className="h-4 w-4" />
          Prev
        </button>
        <div className="min-w-0 flex-1 overflow-x-auto">
          <div className="flex items-center gap-2">
            <span className="shrink-0 rounded-md bg-muted px-2 py-1 text-xs font-semibold">
              {files.length} file{files.length === 1 ? "" : "s"} changed
            </span>
            {files.map((file, index) => (
              <button
                key={file.relative_path}
                onClick={() => onGoToFile(index)}
                className={cn(
                  "shrink-0 rounded-md border px-2 py-1 text-xs transition",
                  index === currentFileIndex ? "bg-primary text-primary-foreground" : "bg-background hover:bg-muted"
                )}
              >
                <span className="max-w-[180px] truncate">{file.relative_path}</span>
                <span className="ml-2 text-[10px] opacity-80">
                  {index + 1}/{files.length}
                </span>
              </button>
            ))}
          </div>
        </div>
        <button
          onClick={() => onGoToFile(currentFileIndex + 1)}
          disabled={currentFileIndex === files.length - 1}
          className="inline-flex h-8 shrink-0 items-center gap-1 rounded-md border bg-background px-2 text-xs font-semibold transition hover:bg-muted disabled:opacity-40"
        >
          Next
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}

function HunkBlock({
  hunk,
  decision,
  onAccept,
  onReject,
}: {
  hunk: PreviewHunk;
  decision: HunkDecisionState;
  onAccept: () => void;
  onReject: () => void;
}) {
  return (
    <section
      className={cn(
        "overflow-hidden rounded-lg border bg-background transition",
        decision === "accepted" && "border-emerald-500/40 bg-emerald-500/5",
        decision === "rejected" && "opacity-50 grayscale"
      )}
    >
      <div className="flex items-center justify-between border-b border-dashed px-3 py-2 font-sans">
        <div className="text-xs text-muted-foreground">
          @@ -{hunk.old_start},{hunk.old_lines} +{hunk.new_start},{hunk.new_lines} @@
        </div>
        <div className="flex items-center gap-2">
          <button onClick={onAccept} className="inline-flex h-7 items-center gap-1 rounded border bg-background px-2 text-xs font-semibold text-emerald-500 transition hover:bg-muted">
            <Check className="h-3.5 w-3.5" />
            Accept
          </button>
          <button onClick={onReject} className="inline-flex h-7 items-center gap-1 rounded border bg-background px-2 text-xs font-semibold text-red-500 transition hover:bg-muted">
            <X className="h-3.5 w-3.5" />
            Reject
          </button>
        </div>
      </div>
      <div className="overflow-x-auto py-2">
        {hunk.changes.map((change, index) => (
          <div
            key={`${index}-${change.type}-${change.content}`}
            className={cn(
              "grid min-w-max grid-cols-[64px_24px_1fr] px-3 py-0.5 leading-6",
              change.type === "context" && "text-zinc-400",
              change.type === "deletion" && "bg-red-500/15 text-red-200",
              change.type === "addition" && "bg-emerald-500/15 text-emerald-100"
            )}
          >
            <span className="select-none text-right text-zinc-600">
              {change.line_number_new ?? change.line_number_old ?? ""}
            </span>
            <span className="select-none text-center">
              {change.type === "addition" ? "+" : change.type === "deletion" ? "-" : " "}
            </span>
            <code className={cn(decision === "rejected" && change.type !== "context" && "line-through")}>
              {change.content || " "}
            </code>
          </div>
        ))}
      </div>
    </section>
  );
}

function PipelineStep({
  title,
  status,
  description,
}: {
  title: string;
  status: "done" | "running" | "failed" | "waiting";
  description: string;
}) {
  return (
    <div className="rounded-lg border bg-background p-3">
      <div className="flex items-center gap-2">
        {status === "done" && <CheckCircle2 className="h-4 w-4 text-emerald-500" />}
        {status === "running" && <Loader2 className="h-4 w-4 animate-spin text-sky-400" />}
        {status === "failed" && <XCircle className="h-4 w-4 text-red-500" />}
        {status === "waiting" && <div className="h-4 w-4 rounded-full border" />}
        <p className="text-sm font-semibold">{title}</p>
      </div>
      <p className="mt-2 text-xs leading-5 text-muted-foreground">{description}</p>
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

function Metric({ label, value, tone }: { label: string; value: number; tone: "emerald" | "red" | "zinc" }) {
  const toneClass =
    tone === "emerald"
      ? "text-emerald-500 bg-emerald-500/10"
      : tone === "red"
        ? "text-red-500 bg-red-500/10"
        : "text-muted-foreground bg-muted";

  return (
    <div className={cn("rounded-md px-2 py-2", toneClass)}>
      <div className="text-sm font-bold">{value}</div>
      <div className="text-[10px] uppercase tracking-wide opacity-80">{label}</div>
    </div>
  );
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
