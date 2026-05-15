import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, Check, ChevronLeft, ChevronRight, Loader2, X } from "lucide-react";
import {
  applyPreview,
  discardPreview,
  type ApplyResponse,
  type PreviewFile,
  type PreviewHunk,
  type PreviewResponse,
} from "@/hooks/useDepGuard";
import { cn } from "@/lib/utils";

type HunkDecisionState = "pending" | "accepted" | "rejected";

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
}

export function DiffReviewPanel({ preview, onApplied, onDiscarded, onError }: DiffReviewPanelProps) {
  const [currentFileIndex, setCurrentFileIndex] = useState(0);
  const [decisions, setDecisions] = useState<Record<string, FileDecision>>(() => initializeDecisions(preview));
  const [isApplying, setIsApplying] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const currentFile = preview.files[currentFileIndex];

  useEffect(() => {
    setDecisions(initializeDecisions(preview));
    setCurrentFileIndex(0);
  }, [preview]);

  const currentFileDecision = currentFile ? decisions[currentFile.relative_path] : undefined;

  const applyDecisions = useMemo(() => buildApplyDecisions(preview, decisions), [decisions, preview]);

  const setHunkDecision = (file: PreviewFile, hunkId: string, decision: HunkDecisionState) => {
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
    try {
      const result = await applyPreview(preview.session_id, applyDecisions);
      onApplied(result);
    } catch (error) {
      onError(error instanceof Error ? error.message : "Failed to apply preview");
    } finally {
      setIsApplying(false);
    }
  };

  const handleRejectAll = async () => {
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

  if (!currentFile) {
    return (
      <div className="fixed inset-0 z-[80] flex items-center justify-center bg-[#0f0f0f] text-foreground">
        <div className="rounded-xl border bg-card p-6 text-center">
          <p className="text-sm text-muted-foreground">No file changes were generated for this preview.</p>
          <button onClick={handleRejectAll} className="mt-4 rounded-lg border px-4 py-2 text-sm font-semibold hover:bg-muted">
            Close
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 z-[80] flex flex-col bg-[#0f0f0f] text-foreground">
      <header className="flex h-14 shrink-0 items-center justify-between border-b border-zinc-800 bg-[#111] px-4">
        <button onClick={handleRejectAll} className="inline-flex items-center gap-2 rounded-md px-3 py-2 text-sm font-semibold hover:bg-zinc-800">
          <ArrowLeft className="h-4 w-4" />
          Review Changes
        </button>
        <div className="min-w-0 text-center">
          <div className="truncate text-sm font-semibold">
            {preview.package} {preview.from_version} → {preview.to_version}
          </div>
          <div className="text-xs text-zinc-500">
            {preview.summary.total_files_changed} files, +{preview.summary.total_additions} / -{preview.summary.total_deletions}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={handleApply}
            disabled={isApplying}
            className="inline-flex h-9 items-center gap-2 rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 text-sm font-semibold text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-60"
          >
            {isApplying ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
            Apply All
          </button>
          <button
            onClick={handleRejectAll}
            className="inline-flex h-9 items-center gap-2 rounded-md border border-red-500/40 bg-red-500/10 px-3 text-sm font-semibold text-red-300 hover:bg-red-500/20"
          >
            <X className="h-4 w-4" />
            Reject All
          </button>
        </div>
      </header>

      <main ref={scrollRef} className="min-h-0 flex-1 overflow-auto bg-[#0f0f0f] px-6 py-5 font-mono text-xs">
        <div className="mb-4 flex items-center justify-between rounded-lg border border-zinc-800 bg-zinc-950 px-4 py-3 font-sans">
          <div>
            <div className="text-sm font-semibold">{currentFile.relative_path}</div>
            <div className="text-xs text-zinc-500">+{currentFile.additions} / -{currentFile.deletions}</div>
          </div>
          <div className="text-xs text-zinc-500">{currentFileIndex + 1} of {preview.files.length}</div>
        </div>

        <div className="space-y-6">
          {currentFile.hunks.map((hunk) => (
            <HunkBlock
              key={hunk.hunk_id}
              hunk={hunk}
              decision={currentFileDecision?.hunks[hunk.hunk_id]?.decision ?? "pending"}
              onAccept={() => setHunkDecision(currentFile, hunk.hunk_id, "accepted")}
              onReject={() => setHunkDecision(currentFile, hunk.hunk_id, "rejected")}
            />
          ))}
        </div>
      </main>

      <footer className="flex h-14 shrink-0 items-center justify-between border-t border-zinc-800 bg-[#0a0a0a] px-4">
        <button
          onClick={() => goToFile(currentFileIndex - 1)}
          disabled={currentFileIndex === 0}
          className="inline-flex h-9 items-center gap-2 rounded-md border border-zinc-800 px-3 text-sm font-semibold hover:bg-zinc-900 disabled:opacity-40"
        >
          <ChevronLeft className="h-4 w-4" />
          Prev File
        </button>
        <button onClick={() => scrollRef.current?.scrollTo({ top: 0, behavior: "smooth" })} className="max-w-[45vw] truncate text-sm font-semibold hover:text-emerald-300">
          {currentFile.relative_path} ({currentFileIndex + 1}/{preview.files.length})
        </button>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setFileDecision(currentFile, "accepted")}
            className="inline-flex h-9 items-center gap-2 rounded-md border border-emerald-500/40 px-3 text-sm font-semibold text-emerald-300 hover:bg-emerald-500/10"
          >
            <Check className="h-4 w-4" />
            Accept File
          </button>
          <button
            onClick={() => setFileDecision(currentFile, "rejected")}
            className="inline-flex h-9 items-center gap-2 rounded-md border border-red-500/40 px-3 text-sm font-semibold text-red-300 hover:bg-red-500/10"
          >
            <X className="h-4 w-4" />
            Reject File
          </button>
          <button
            onClick={() => goToFile(currentFileIndex + 1)}
            disabled={currentFileIndex === preview.files.length - 1}
            className="inline-flex h-9 items-center gap-2 rounded-md border border-zinc-800 px-3 text-sm font-semibold hover:bg-zinc-900 disabled:opacity-40"
          >
            Next File
            <ChevronRight className="h-4 w-4" />
          </button>
        </div>
      </footer>
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
        "rounded-lg border border-zinc-800 bg-zinc-950/70 transition",
        decision === "accepted" && "border-emerald-500/40 bg-emerald-500/10",
        decision === "rejected" && "opacity-45 grayscale"
      )}
    >
      <div className="flex items-center justify-between border-b border-dashed border-zinc-800 px-3 py-2 font-sans">
        <div className="text-xs text-zinc-500">
          @@ -{hunk.old_start},{hunk.old_lines} +{hunk.new_start},{hunk.new_lines} @@
        </div>
        <div className="flex items-center gap-2">
          {decision === "accepted" && <span className="text-xs font-semibold text-emerald-300">Accepted</span>}
          {decision === "rejected" && <span className="text-xs font-semibold text-zinc-400">Rejected</span>}
          <button onClick={onAccept} className="rounded border border-emerald-500/40 px-2 py-1 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/10">
            ✓ Accept
          </button>
          <button onClick={onReject} className="rounded border border-red-500/40 px-2 py-1 text-xs font-semibold text-red-300 hover:bg-red-500/10">
            ✕ Reject
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
              change.type === "deletion" && "bg-[#3f1212] text-red-400",
              change.type === "addition" && "bg-[#0f2f1a] text-emerald-400"
            )}
          >
            <span className="select-none text-right text-zinc-600">
              {change.line_number_new ?? change.line_number_old ?? ""}
            </span>
            <span className="select-none text-center">
              {change.type === "addition" ? "+" : change.type === "deletion" ? "-" : " "}
            </span>
            <code className={cn(decision === "rejected" && change.type !== "context" && "line-through")}>{change.content || " "}</code>
          </div>
        ))}
      </div>
    </section>
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
