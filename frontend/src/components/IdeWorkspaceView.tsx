import React, { useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  FileCode2,
  Folder,
  FolderOpen,
  FolderTree,
  GitCompareArrows,
  GripVertical,
  ListTodo,
  Loader2,
  PackageOpen,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import { type PackageData } from "@/components/PackagesTable";
import {
  applyPreview,
  applyPreviewSelection,
  batchSandboxCheckStream,
  discardPreview,
  getFileContent,
  getProjectFiles,
  previewUpdateStream,
  rollbackPackage,
  type ApplyResponse,
  type ChangedFile,
  type PreviewFile,
  type PreviewHunk,
  type PreviewResponse,
  type PreviewStreamEvent,
  type ProjectFile,
} from "@/hooks/useDepGuard";
import { cn } from "@/lib/utils";
import {
  DiffReviewPanel,
  type DiffReviewActivityState,
} from "@/components/DiffReviewPanel";

interface IdeWorkspaceViewProps {
  folderPath: string;
  packages: PackageData[];
  onBack: () => void;
  onLog: (message: string, type: "info" | "success" | "error") => void;
  onPackagesUpdated?: (updater: (prev: PackageData[]) => PackageData[]) => void;
}

type DiffLine = {
  type: "same" | "added" | "removed";
  text: string;
  oldLine?: number;
  newLine?: number;
};

type StreamLine = {
  id: number;
  message: string;
  detail?: string;
  badge?: string;
  status: "running" | "success" | "error" | "info" | "sub" | "separator" | "header";
};

type ExplorerNode = {
  name: string;
  path: string;
  type: "folder" | "file";
  children: ExplorerNode[];
  file?: ProjectFile;
};

type CombinedDiffEntry = {
  packageName: string;
  fromVersion: string;
  toVersion: string;
  sessionId: string;
  files: PreviewFile[];
};

type HunkDecision = "accepted" | "rejected" | "pending";
// key = `${sessionId}:${hunkId}`
type AllHunkDecisions = Record<string, HunkDecision>;

interface MergedHunk {
  sessionId: string;
  hunkData: PreviewHunk;
}

interface MergedFileView {
  relativePath: string;
  filePath: string;
  totalAdditions: number;
  totalDeletions: number;
  mergedHunks: MergedHunk[];
}

function buildMergedFiles(entries: CombinedDiffEntry[]): MergedFileView[] {
  const fileMap = new Map<string, MergedFileView>();
  for (const entry of entries) {
    for (const file of entry.files) {
      if (!fileMap.has(file.relative_path)) {
        fileMap.set(file.relative_path, {
          relativePath: file.relative_path,
          filePath: file.file_path,
          totalAdditions: 0,
          totalDeletions: 0,
          mergedHunks: [],
        });
      }
      const merged = fileMap.get(file.relative_path)!;
      merged.totalAdditions += file.additions;
      merged.totalDeletions += file.deletions;
      for (const hunk of file.hunks) {
        merged.mergedHunks.push({ sessionId: entry.sessionId, hunkData: hunk });
      }
    }
  }
  for (const merged of fileMap.values()) {
    merged.mergedHunks.sort((a, b) => a.hunkData.old_start - b.hunkData.old_start);
  }
  return Array.from(fileMap.values());
}

const EXPLORER_MIN_WIDTH = 220;
const EXPLORER_DEFAULT_WIDTH = 300;
const EXPLORER_MAX_WIDTH = 520;

function clampExplorerWidth(width: number) {
  return Math.min(EXPLORER_MAX_WIDTH, Math.max(EXPLORER_MIN_WIDTH, width));
}

function normalizeDependencyVersion(version?: string) {
  return (version || "").replace(/^[\^~=<>v\s]+/i, "");
}

function isUpdateCandidate(pkg: PackageData) {
  const current = normalizeDependencyVersion(pkg.current_version);
  const latest = normalizeDependencyVersion(pkg.latest_version);
  return Boolean(pkg.name && current && latest && current !== latest);
}

export function IdeWorkspaceView({ folderPath, packages, onBack, onLog, onPackagesUpdated }: IdeWorkspaceViewProps) {
  const [files, setFiles] = useState<ProjectFile[]>([]);
  const [fileQuery, setFileQuery] = useState("");
  const [selectedFile, setSelectedFile] = useState("");
  const [fileContent, setFileContent] = useState("");
  const [isLoadingFiles, setIsLoadingFiles] = useState(false);
  const [isLoadingContent, setIsLoadingContent] = useState(false);
  const [updatingPackage, setUpdatingPackage] = useState("");
  const [changedFiles, setChangedFiles] = useState<ChangedFile[]>([]);
  const [activeChangedFile, setActiveChangedFile] = useState("");
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(() => new Set([""]));
  const [previewData, setPreviewData] = useState<PreviewResponse | null>(null);
  const [previewPackageName, setPreviewPackageName] = useState("");
  const [previewActiveFile, setPreviewActiveFile] = useState("");
  const [reviewActivity, setReviewActivity] = useState<DiffReviewActivityState | null>(null);
  const [streamLines, setStreamLines] = useState<StreamLine[]>([]);
  const [pendingUpdatePkg, setPendingUpdatePkg] = useState<PackageData | null>(null);
  const [isApplyingForModal, setIsApplyingForModal] = useState(false);
  const [lastCheckpointId, setLastCheckpointId] = useState("");
  const [isRollingBack, setIsRollingBack] = useState(false);
  const [rightPanelMode, setRightPanelMode] = useState<"dependencies" | "progress">("dependencies");
  const [explorerWidth, setExplorerWidth] = useState(EXPLORER_DEFAULT_WIDTH);
  const [isExplorerHidden, setIsExplorerHidden] = useState(false);
  const [isExplorerPeeking, setIsExplorerPeeking] = useState(false);
  const [isResizingExplorer, setIsResizingExplorer] = useState(false);
  const [updateQueue, setUpdateQueue] = useState<PackageData[]>([]);
  const queueRef = useRef<PackageData[]>([]);
  const [isBatchUpdating, setIsBatchUpdating] = useState(false);
  const [combinedDiffEntries, setCombinedDiffEntries] = useState<CombinedDiffEntry[] | null>(null);
  const [combinedActiveFile, setCombinedActiveFile] = useState("");
  const [combinedHunkDecisions, setCombinedHunkDecisions] = useState<AllHunkDecisions>({});
  const [isApplyingAll, setIsApplyingAll] = useState(false);
  const batchCollectedRef = useRef<PreviewResponse[]>([]);
  const explorerDragRef = useRef({ startX: 0, startWidth: EXPLORER_DEFAULT_WIDTH });

  const updateAllCandidates = useMemo(() => packages.filter(isUpdateCandidate), [packages]);

  const mergedFiles = useMemo(
    () => (combinedDiffEntries ? buildMergedFiles(combinedDiffEntries) : []),
    [combinedDiffEntries]
  );

  const combinedFilePaths = useMemo(() => {
    const s = new Set<string>();
    for (const f of mergedFiles) {
      s.add(f.filePath);
      s.add(f.relativePath);
    }
    return s;
  }, [mergedFiles]);

  const appendProgressLine = (line: Omit<StreamLine, "id">) =>
    setStreamLines((prev) => [...prev, { ...line, id: prev.length }]);

  const resetWorkspaceState = () => {
    setFiles([]);
    setFileQuery("");
    setSelectedFile("");
    setFileContent("");
    setChangedFiles([]);
    setActiveChangedFile("");
    setExpandedFolders(new Set([""]));
  };

  const loadFiles = async (selectFirstFile = false) => {
    if (!folderPath) return;

    setIsLoadingFiles(true);
    try {
      const data = await getProjectFiles(folderPath);
      setFiles(data.files);
      if ((selectFirstFile || !selectedFile) && data.files.length > 0) {
        setSelectedFile(data.files[0].path);
      }
    } catch (error) {
      onLog(`Could not load project files: ${error instanceof Error ? error.message : "Unknown error"}`, "error");
    } finally {
      setIsLoadingFiles(false);
    }
  };

  const loadContent = async (filePath: string) => {
    if (!folderPath || !filePath) return;

    setIsLoadingContent(true);
    try {
      const data = await getFileContent(folderPath, filePath);
      setSelectedFile(data.path);
      setFileContent(data.content);
      setActiveChangedFile("");
    } catch (error) {
      onLog(`Could not open ${filePath}: ${error instanceof Error ? error.message : "Unknown error"}`, "error");
    } finally {
      setIsLoadingContent(false);
    }
  };

  useEffect(() => {
    resetWorkspaceState();
    void loadFiles(true);
  }, [folderPath]);

  useEffect(() => {
    if (selectedFile && !activeChangedFile) {
      void loadContent(selectedFile);
    }
  }, [selectedFile]);

  const filteredFiles = useMemo(() => {
    const query = fileQuery.trim().toLowerCase();
    if (!query) return files;
    return files.filter((file) => file.path.toLowerCase().includes(query));
  }, [fileQuery, files]);

  const explorerTree = useMemo(() => buildExplorerTree(filteredFiles), [filteredFiles]);

  const previewFilePaths = useMemo(() => {
    if (!previewData) return new Set<string>();
    const s = new Set<string>();
    for (const f of previewData.files) {
      s.add(f.file_path);
      s.add(f.relative_path);
    }
    return s;
  }, [previewData]);

  const refreshWorkspace = async () => {
    await loadFiles();
    if (selectedFile && (!previewData || !previewFilePaths.has(selectedFile))) {
      await loadContent(selectedFile);
    }
  };

  useEffect(() => {
    if (fileQuery.trim()) {
      setExpandedFolders(getFolderPaths(filteredFiles));
    }
  }, [fileQuery, filteredFiles]);

  useEffect(() => {
    if (!isResizingExplorer) return;

    const handleMouseMove = (event: MouseEvent) => {
      const delta = event.clientX - explorerDragRef.current.startX;
      setExplorerWidth(clampExplorerWidth(explorerDragRef.current.startWidth + delta));
    };
    const handleMouseUp = () => setIsResizingExplorer(false);

    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);
    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
    };
  }, [isResizingExplorer]);

  const currentChangedFile = useMemo(
    () => changedFiles.find((file) => file.file === activeChangedFile) ?? null,
    [activeChangedFile, changedFiles]
  );

  const startExplorerResize = (event: ReactMouseEvent<HTMLDivElement>) => {
    event.preventDefault();
    explorerDragRef.current = { startX: event.clientX, startWidth: explorerWidth };
    setIsResizingExplorer(true);
  };

  const handleUpdate = async (pkg: PackageData, options?: { fromBatch?: boolean }) => {
    setUpdatingPackage(pkg.name);
    setRightPanelMode("progress");
    setReviewActivity(null);
    onLog(`Preparing IDE preview for ${pkg.name}.`, "info");

    const assistantLineForEvent = (event: PreviewStreamEvent): Omit<StreamLine, "id"> | null => {
      const msg = event.message ?? "";
      const versionRange = `${pkg.current_version || "current"} → ${pkg.latest_version || "target"}`;

      if (event.event === "phase" && event.phase === "ast_scan") {
        return {
          message: msg,
          status: "running",
          badge: "Finding usage",
          detail: `I am locating real ${pkg.name} calls in this project first, so DepGuard only patches code that actually uses the migration surface.`,
        };
      }
      if (event.event === "ast_done") {
        return {
          message: msg,
          status: "success",
          badge: "Usage map",
          detail: event.usage_count
            ? `These matches become the patch scope. Files outside this map are left alone unless a dependency version pin needs updating.`
            : `No direct API usage was found, so the update is likely limited to dependency metadata unless Scout finds a review-only migration.`,
        };
      }
      if (event.event === "phase" && event.phase === "scout") {
        return {
          message: msg,
          status: "running",
          badge: "Evidence",
          detail: `I am comparing ${pkg.name} ${versionRange} against migration docs and changelogs, then filtering for APIs used by this project.`,
        };
      }
      if (event.event === "scout_done") {
        return {
          message: msg,
          status: "success",
          badge: "Breaking changes",
          detail: event.breaking_changes_count
            ? `Scout found migration rules that may make existing code fail on the target version. Next, DepGuard will connect those rules to matched code.`
            : `Scout did not find a concrete breaking API match. DepGuard will avoid speculative source edits and only update safe metadata if needed.`,
        };
      }
      if (event.event === "breaking_change") {
        const target = event.new_api ? `Replacement candidate: ${event.new_api}.` : "No automatic replacement was documented, so this may require review.";
        return {
          message: msg,
          status: "sub",
          badge: event.change_type ? event.change_type.replace("_", " ") : "breaking",
          detail: `${event.description || "This API changed between the selected versions."} ${target}`,
        };
      }
      if (event.event === "phase" && event.phase === "patch") {
        return {
          message: msg,
          status: "running",
          badge: "Patch plan",
          detail: `The patch agent will edit only matched target blocks, preserve unrelated lines, and apply documented coupled obligations such as removed kwargs or changed call targets.`,
        };
      }
      if (event.event === "patch_file_done") {
        return {
          message: msg,
          status: event.success ? "sub" : "error",
          badge: event.success ? "Patched file" : "Needs attention",
          detail: event.success
            ? `Generated a preview for this file. Review the diff to see exactly what changed and why before applying.`
            : `The patch agent could not produce a safe edit for this file. DepGuard keeps it out of the preview instead of applying a risky change.`,
        };
      }
      if (event.event === "file_stats") {
        return {
          message: msg,
          status: "sub",
          badge: "Diff size",
          detail: `This preview changes ${event.additions ?? 0} added and ${event.deletions ?? 0} removed line(s). Small diffs are easier to audit before applying.`,
        };
      }
      if (event.event === "done") {
        return {
          message: msg,
          status: "success",
          badge: "Ready to review",
          detail: `The preview is not applied yet. Check the diff, accept or reject hunks, then the checker can validate the project after write.`,
        };
      }
      if (event.event === "error") {
        return {
          message: msg,
          status: "error",
          badge: "Stopped",
          detail: `DepGuard stopped before applying changes. Use this error text as the next debugging clue.`,
        };
      }
      if (event.event === "info") {
        return {
          message: msg,
          status: "info",
          badge: "Note",
          detail: msg.startsWith("Version pin:")
            ? `The dependency manifest must move to the target version so runtime and source changes stay in sync.`
            : undefined,
        };
      }
      return msg ? { message: msg, status: "info" } : null;
    };

    // Append a visual separator + header instead of clearing the log
    setStreamLines((prev) => [
      ...prev,
      ...(prev.length > 0 ? [{ id: prev.length, message: "", status: "separator" as const }] : []),
      { id: prev.length + (prev.length > 0 ? 1 : 0), message: pkg.name, status: "header" as const },
    ]);

    const onEvent = (event: PreviewStreamEvent) => {
      if (event.event === "patch_file_start") return;
      const line = assistantLineForEvent(event);
      if (line) appendProgressLine(line);
    };

    try {
      const result = await previewUpdateStream(folderPath, pkg, onEvent);
      if (result.files.length === 0) {
        onLog(`No file changes needed for ${pkg.name}.`, "success");
        if (options?.fromBatch) {
          startNextQueuedUpdate();
        }
      } else if (options?.fromBatch) {
        // Batch mode: collect preview without showing it, continue to next package
        batchCollectedRef.current = [...batchCollectedRef.current, result];
        appendProgressLine({
          message: `${pkg.name}: ${result.summary.total_files_changed} file(s) collected`,
          status: "success",
          badge: "Collected",
          detail: `+${result.summary.total_additions}/−${result.summary.total_deletions} — will be shown for review after all packages and checks finish.`,
        });
        startNextQueuedUpdate();
      } else {
        // Single package: show for immediate review
        setPreviewData(result);
        setPreviewPackageName(pkg.name);
        setPreviewActiveFile(result.files[0]?.file_path ?? "");
        setSelectedFile(result.files[0]?.file_path ?? "");
        setRightPanelMode("progress");
        onLog(`Preview ready for ${pkg.name}: ${result.summary.total_files_changed} file(s) changed.`, "info");
      }
      void loadFiles();
    } catch (error) {
      const errMsg = error instanceof Error ? error.message : "Unknown error";
      onLog(`Error previewing ${pkg.name}: ${errMsg}`, "error");
      if (options?.fromBatch) {
        appendProgressLine({
          message: `${pkg.name}: preview failed — skipping`,
          status: "error",
          badge: "Skipped",
          detail: errMsg,
        });
        startNextQueuedUpdate();
      }
    } finally {
      setUpdatingPackage("");
    }
  };

  const startNextQueuedUpdate = () => {
    const [nextPackage, ...remainingQueue] = queueRef.current;
    queueRef.current = remainingQueue;
    setUpdateQueue(remainingQueue);
    if (!nextPackage) {
      void runBatchSandboxCheckAndReview();
    } else {
      void handleUpdate(nextPackage, { fromBatch: true });
    }
  };

  const cancelUpdateAll = (message = "Update All queue cancelled.") => {
    queueRef.current = [];
    setUpdateQueue([]);
    setIsBatchUpdating(false);
    setPendingUpdatePkg(null);
    batchCollectedRef.current = [];
    setCombinedDiffEntries(null);
    setIsApplyingAll(false);
    appendProgressLine({
      message,
      status: "info",
      badge: "Batch",
      detail: "The current preview is left untouched; only the remaining queued package updates were cleared.",
    });
  };

  const requestUpdateAll = () => {
    if (updateAllCandidates.length === 0) {
      onLog("No outdated packages are available for Update All.", "info");
      return;
    }

    const [firstPackage, ...remainingPackages] = updateAllCandidates;
    batchCollectedRef.current = [];
    queueRef.current = remainingPackages;
    setUpdateQueue(remainingPackages);
    setIsBatchUpdating(true);
    setRightPanelMode("progress");
    appendProgressLine({
      message: `Update All queued ${updateAllCandidates.length} package${updateAllCandidates.length === 1 ? "" : "s"}`,
      status: "header",
    });
    appendProgressLine({
      message: `Starting with ${firstPackage.name}`,
      status: "running",
      badge: "Batch",
      detail: "DepGuard will process all packages automatically, then show all diffs for review.",
    });

    if (previewData) {
      setPendingUpdatePkg(firstPackage);
      return;
    }

    void handleUpdate(firstPackage, { fromBatch: true });
  };

  const runBatchSandboxCheckAndReview = async () => {
    setIsBatchUpdating(false);
    setRightPanelMode("progress");

    const collected = batchCollectedRef.current;
    batchCollectedRef.current = [];

    if (collected.length === 0) {
      appendProgressLine({
        message: "No file changes were needed across all packages.",
        status: "success",
        badge: "Batch",
        detail: "All packages were already at their target versions or required no source edits.",
      });
      return;
    }

    const sessionIds = collected.map((p) => p.session_id);

    appendProgressLine({ message: "", status: "separator" });
    appendProgressLine({ message: "Post-update verification", status: "header" });

    const onEvent = (event: PreviewStreamEvent) => {
      const msg = event.message ?? "";
      let line: Omit<StreamLine, "id"> | null = null;
      if (event.event === "phase" && event.phase === "verify") {
        line = { message: msg, status: "running", badge: "Sandbox", detail: "Running your project's build and test commands on all proposed changes in an isolated copy." };
      } else if (event.event === "phase" && event.phase === "repair") {
        line = { message: msg, status: "running", badge: "Auto-repair", detail: "The Repair Agent is fixing errors found in the sandbox before showing you the diff." };
      } else if (event.event === "verify_done") {
        line = { message: msg, status: "success", badge: "Sandbox ✓", detail: "All changes verified clean in the sandbox." };
      } else if (event.event === "verify_fail") {
        line = { message: msg, status: "error", badge: "Sandbox ✗", detail: "Errors detected — the Repair Agent will fix them before showing you the diff." };
      } else if (event.event === "repair_attempt") {
        line = { message: msg, status: "running", badge: `Repair #${event.attempt ?? ""}`, detail: "Sending error context and code slices to the LLM for targeted fixes." };
      } else if (event.event === "repair_done") {
        line = { message: msg, status: "success", badge: "Repair ✓", detail: "All errors resolved. The diffs you are about to review already include the repair fixes." };
      } else if (event.event === "repair_fail") {
        line = { message: msg, status: "error", badge: "Repair ✗", detail: "Repair could not resolve all errors — review the diffs carefully before applying." };
      } else if (event.event === "done") {
        line = { message: msg, status: "success", badge: "Ready" };
      } else if (event.event === "error") {
        line = { message: msg, status: "error", badge: "Error" };
      } else if (msg) {
        line = { message: msg, status: "info" };
      }
      if (line) appendProgressLine(line);
    };

    try {
      await batchSandboxCheckStream(folderPath, sessionIds, onEvent);
    } catch (error) {
      appendProgressLine({
        message: `Sandbox check failed: ${error instanceof Error ? error.message : "Unknown error"}`,
        status: "error",
        badge: "Error",
      });
    }

    if (collected.length > 0) {
      appendProgressLine({ message: "", status: "separator" });
      appendProgressLine({
        message: `Review ${collected.length} package change${collected.length !== 1 ? "s" : ""}`,
        status: "info",
        badge: "Review",
        detail: "All diffs are shown below. Accept to write to disk.",
      });
      const builtEntries = collected.map((p) => ({
        packageName: p.package,
        fromVersion: p.from_version,
        toVersion: p.to_version,
        sessionId: p.session_id,
        files: p.files,
      }));
      setCombinedDiffEntries(builtEntries);
      setCombinedActiveFile(buildMergedFiles(builtEntries)[0]?.filePath ?? "");
      setCombinedHunkDecisions({});
    }
  };

  const handleApplyAll = async (hunkDecisions: AllHunkDecisions = {}) => {
    if (!combinedDiffEntries) return;
    setIsApplyingAll(true);
    try {
      for (const entry of combinedDiffEntries) {
        const decisions = Object.fromEntries(
          entry.files.map((file) => {
            const hunkEntries = file.hunks.map((h) => {
              const d = hunkDecisions[`${entry.sessionId}:${h.hunk_id}`] ?? "pending";
              return [h.hunk_id, d === "rejected" ? "reject" : "accept"] as [string, string];
            });
            const values = hunkEntries.map((e) => e[1]);
            const fileDecision = values.every((v) => v === "reject")
              ? "reject"
              : values.every((v) => v === "accept")
              ? "accept"
              : "partial";
            return [
              file.relative_path,
              { file_decision: fileDecision, hunks: Object.fromEntries(hunkEntries) },
            ];
          })
        );
        await applyPreview(entry.sessionId, decisions);
        const newVersion = entry.toVersion;
        const pkgName = entry.packageName;
        onPackagesUpdated?.((prev) =>
          prev.map((p) => (p.name === pkgName ? { ...p, current_version: newVersion } : p))
        );
        appendProgressLine({
          message: `Applied ${entry.packageName}: ${entry.files.length} file(s)`,
          status: "success",
          badge: "Applied",
        });
      }
      setCombinedDiffEntries(null);
      setCombinedActiveFile("");
      setCombinedHunkDecisions({});
      setRightPanelMode("progress");
      appendProgressLine({ message: "All packages applied", status: "success", badge: "Done" });
      void loadFiles();
      if (selectedFile) void loadContent(selectedFile);
    } catch (error) {
      appendProgressLine({
        message: `Apply failed: ${error instanceof Error ? error.message : "Unknown error"}`,
        status: "error",
        badge: "Error",
      });
    } finally {
      setIsApplyingAll(false);
    }
  };

  const handleDiscardAll = async () => {
    if (!combinedDiffEntries) return;
    const entries = combinedDiffEntries;
    setCombinedDiffEntries(null);
    setCombinedActiveFile("");
    setCombinedHunkDecisions({});
    setRightPanelMode("progress");
    for (const entry of entries) {
      await discardPreview(entry.sessionId).catch(() => {});
    }
    appendProgressLine({ message: "All pending changes discarded", status: "info", badge: "Discarded" });
  };

  const reconcileCombinedSelectionResults = (
    results: Array<[string, Awaited<ReturnType<typeof applyPreviewSelection>>]>
  ) => {
    if (!combinedDiffEntries) return;

    const resultBySession = new Map(results);
    const nextEntries = combinedDiffEntries.flatMap((entry) => {
      const result = resultBySession.get(entry.sessionId);
      if (!result) return [entry];
      if (result.complete || !result.preview) return [];
      return [{
        ...entry,
        packageName: result.preview.package || entry.packageName,
        fromVersion: result.preview.from_version || entry.fromVersion,
        toVersion: result.preview.to_version || entry.toVersion,
        files: result.preview.files,
      }];
    });

    for (const [sessionId, result] of results) {
      const entry = combinedDiffEntries.find((item) => item.sessionId === sessionId);
      if (result.checkpoint_id) setLastCheckpointId(result.checkpoint_id);
      if (!entry || !result.complete || result.files_accepted.length === 0) continue;
      onPackagesUpdated?.((prev) =>
        prev.map((pkg) =>
          pkg.name === entry.packageName ? { ...pkg, current_version: entry.toVersion } : pkg
        )
      );
    }

    setCombinedDiffEntries(nextEntries.length > 0 ? nextEntries : null);
    setCombinedHunkDecisions({});
    const nextMergedFiles = buildMergedFiles(nextEntries);
    const activeStillPending = nextMergedFiles.some(
      (file) => file.filePath === combinedActiveFile || file.relativePath === combinedActiveFile
    );
    const nextActiveFile = activeStillPending
      ? combinedActiveFile
      : nextMergedFiles[0]?.filePath ?? "";
    setCombinedActiveFile(nextActiveFile);
    if (nextActiveFile) {
      setSelectedFile(nextActiveFile);
    } else {
      setRightPanelMode("progress");
      appendProgressLine({ message: "All reviewed changes resolved", status: "success", badge: "Done" });
      if (selectedFile) void loadContent(selectedFile);
    }
    void loadFiles();
  };

  const applyCombinedSelections = async (
    requests: Array<[string, object]>,
    successMessage?: string,
  ) => {
    if (!combinedDiffEntries || requests.length === 0) return;
    setIsApplyingAll(true);
    try {
      const results: Array<[string, Awaited<ReturnType<typeof applyPreviewSelection>>]> = [];
      for (const [sessionId, decisions] of requests) {
        const result = await applyPreviewSelection(sessionId, decisions);
        results.push([sessionId, result]);
      }
      reconcileCombinedSelectionResults(results);
      if (successMessage) {
        appendProgressLine({ message: successMessage, status: "sub", badge: "Reviewed" });
      }
    } catch (error) {
      appendProgressLine({
        message: `Apply selection failed: ${error instanceof Error ? error.message : "Unknown error"}`,
        status: "error",
        badge: "Error",
      });
      setCombinedHunkDecisions({});
    } finally {
      setIsApplyingAll(false);
    }
  };

  const handlePreviewApplied = (result: ApplyResponse) => {
    onLog(`Applied ${previewPackageName}: ${result.files_accepted.length} file(s) accepted.`, "success");
    if (result.verification?.status === "passed") {
      onLog(`Checker passed for ${previewPackageName}.`, "success");
    } else if (result.repair?.status === "success") {
      onLog(`Repair Agent fixed ${previewPackageName} after checker feedback.`, "success");
    } else if (result.verification?.status === "failed") {
      onLog(`Checker still reports errors for ${previewPackageName}. Review the pipeline output.`, "error");
    }

    // Append apply result to the persistent log
    const accepted = result.files_accepted.length;
    const rejected = result.files_rejected.length;
    setStreamLines((prev) => [
      ...prev,
      { id: prev.length,     message: `Applied — ${accepted} accepted, ${rejected} rejected`, status: "success" as const },
      ...(result.verification?.status === "passed"
        ? [{ id: prev.length + 1, message: "Project checker passed ✓", status: "success" as const }]
        : result.verification?.status === "failed"
        ? [{ id: prev.length + 1, message: `Checker: ${result.verification.message ?? "errors found"}`, status: "error" as const }]
        : []),
      ...(result.repair?.status === "success"
        ? [{ id: prev.length + 2, message: "Repair Agent fixed remaining issues ✓", status: "success" as const }]
        : []),
    ]);

    if (result.checkpoint_id) setLastCheckpointId(result.checkpoint_id);

    // Immediately update the version in the packages list so the dashboard reflects the change
    if (result.files_accepted.length > 0 && previewData) {
      const newVersion = previewData.to_version;
      const pkgName = previewPackageName;
      onPackagesUpdated?.((prev) =>
        prev.map((p) =>
          p.name === pkgName
            ? { ...p, current_version: newVersion }
            : p
        )
      );
    }

    setPreviewData(null);
    setPreviewPackageName("");
    setPreviewActiveFile("");
    setReviewActivity(null);
    setRightPanelMode("progress");
    void loadFiles();
    if (selectedFile) {
      void loadContent(selectedFile);
    }
  };

  const handlePreviewDiscarded = () => {
    onLog(`Discarded preview for ${previewPackageName}.`, "info");
    setPreviewData(null);
    setPreviewPackageName("");
    setPreviewActiveFile("");
    setReviewActivity(null);
    setRightPanelMode("dependencies");
    if (selectedFile) {
      void loadContent(selectedFile);
    }
  };

  const handlePreviewUpdated = (preview: PreviewResponse) => {
    setPreviewData(preview);
    const pendingPaths = new Set<string>();
    preview.files.forEach((file) => {
      pendingPaths.add(file.file_path);
      pendingPaths.add(file.relative_path);
    });
    const currentPath = previewActiveFile || selectedFile;
    const nextPath = pendingPaths.has(currentPath)
      ? currentPath
      : preview.files[0]?.file_path ?? "";

    setPreviewActiveFile(nextPath);
    if (nextPath) {
      setSelectedFile(nextPath);
    } else if (selectedFile) {
      void loadContent(selectedFile);
    }
    void loadFiles();
  };

  const handleRollback = async () => {
    if (!lastCheckpointId || isRollingBack) return;
    setIsRollingBack(true);
    setStreamLines((prev) => [
      ...prev,
      { id: prev.length, message: `Rolling back to checkpoint ${lastCheckpointId}…`, status: "running" as const },
    ]);
    try {
      await rollbackPackage(lastCheckpointId, folderPath);
      setLastCheckpointId("");
      setStreamLines((prev) => [
        ...prev,
        { id: prev.length, message: "Rollback successful — code restored", status: "success" as const },
      ]);
      onLog("Rollback successful.", "success");
      void loadFiles();
      if (selectedFile) void loadContent(selectedFile);
    } catch (error) {
      const msg = error instanceof Error ? error.message : "Rollback failed";
      setStreamLines((prev) => [
        ...prev,
        { id: prev.length, message: `Rollback failed: ${msg}`, status: "error" as const },
      ]);
      onLog(`Rollback failed: ${msg}`, "error");
    } finally {
      setIsRollingBack(false);
    }
  };

  const requestUpdate = (pkg: PackageData) => {
    if (previewData && !pendingUpdatePkg) {
      setPendingUpdatePkg(pkg);
      setRightPanelMode("progress");
      return;
    }
    void handleUpdate(pkg);
  };

  const handleModalApplyAndContinue = async () => {
    if (!previewData || !pendingUpdatePkg) return;
    setIsApplyingForModal(true);
    try {
      await applyPreview(previewData.session_id, buildAllAcceptDecisions(previewData));
      setPreviewData(null);
      setPreviewPackageName("");
      setPreviewActiveFile("");
      setReviewActivity(null);
      const pkg = pendingUpdatePkg;
      setPendingUpdatePkg(null);
      void handleUpdate(pkg, { fromBatch: isBatchUpdating });
    } catch (error) {
      onLog(`Error applying preview: ${error instanceof Error ? error.message : "Unknown error"}`, "error");
    } finally {
      setIsApplyingForModal(false);
    }
  };

  const handleModalDiscard = async () => {
    if (!previewData || !pendingUpdatePkg) return;
    setIsApplyingForModal(true);
    try {
      await discardPreview(previewData.session_id);
      setPreviewData(null);
      setPreviewPackageName("");
      setPreviewActiveFile("");
      setReviewActivity(null);
      const pkg = pendingUpdatePkg;
      setPendingUpdatePkg(null);
      void handleUpdate(pkg, { fromBatch: isBatchUpdating });
    } catch (error) {
      onLog(`Error discarding preview: ${error instanceof Error ? error.message : "Unknown error"}`, "error");
    } finally {
      setIsApplyingForModal(false);
    }
  };

  const openChangedFile = (file: ChangedFile) => {
    setActiveChangedFile(file.file);
    setSelectedFile(file.file);
  };

  const toggleFolder = (path: string) => {
    setExpandedFolders((current) => {
      const next = new Set(current);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  };

  return (
    <main className="flex h-full min-h-0 flex-col overflow-hidden bg-background">
      <div className="flex h-11 shrink-0 items-center justify-between border-b bg-card px-3">
        <button
          onClick={onBack}
          className="inline-flex h-8 items-center gap-2 rounded-md border bg-background px-2.5 text-xs font-semibold transition hover:bg-muted"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to Dashboard
        </button>
        <div className="hidden min-w-0 text-right md:block">
          <p className="max-w-[520px] truncate text-xs font-medium">{folderPath}</p>
        </div>
      </div>

      <div
        className={cn(
          "grid min-h-0 flex-1 overflow-hidden transition-[grid-template-columns] duration-200",
          isResizingExplorer && "cursor-col-resize select-none"
        )}
        style={{
          gridTemplateColumns: isExplorerHidden
            ? `${isExplorerPeeking ? 44 : 8}px minmax(0, 1fr) minmax(280px, 25%)`
            : `${explorerWidth}px minmax(0, 1fr) minmax(280px, 25%)`,
        }}
      >
        <aside
          onMouseEnter={() => isExplorerHidden && setIsExplorerPeeking(true)}
          onMouseLeave={() => isExplorerHidden && setIsExplorerPeeking(false)}
          onClick={() => {
            if (!isExplorerHidden) return;
            setIsExplorerHidden(false);
            setIsExplorerPeeking(false);
          }}
          className={cn(
            "relative flex min-h-0 min-w-0 flex-col overflow-hidden border-b bg-card/70 lg:border-b-0 lg:border-r",
            isExplorerHidden && "cursor-pointer bg-card/90"
          )}
        >
          {isExplorerHidden ? (
            <div
              className={cn(
                "flex h-full items-start justify-center border-r border-border pt-3 text-muted-foreground transition-colors hover:border-zinc-500/70 hover:bg-muted/60 hover:text-zinc-200",
                !isExplorerPeeking && "text-transparent"
              )}
              title="Show Explorer"
            >
              <PanelLeftOpen className="h-4 w-4 shrink-0" />
            </div>
          ) : (
            <>
          <div className="flex h-10 shrink-0 items-center gap-2 border-b px-3">
            <FolderTree className="h-4 w-4" />
            <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-100">Explorer</h2>
            <button
              onClick={() => void refreshWorkspace()}
              className="ml-auto rounded-md p-1 text-muted-foreground transition hover:bg-muted hover:text-foreground"
              title="Refresh Explorer and editor"
            >
              {isLoadingFiles || isLoadingContent ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            </button>
            <button
              onClick={() => {
                setIsExplorerHidden(true);
                setIsExplorerPeeking(false);
              }}
              className="rounded-md p-1 text-muted-foreground transition hover:bg-muted hover:text-foreground"
              title="Hide Explorer"
            >
              <PanelLeftClose className="h-4 w-4" />
            </button>
          </div>
          <div className="shrink-0 border-b p-2">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <input
                value={fileQuery}
                onChange={(event) => setFileQuery(event.target.value)}
                placeholder="Search files"
                className="h-8 w-full rounded-md border bg-background pl-8 pr-2 text-sm outline-none transition focus:border-primary"
              />
            </div>
          </div>
          <div className="min-h-0 flex-1 overflow-auto px-2 py-2">
            {isLoadingFiles && files.length === 0 ? (
              <div className="flex items-center gap-2 rounded-lg border border-dashed p-3 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                Loading project files...
              </div>
            ) : explorerTree.children.length === 0 ? (
              <div className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">No files found.</div>
            ) : (
              explorerTree.children.map((node) => (
                <ExplorerTreeNode
                  key={node.path}
                  node={node}
                  depth={0}
                  selectedFile={selectedFile}
                  expandedFolders={expandedFolders}
                  previewFilePaths={combinedDiffEntries ? combinedFilePaths : previewFilePaths}
                  onToggleFolder={toggleFolder}
                  onSelectFile={(file) => {
                    if (combinedDiffEntries) {
                      setSelectedFile(file.path);
                      if (combinedFilePaths.has(file.path)) {
                        setCombinedActiveFile(file.path);
                      } else {
                        void loadContent(file.path);
                      }
                      return;
                    }
                    if (previewData) {
                      setSelectedFile(file.path);
                      if (previewFilePaths.has(file.path)) {
                        setPreviewActiveFile(file.path);
                      } else {
                        void loadContent(file.path);
                      }
                      return;
                    }
                    setActiveChangedFile("");
                    setSelectedFile(file.path);
                  }}
                />
              ))
            )}
          </div>
          <div
            role="separator"
            aria-label="Resize Explorer"
            aria-orientation="vertical"
            title="Drag to resize Explorer. Double-click to reset."
            onMouseDown={startExplorerResize}
            onDoubleClick={() => setExplorerWidth(EXPLORER_DEFAULT_WIDTH)}
            className="absolute right-0 top-0 z-20 flex h-full w-2 translate-x-1 cursor-col-resize items-center justify-center text-transparent transition hover:bg-sky-500/20 hover:text-sky-300"
          >
            <GripVertical className="h-4 w-4" />
          </div>
            </>
          )}
        </aside>

        <section className="relative flex min-h-0 min-w-0 flex-col overflow-hidden border-b bg-[#101010] lg:border-b-0 lg:border-r">
          {!previewData && !combinedDiffEntries && (
            <div className="flex h-10 shrink-0 items-center justify-between border-b bg-card px-3">
              <div className="flex min-w-0 items-center gap-2">
                <GitCompareArrows className="h-4 w-4 shrink-0" />
                <div className="min-w-0">
                  <h2 className="text-[11px] font-semibold uppercase tracking-wider text-zinc-100">
                    {currentChangedFile ? "Diff Viewer" : "Editor"}
                  </h2>
                  <p className="truncate text-xs text-zinc-300">{selectedFile || "Select a file"}</p>
                </div>
              </div>
            </div>
          )}

          {combinedDiffEntries ? (
            <>
              {/* Diff panel — hidden when browsing an unchanged file */}
              <div className={cn("flex-1 min-h-0 overflow-hidden", !combinedFilePaths.has(selectedFile) && "hidden")}>
                <CombinedDiffPanel
                  entries={combinedDiffEntries}
                  allFiles={mergedFiles}
                  currentFile={
                    mergedFiles.find((f) => f.filePath === combinedActiveFile || f.relativePath === combinedActiveFile)
                    ?? mergedFiles[0]
                    ?? null
                  }
                  onFileChange={(filePath) => {
                    setCombinedActiveFile(filePath);
                    setSelectedFile(filePath);
                  }}
                  folderPath={folderPath}
                  decisions={combinedHunkDecisions}
                  onDecisionChange={setCombinedHunkDecisions}
                  onApplyHunkDecision={(file, sessionId, hunkId, decision) => {
                    const shortFile = file.relativePath.split("/").pop() ?? file.relativePath;
                    void applyCombinedSelections(
                      [[sessionId, buildCombinedHunkSelectionDecision(file, hunkId, decision)]],
                      `Hunk ${decision === "accepted" ? "accepted" : "rejected"} in ${shortFile}`,
                    );
                  }}
                  onApplyFileDecision={(file, decision) => {
                    const shortFile = file.relativePath.split("/").pop() ?? file.relativePath;
                    void applyCombinedSelections(
                      buildCombinedFileSelectionDecisions(file, decision),
                      `${decision === "accepted" ? "Accepted" : "Rejected"} all hunks in ${shortFile}`,
                    );
                  }}
                  onAcceptAll={() => {
                    setCombinedDiffEntries(null);
                    setCombinedActiveFile("");
                    setCombinedHunkDecisions({});
                    setRightPanelMode("progress");
                    void handleApplyAll({});
                  }}
                  onApplyAll={() => void handleApplyAll(combinedHunkDecisions)}
                  onDiscardAll={() => void handleDiscardAll()}
                  isApplying={isApplyingAll}
                />
              </div>
              {/* Regular file view when an unchanged file is selected */}
              {!combinedFilePaths.has(selectedFile) && (
                <div className="flex flex-1 min-h-0 flex-col overflow-hidden">
                  <div className="flex h-10 shrink-0 items-center justify-between border-b bg-card px-3">
                    <div className="flex min-w-0 items-center gap-2">
                      <FileCode2 className="h-4 w-4 shrink-0 text-zinc-400" />
                      <p className="min-w-0 truncate text-xs text-zinc-200">{selectedFile || "Select a file"}</p>
                    </div>
                    {combinedActiveFile && (
                      <button
                        onClick={() => setSelectedFile(combinedActiveFile)}
                        className="inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md border bg-background px-2 text-xs font-semibold transition hover:bg-muted"
                      >
                        <GitCompareArrows className="h-3.5 w-3.5" />
                        Back to diff
                      </button>
                    )}
                  </div>
                  {isLoadingContent ? (
                    <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Loading file
                    </div>
                  ) : (
                    <CodeViewer content={fileContent} />
                  )}
                </div>
              )}
            </>
          ) : previewData ? (
            <>
              {/* Always mounted — hidden when browsing a non-preview file so decision state is preserved */}
              <div className={cn("flex-1 min-h-0 overflow-hidden", !previewFilePaths.has(selectedFile) && "hidden")}>
                <DiffReviewPanel
                  preview={previewData}
                  layout="embedded"
                  folderPath={folderPath}
                  activeFilePath={previewActiveFile}
                  onFileChange={(fp) => { setPreviewActiveFile(fp); setSelectedFile(fp); }}
                  onActivityChange={setReviewActivity}
                  onApplied={handlePreviewApplied}
                  onDiscarded={handlePreviewDiscarded}
                  onError={(message) => onLog(message, "error")}
                  onPreviewUpdated={handlePreviewUpdated}
                  onDecision={(file, scope, decision) => {
                    const shortFile = file.split("/").pop() ?? file;
                    const label = scope === "file"
                      ? `${decision === "accepted" ? "Accepted" : "Rejected"} all hunks — ${shortFile}`
                      : `Hunk ${decision === "accepted" ? "accepted" : "rejected"} in ${shortFile}`;
                    const status: StreamLine["status"] = decision === "accepted" ? "sub" : "error";
                    setStreamLines((prev) => [
                      ...prev,
                      { id: prev.length, message: label, status },
                    ]);
                    setRightPanelMode("progress");
                  }}
                />
              </div>
              {/* Regular file view while preview is active */}
              {!previewFilePaths.has(selectedFile) && (
                <div className="flex flex-1 min-h-0 flex-col overflow-hidden">
                  <div className="flex h-10 shrink-0 items-center justify-between border-b bg-card px-3">
                    <div className="flex min-w-0 items-center gap-2">
                      <FileCode2 className="h-4 w-4 shrink-0 text-zinc-400" />
                      <p className="min-w-0 truncate text-xs text-zinc-200">{selectedFile || "Select a file"}</p>
                    </div>
                    {previewActiveFile && (
                      <button
                        onClick={() => setSelectedFile(previewActiveFile)}
                        className="inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md border bg-background px-2 text-xs font-semibold transition hover:bg-muted"
                      >
                        <GitCompareArrows className="h-3.5 w-3.5" />
                        Back to diff
                      </button>
                    )}
                  </div>
                  {isLoadingContent ? (
                    <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Loading file
                    </div>
                  ) : (
                    <CodeViewer content={fileContent} />
                  )}
                </div>
              )}
            </>
          ) : isLoadingFiles && !selectedFile ? (
            <div className="flex min-h-0 flex-1 items-center justify-center text-sm text-muted-foreground">
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              Loading project workspace
            </div>
          ) : isLoadingContent ? (
            <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto text-sm text-muted-foreground">
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              Loading file
            </div>
          ) : currentChangedFile ? (
            <DiffViewer before={currentChangedFile.before} after={currentChangedFile.after} />
          ) : (
            <CodeViewer content={fileContent} />
          )}

          {changedFiles.length > 0 && (
            <ChangedFilesPopup
              files={changedFiles}
              activeFile={activeChangedFile}
              onOpenFile={openChangedFile}
              onClose={() => setChangedFiles([])}
            />
          )}
        </section>

        <aside className="flex min-h-0 min-w-0 flex-col overflow-hidden bg-card/70">
          <div className="flex h-10 shrink-0 items-center gap-2 border-b px-3">
            <PackageOpen className="h-4 w-4" />
            <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-100">Right Panel</h2>
            <div className="ml-auto flex rounded-md border bg-background p-0.5">
              <button
                onClick={() => setRightPanelMode("dependencies")}
                className={cn(
                  "rounded px-2 py-1 text-[11px] font-semibold transition",
                  rightPanelMode === "dependencies" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"
                )}
              >
                Dependencies
              </button>
              <button
                onClick={() => setRightPanelMode("progress")}
                className={cn(
                  "rounded px-2 py-1 text-[11px] font-semibold transition",
                  rightPanelMode === "progress" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"
                )}
              >
                Progress
              </button>
            </div>
          </div>
          {rightPanelMode === "progress" ? (
            <StreamingProgressPanel
              lines={streamLines}
              packageName={updatingPackage || previewPackageName}
              reviewActivity={reviewActivity}
              isStreaming={Boolean(updatingPackage)}
              pendingPackage={pendingUpdatePkg && previewData ? pendingUpdatePkg.name : undefined}
              currentPackage={previewPackageName}
              isApplyingForPending={isApplyingForModal}
              onApplyAndContinue={() => void handleModalApplyAndContinue()}
              onDiscard={() => void handleModalDiscard()}
              onCancel={() => (isBatchUpdating ? cancelUpdateAll() : setPendingUpdatePkg(null))}
              queuedPackageCount={updateQueue.length}
              isBatchUpdating={isBatchUpdating}
              onCancelBatch={() => cancelUpdateAll()}
              checkpointId={lastCheckpointId}
              isRollingBack={isRollingBack}
              onRollback={() => void handleRollback()}
            />
          ) : (
            <DependenciesPanel
              packages={packages}
              updatingPackage={updatingPackage}
              updateAllCount={updateAllCandidates.length}
              isBatchUpdating={isBatchUpdating}
              onUpdateAll={requestUpdateAll}
              onCancelUpdateAll={() => cancelUpdateAll()}
              onUpdate={requestUpdate}
            />
          )}
        </aside>
      </div>

    </main>
  );
}

function buildAllAcceptDecisions(preview: PreviewResponse) {
  return Object.fromEntries(
    preview.files.map((file) => [
      file.relative_path,
      {
        file_decision: "accept",
        hunks: Object.fromEntries(file.hunks.map((h) => [h.hunk_id, "accept"])),
      },
    ])
  );
}

function buildCombinedHunkSelectionDecision(file: MergedFileView, hunkId: string, decision: HunkDecision) {
  return {
    [file.relativePath]: {
      file_decision: "partial",
      hunks: {
        [hunkId]: decision === "rejected" ? "reject" : "accept",
      },
    },
  };
}

function buildCombinedFileSelectionDecisions(file: MergedFileView, decision: HunkDecision): Array<[string, object]> {
  const applyDecision = decision === "rejected" ? "reject" : "accept";
  const hunksBySession = new Map<string, string[]>();
  for (const { sessionId, hunkData } of file.mergedHunks) {
    const hunks = hunksBySession.get(sessionId) ?? [];
    hunks.push(hunkData.hunk_id);
    hunksBySession.set(sessionId, hunks);
  }

  return Array.from(hunksBySession.entries()).map(([sessionId, hunkIds]) => [
    sessionId,
    {
      [file.relativePath]: {
        file_decision: applyDecision,
        hunks: Object.fromEntries(hunkIds.map((hunkId) => [hunkId, applyDecision])),
      },
    },
  ]);
}

interface MergedUnifiedRow {
  type: "gap" | "context" | "deletion" | "addition";
  oldLine: number | null;
  newLine: number | null;
  content: string;
  sessionId: string | null;
  hunkId: string | null;
  hunk: PreviewHunk | null;
}

function buildMergedUnifiedRows(originalLines: string[], mergedHunks: MergedHunk[]): MergedUnifiedRow[] {
  const rows: MergedUnifiedRow[] = [];
  let oldCursor = 1;
  let newOffset = 0;
  const sorted = [...mergedHunks].sort((a, b) => a.hunkData.old_start - b.hunkData.old_start);
  for (const { sessionId, hunkData: hunk } of sorted) {
    for (let ln = oldCursor; ln < hunk.old_start; ln++) {
      rows.push({ type: "gap", oldLine: ln, newLine: ln + newOffset, content: originalLines[ln - 1] ?? "", sessionId: null, hunkId: null, hunk: null });
    }
    for (const c of hunk.changes) {
      rows.push({ type: c.type, oldLine: c.line_number_old, newLine: c.line_number_new, content: c.content, sessionId, hunkId: hunk.hunk_id, hunk });
    }
    newOffset += hunk.new_lines - hunk.old_lines;
    oldCursor = hunk.old_start + hunk.old_lines;
  }
  for (let ln = oldCursor; ln <= originalLines.length; ln++) {
    rows.push({ type: "gap", oldLine: ln, newLine: ln + newOffset, content: originalLines[ln - 1] ?? "", sessionId: null, hunkId: null, hunk: null });
  }
  return rows;
}

function CombinedDiffPanel({
  entries,
  allFiles,
  currentFile,
  folderPath,
  onFileChange,
  decisions,
  onDecisionChange,
  onApplyHunkDecision,
  onApplyFileDecision,
  onAcceptAll,
  onApplyAll,
  onDiscardAll,
  isApplying,
}: {
  entries: CombinedDiffEntry[];
  allFiles: MergedFileView[];
  currentFile: MergedFileView | null;
  folderPath: string;
  onFileChange: (filePath: string) => void;
  decisions: AllHunkDecisions;
  onDecisionChange: React.Dispatch<React.SetStateAction<AllHunkDecisions>>;
  onApplyHunkDecision: (file: MergedFileView, sessionId: string, hunkId: string, decision: HunkDecision) => void;
  onApplyFileDecision: (file: MergedFileView, decision: HunkDecision) => void;
  onAcceptAll: () => void;
  onApplyAll: () => void;
  onDiscardAll: () => void;
  isApplying: boolean;
}) {
  const allMergedFiles = allFiles;
  const scrollRef = useRef<HTMLDivElement>(null);
  const [isFileListOpen, setIsFileListOpen] = useState(false);
  const [originalLines, setOriginalLines] = useState<string[]>([]);
  const [loadingFile, setLoadingFile] = useState(false);
  const hunkSignature = useMemo(
    () => currentFile?.mergedHunks.map(({ sessionId, hunkData }) => `${sessionId}:${hunkData.hunk_id}:${hunkData.old_start}:${hunkData.old_lines}:${hunkData.new_start}:${hunkData.new_lines}`).join("|") ?? "",
    [currentFile]
  );

  useEffect(() => {
    if (!currentFile) return;
    setLoadingFile(true);
    setOriginalLines([]);
    getFileContent(folderPath, currentFile.filePath)
      .then((r) => setOriginalLines(r.content.split("\n")))
      .catch(() => setOriginalLines([]))
      .finally(() => setLoadingFile(false));
  }, [currentFile?.filePath, folderPath, hunkSignature]);

  const rows = useMemo(
    () => (currentFile && originalLines.length > 0 ? buildMergedUnifiedRows(originalLines, currentFile.mergedHunks) : []),
    [originalLines, currentFile]
  );

  const currentIndex = currentFile
    ? allMergedFiles.findIndex((f) => f.filePath === currentFile.filePath)
    : 0;

  const goToFile = (index: number) => {
    if (allMergedFiles.length === 0) return;
    const wrapped = ((index % allMergedFiles.length) + allMergedFiles.length) % allMergedFiles.length;
    onFileChange(allMergedFiles[wrapped].filePath);
    scrollRef.current?.scrollTo({ top: 0, behavior: "smooth" });
  };

  const setHunkDecision = (sessionId: string, hunkId: string, d: HunkDecision) => {
    onDecisionChange((prev) => ({ ...prev, [`${sessionId}:${hunkId}`]: d }));
    if (currentFile) {
      onApplyHunkDecision(currentFile, sessionId, hunkId, d);
    }
  };

  const getDecision = (sessionId: string, hunkId: string): HunkDecision =>
    decisions[`${sessionId}:${hunkId}`] ?? "pending";

  const setFileDecision = (file: MergedFileView, d: HunkDecision) => {
    onDecisionChange((prev) => ({
      ...prev,
      ...Object.fromEntries(file.mergedHunks.map(({ sessionId, hunkData }) => [`${sessionId}:${hunkData.hunk_id}`, d])),
    }));
    onApplyFileDecision(file, d);
  };

  const totalAdditions = allMergedFiles.reduce((s, f) => s + f.totalAdditions, 0);
  const totalDeletions = allMergedFiles.reduce((s, f) => s + f.totalDeletions, 0);

  return (
    <div className="relative flex h-full min-h-0 flex-col overflow-hidden">
      {/* ── Toolbar ── */}
      <div className="flex h-11 shrink-0 items-center justify-between border-b bg-card px-3">
        <div className="flex items-center gap-3">
          <GitCompareArrows className="h-4 w-4 shrink-0 text-sky-400" />
          <span className="text-xs font-semibold text-zinc-100">
            {entries.length} package{entries.length !== 1 ? "s" : ""}
          </span>
          <span className="text-[11px] text-zinc-500">
            {allMergedFiles.length} file{allMergedFiles.length !== 1 ? "s" : ""}&nbsp;·&nbsp;
            <span className="text-emerald-400">+{totalAdditions}</span>
            {" / "}
            <span className="text-red-400">−{totalDeletions}</span>
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={onAcceptAll}
            disabled={isApplying}
            className="inline-flex h-7 items-center gap-1.5 rounded-md bg-emerald-600 px-3 text-xs font-semibold text-white transition hover:bg-emerald-500 disabled:opacity-50"
          >
            {isApplying ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
            Accept All
          </button>
          <button
            onClick={onDiscardAll}
            disabled={isApplying}
            className="inline-flex h-7 items-center gap-1.5 rounded-md border border-red-700/40 bg-red-950/50 px-3 text-xs font-semibold text-red-400 transition hover:bg-red-900/50 disabled:opacity-50"
          >
            <X className="h-3.5 w-3.5" />
            Reject All
          </button>
          <button
            onClick={onApplyAll}
            disabled={isApplying}
            className="inline-flex h-7 items-center gap-1.5 rounded-md border bg-background px-3 text-xs font-semibold text-muted-foreground transition hover:bg-muted hover:text-foreground disabled:opacity-50"
          >
            Apply
          </button>
        </div>
      </div>

      {/* ── File sub-header ── */}
      {currentFile && (
        <div className="flex h-11 shrink-0 items-center gap-2 border-b bg-background px-4">
          <FileCode2 className="h-4 w-4 shrink-0 text-muted-foreground" />
          <span className="min-w-0 flex-1 truncate text-sm font-semibold">{currentFile.relativePath}</span>
          <span className="shrink-0 text-xs text-muted-foreground">
            <span className="text-emerald-400">+{currentFile.totalAdditions}</span>
            {" / "}
            <span className="text-red-400">−{currentFile.totalDeletions}</span>
          </span>
        </div>
      )}

      {/* ── Diff pane ── */}
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto font-mono text-xs leading-5">
        {!currentFile ? (
          <div className="flex h-full items-center justify-center text-sm text-zinc-500">
            Select a changed file in the Explorer to review
          </div>
        ) : loadingFile ? (
          <div className="flex items-center justify-center py-16">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        ) : (
          <div className="overflow-x-auto pb-16">
            {rows.map((row, index) => {
              const prevRow = index > 0 ? rows[index - 1] : null;
              const isHunkStart = row.hunkId !== null && prevRow?.hunkId !== row.hunkId;
              const decision = row.sessionId && row.hunkId ? getDecision(row.sessionId, row.hunkId) : "pending";

              // Block-level visual: accepted = red lines gone, rejected = green lines gone
              if (decision === "accepted" && row.type === "deletion") return null;
              if (decision === "rejected" && row.type === "addition") return null;

              return (
                <div key={`row-${index}`}>
                  {isHunkStart && row.hunk !== null && (
                    <div
                      className={cn(
                        "sticky top-0 z-10 flex items-center justify-between border-y border-dashed px-3 py-1 backdrop-blur-sm",
                        decision === "accepted" && "border-emerald-500/30 bg-emerald-950/30",
                        decision === "rejected" && "border-red-500/20 bg-red-950/20 opacity-60",
                        decision === "pending" && "border-zinc-700 bg-[#0d1117]/95"
                      )}
                    >
                      <span className="font-sans text-[10px] text-zinc-500">
                        @@ -{row.hunk.old_start},{row.hunk.old_lines} +{row.hunk.new_start},{row.hunk.new_lines} @@
                      </span>
                      {decision === "pending" && (
                        <div className="flex gap-2">
                          <button
                            onClick={() => setHunkDecision(row.sessionId!, row.hunk!.hunk_id, "accepted")}
                            disabled={isApplying}
                            className="inline-flex h-5 items-center gap-1 rounded border border-emerald-700/40 bg-emerald-950/50 px-1.5 text-[10px] font-semibold text-emerald-500 transition hover:bg-emerald-900/50 disabled:opacity-50"
                          >
                            <Check className="h-2.5 w-2.5" /> Accept
                          </button>
                          <button
                            onClick={() => setHunkDecision(row.sessionId!, row.hunk!.hunk_id, "rejected")}
                            disabled={isApplying}
                            className="inline-flex h-5 items-center gap-1 rounded border border-red-700/40 bg-red-950/50 px-1.5 text-[10px] font-semibold text-red-500 transition hover:bg-red-900/50 disabled:opacity-50"
                          >
                            <X className="h-2.5 w-2.5" /> Reject
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
                      {(row.type === "gap" || row.type === "context" || row.type === "deletion") ? (row.oldLine ?? "") : ""}
                    </span>
                    <span className="select-none pr-2 text-right text-[10px] text-zinc-500">
                      {(row.type === "gap" || row.type === "context" || row.type === "addition") ? (row.newLine ?? "") : ""}
                    </span>
                    <span className="select-none text-center">
                      {decision === "pending" && row.type === "deletion" ? "-" : decision === "pending" && row.type === "addition" ? "+" : " "}
                    </span>
                    <code className="whitespace-pre pl-1">
                      {row.content || " "}
                    </code>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* ── Floating file navigator — outside scroll container so it stays fixed at bottom ── */}
      {allMergedFiles.length > 0 && (
        <div className="absolute bottom-4 left-1/2 z-20 w-[min(520px,calc(100%-2rem))] -translate-x-1/2 rounded-lg border bg-card/95 p-1 shadow-2xl backdrop-blur">
          {isFileListOpen && (
            <div className="absolute bottom-full left-1/2 mb-1.5 max-h-56 w-[min(480px,calc(100vw-3rem))] -translate-x-1/2 overflow-auto rounded-lg border bg-card p-1.5 shadow-2xl">
              <div className="mb-1 px-2 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                {allMergedFiles.length} changed file{allMergedFiles.length === 1 ? "" : "s"}
              </div>
              <div className="space-y-0.5">
                {allMergedFiles.map((file, index) => (
                  <button
                    key={file.filePath}
                    onClick={() => { goToFile(index); setIsFileListOpen(false); }}
                    className={cn(
                      "flex h-7 w-full min-w-0 items-center justify-between gap-2 rounded px-2 text-left text-[11px] transition",
                      index === currentIndex ? "bg-primary text-primary-foreground" : "hover:bg-muted"
                    )}
                  >
                    <span className="min-w-0 truncate font-mono">{file.relativePath}</span>
                    <span className="shrink-0 text-[10px] opacity-70">
                      +{file.totalAdditions}/−{file.totalDeletions}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}
          <div className="flex items-center gap-1">
            <button
              onClick={() => goToFile(currentIndex - 1)}
              disabled={allMergedFiles.length <= 1}
              title="Previous file"
              className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded border bg-background text-xs transition hover:bg-muted disabled:opacity-40"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
            </button>
            <button
              onClick={() => setIsFileListOpen((o) => !o)}
              className="min-w-0 flex-1 rounded bg-muted px-2 py-1 text-center text-[11px] font-semibold transition hover:bg-muted/80"
            >
              <span>{allMergedFiles.length} file{allMergedFiles.length === 1 ? "" : "s"} changed</span>
              <span className="mx-1.5 text-muted-foreground">|</span>
              <span>{currentIndex + 1}/{allMergedFiles.length}</span>
            </button>
            <button
              onClick={() => currentFile && setFileDecision(currentFile, "accepted")}
              disabled={!currentFile || isApplying}
              className="inline-flex h-7 shrink-0 items-center gap-1 rounded border bg-background px-2 text-[11px] font-semibold text-emerald-500 transition hover:bg-muted disabled:opacity-40"
            >
              <Check className="h-3.5 w-3.5" />
              Accept
            </button>
            <button
              onClick={() => currentFile && setFileDecision(currentFile, "rejected")}
              disabled={!currentFile || isApplying}
              className="inline-flex h-7 shrink-0 items-center gap-1 rounded border bg-background px-2 text-[11px] font-semibold text-red-500 transition hover:bg-muted disabled:opacity-40"
            >
              <X className="h-3.5 w-3.5" />
              Reject
            </button>
            <button
              onClick={() => goToFile(currentIndex + 1)}
              disabled={allMergedFiles.length <= 1}
              title="Next file"
              className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded border bg-background text-xs transition hover:bg-muted disabled:opacity-40"
            >
              <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function StreamingProgressPanel({
  lines,
  packageName,
  reviewActivity,
  isStreaming,
  pendingPackage,
  currentPackage,
  isApplyingForPending,
  onApplyAndContinue,
  onDiscard,
  onCancel,
  queuedPackageCount,
  isBatchUpdating,
  onCancelBatch,
  checkpointId,
  isRollingBack,
  onRollback,
}: {
  lines: StreamLine[];
  packageName: string;
  reviewActivity?: DiffReviewActivityState | null;
  isStreaming: boolean;
  pendingPackage?: string;
  currentPackage?: string;
  isApplyingForPending?: boolean;
  onApplyAndContinue?: () => void;
  onDiscard?: () => void;
  onCancel?: () => void;
  queuedPackageCount?: number;
  isBatchUpdating?: boolean;
  onCancelBatch?: () => void;
  checkpointId?: string;
  isRollingBack?: boolean;
  onRollback?: () => void;
}) {
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length, pendingPackage]);

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="flex h-9 shrink-0 items-center gap-2 border-b px-3">
        {isStreaming
          ? <Loader2 className="h-3.5 w-3.5 animate-spin text-sky-400" />
          : pendingPackage
          ? <AlertTriangle className="h-3.5 w-3.5 text-amber-400" />
          : <span className="text-emerald-400">✓</span>
        }
        <span className="text-xs font-semibold text-zinc-200">
          {isStreaming ? "Preparing" : pendingPackage ? "Action required" : "Preview ready"}
          {packageName ? ` — ${packageName}` : ""}
        </span>
        {isBatchUpdating && !pendingPackage && (
          <button
            onClick={onCancelBatch}
            title="Cancel the remaining Update All queue"
            className="ml-auto inline-flex h-6 items-center gap-1 rounded border border-zinc-600 bg-zinc-800 px-2 text-[10px] font-semibold text-zinc-300 transition hover:border-red-500/60 hover:bg-red-950/40 hover:text-red-300"
          >
            <X className="h-3 w-3" />
            Stop queue
          </button>
        )}
        {checkpointId && !isStreaming && !pendingPackage && (
          <button
            onClick={onRollback}
            disabled={isRollingBack}
            title="Undo applied changes and restore code to before this update"
            className={cn(
              "inline-flex h-6 items-center gap-1 rounded border border-zinc-600 bg-zinc-800 px-2 text-[10px] font-semibold text-zinc-300 transition hover:border-red-500/60 hover:bg-red-950/40 hover:text-red-300 disabled:opacity-50",
              !isBatchUpdating && "ml-auto"
            )}
          >
            {isRollingBack
              ? <Loader2 className="h-3 w-3 animate-spin" />
              : <X className="h-3 w-3" />
            }
            Undo
          </button>
        )}
      </div>
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto p-3 pb-6 font-mono text-xs">
        <div className="space-y-1">
          {reviewActivity?.preview && (
            <div className="mb-2 border-l border-sky-500/40 bg-sky-950/10 px-3 py-2 font-sans text-[11px] leading-5 text-zinc-400">
              <div className="font-semibold text-sky-300">Review context</div>
              <div>
                {reviewActivity.preview.summary.total_files_changed} file(s), +{reviewActivity.preview.summary.total_additions}/-
                {reviewActivity.preview.summary.total_deletions}. Accept the safe hunks, reject anything suspicious, then apply to let the checker verify.
              </div>
            </div>
          )}
          {lines.length === 0 && (
            <p className="text-zinc-500">Starting pipeline…</p>
          )}
          {isBatchUpdating && (
            <div className="mb-2 border-l border-emerald-500/40 bg-emerald-950/10 px-3 py-2 font-sans text-[11px] leading-5 text-zinc-400">
              <div className="font-semibold text-emerald-300">Update All queue</div>
              <div>
                {queuedPackageCount ?? 0} package{(queuedPackageCount ?? 0) === 1 ? "" : "s"} waiting. DepGuard pauses after each preview so you can inspect and apply the diff.
              </div>
            </div>
          )}
          {lines.map((line, idx) => {
            if (line.status === "separator") {
              return <div key={line.id} className="my-2 border-t border-zinc-800" />;
            }
            if (line.status === "header") {
              return (
                <div key={line.id} className="mb-1 flex items-center gap-2 pt-1">
                  <span className="text-[10px] font-bold uppercase tracking-widest text-sky-400">{line.message}</span>
                </div>
              );
            }
            const isActiveLast = line.status === "running" && idx === lines.length - 1 && !pendingPackage;
            return (
              <div key={line.id} className={cn("flex items-start gap-2", line.status === "sub" && "pl-4")}>
                <span className="mt-px shrink-0 w-4 text-center">
                  {isActiveLast && <Loader2 className="h-3 w-3 animate-spin text-sky-400" />}
                  {!isActiveLast && line.status === "running" && <span className="text-zinc-500">●</span>}
                  {line.status === "success" && <span className="text-emerald-400">✓</span>}
                  {line.status === "error"   && <span className="text-red-400">✗</span>}
                  {line.status === "sub"     && <span className="text-zinc-500">↳</span>}
                  {line.status === "info"    && <span className="text-zinc-600">·</span>}
                </span>
                <span className="min-w-0 flex-1">
                  <span className={cn(
                    "block min-w-0 break-all leading-5",
                    isActiveLast ? "text-zinc-100" : "text-zinc-400",
                    line.status === "success" && "text-zinc-300",
                    line.status === "error"   && "text-red-400",
                    line.status === "sub"     && "text-zinc-400",
                  )}>
                    {line.badge && (
                      <span className="mr-2 inline-flex rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-sky-300">
                        {line.badge}
                      </span>
                    )}
                    {line.message}
                  </span>
                  {line.detail && (
                    <span className="mt-1 block max-w-full whitespace-normal break-words border-l border-zinc-800 pl-3 font-sans text-[11px] leading-5 text-zinc-500">
                      {line.detail}
                    </span>
                  )}
                </span>
              </div>
            );
          })}
        </div>

        {/* Inline confirmation — plain terminal text, no box/border */}
        {pendingPackage && (
          <div className="mt-1 space-y-0.5">
            <div className="flex items-start gap-2">
              <span className="mt-px shrink-0 w-4 text-center">
                <AlertTriangle className="h-3 w-3 text-amber-400" />
              </span>
              <span className="min-w-0 break-all leading-5 text-amber-200">
                Unsaved preview for <span className="font-semibold">{currentPackage}</span> — starting{" "}
                <span className="font-semibold">{pendingPackage}</span> will replace it.
              </span>
            </div>
            <div className="flex items-center gap-0 pl-6 leading-5 text-zinc-500">
              <span>[</span>
              <button
                onClick={onApplyAndContinue}
                disabled={isApplyingForPending}
                className="inline-flex items-center gap-1 px-0.5 text-zinc-400 transition hover:text-white disabled:opacity-40"
              >
                {isApplyingForPending ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
                Apply &amp; Continue
              </button>
              <span>]</span>
              <span className="mx-1">|</span>
              <span>[</span>
              <button
                onClick={onDiscard}
                disabled={isApplyingForPending}
                className="px-0.5 text-zinc-400 transition hover:text-white disabled:opacity-40"
              >
                Discard
              </button>
              <span>]</span>
              <span className="mx-1">|</span>
              <span>[</span>
              <button
                onClick={onCancel}
                disabled={isApplyingForPending}
                className="px-0.5 text-zinc-400 transition hover:text-white disabled:opacity-40"
              >
                Cancel
              </button>
              <span>]</span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function DependenciesPanel({
  packages,
  updatingPackage,
  updateAllCount,
  isBatchUpdating,
  onUpdateAll,
  onCancelUpdateAll,
  onUpdate,
}: {
  packages: PackageData[];
  updatingPackage: string;
  updateAllCount: number;
  isBatchUpdating: boolean;
  onUpdateAll: () => void;
  onCancelUpdateAll: () => void;
  onUpdate: (pkg: PackageData) => void;
}) {
  const [query, setQuery] = useState("");
  const [filterMode, setFilterMode] = useState<"all" | "outdated" | "vulnerable" | "unpinned">("all");

  const filteredPackages = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    const severityOrder: Record<string, number> = { CRITICAL: 5, HIGH: 4, UNPINNED: 3.5, MEDIUM: 3, LOW: 2, OK: 1 };

    return packages
      .filter((pkg) => {
        if (normalizedQuery) {
          const haystack = `${pkg.name} ${pkg.ecosystem} ${pkg.file_path} ${pkg.current_version} ${pkg.latest_version}`.toLowerCase();
          if (!haystack.includes(normalizedQuery)) return false;
        }

        if (filterMode === "vulnerable") return Boolean(pkg.cves?.length);
        if (filterMode === "unpinned") return pkg.severity === "UNPINNED";
        if (filterMode === "outdated") return isUpdateCandidate(pkg);
        return true;
      })
      .sort((left, right) => {
        const severityDiff = (severityOrder[right.severity] || 0) - (severityOrder[left.severity] || 0);
        if (severityDiff !== 0) return severityDiff;
        const ecosystemDiff = (left.ecosystem || "").localeCompare(right.ecosystem || "");
        if (ecosystemDiff !== 0) return ecosystemDiff;
        return left.name.localeCompare(right.name);
      });
  }, [filterMode, packages, query]);

  const groupedPackages = useMemo(() => {
    return filteredPackages.reduce((groups, pkg) => {
      const ecosystem = pkg.ecosystem || "Unknown";
      if (!groups[ecosystem]) groups[ecosystem] = [];
      groups[ecosystem].push(pkg);
      return groups;
    }, {} as Record<string, PackageData[]>);
  }, [filteredPackages]);

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="shrink-0 space-y-2 border-b p-3">
        <div className="relative">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search dependencies"
            className="h-8 w-full rounded-md border bg-background pl-8 pr-2 text-xs outline-none transition focus:border-primary"
          />
        </div>
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 flex-1 gap-1 overflow-x-auto pb-0.5">
            {(["all", "outdated", "vulnerable", "unpinned"] as const).map((mode) => (
              <button
                key={mode}
                onClick={() => setFilterMode(mode)}
                className={cn(
                  "shrink-0 rounded-md border px-2 py-1 text-[11px] font-semibold capitalize transition",
                  filterMode === mode ? "border-primary bg-primary text-primary-foreground" : "bg-background text-muted-foreground hover:bg-muted"
                )}
              >
                {mode}
              </button>
            ))}
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <button
              onClick={onUpdateAll}
              disabled={Boolean(updatingPackage) || isBatchUpdating || updateAllCount === 0}
              title="Queue every outdated package and review one preview at a time"
              className="inline-flex h-7 items-center gap-1.5 rounded-md border bg-background px-2 text-[11px] font-semibold text-muted-foreground transition hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-45"
            >
              {isBatchUpdating || updatingPackage ? <Loader2 className="h-3 w-3 animate-spin" /> : <ListTodo className="h-3 w-3" />}
              All
              <span className="rounded bg-muted px-1 py-0.5 text-[10px]">{updateAllCount}</span>
            </button>
            {isBatchUpdating && (
              <button
                onClick={onCancelUpdateAll}
                title="Cancel remaining queued updates"
                className="inline-flex h-7 items-center justify-center rounded-md border bg-background px-2 text-xs font-semibold text-muted-foreground transition hover:bg-muted hover:text-foreground"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        </div>
      </div>

      <div className="min-h-0 flex-1 space-y-3 overflow-auto p-3">
      {packages.length === 0 ? (
        <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
          Run a scan from the dashboard to populate dependency actions.
        </div>
      ) : filteredPackages.length === 0 ? (
        <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
          No dependencies match the current filter.
        </div>
      ) : (
        Object.entries(groupedPackages).map(([ecosystem, ecosystemPackages]) => (
          <div key={ecosystem} className="space-y-2">
            <div className="sticky top-0 z-10 flex items-center justify-between border-y bg-card/95 px-1 py-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground backdrop-blur">
              <span>{ecosystem}</span>
              <span>{ecosystemPackages.length}</span>
            </div>
            {ecosystemPackages.map((pkg) => {
              const canUpdatePackage = isUpdateCandidate(pkg) && pkg.severity !== "OK";
              const isUpdateDisabled = Boolean(updatingPackage) || isBatchUpdating || !canUpdatePackage;
              return (
                <div
                  key={`${pkg.ecosystem}-${pkg.name}-${pkg.file_path}`}
                  className={cn("rounded-lg border bg-background p-3", pkg.severity === "OK" && "opacity-70")}
                >
                  <div className="min-w-0">
                    <div className="truncate text-sm font-semibold" title={pkg.name}>{pkg.name}</div>
                    <div className="mt-1 truncate text-xs text-muted-foreground" title={`${pkg.current_version} -> ${pkg.latest_version}`}>
                      {pkg.current_version} → {pkg.latest_version}
                    </div>
                  </div>
                  <div className="mt-3 flex items-center justify-between gap-2">
                    <span className="rounded-md border px-2 py-1 text-xs text-muted-foreground">{pkg.severity}</span>
                    <button
                      onClick={() => canUpdatePackage && onUpdate(pkg)}
                      disabled={isUpdateDisabled}
                      title={canUpdatePackage ? `Preview update for ${pkg.name}` : `${pkg.name} is already OK`}
                      className={cn(
                        "inline-flex h-8 items-center rounded-md px-3 text-xs font-semibold transition disabled:cursor-not-allowed",
                        canUpdatePackage
                          ? "bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-60"
                          : "border bg-muted/40 text-muted-foreground opacity-50 blur-[0.3px]"
                      )}
                    >
                      {updatingPackage === pkg.name ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : canUpdatePackage ? (
                        "Update"
                      ) : (
                        "Update"
                      )}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        ))
      )}
      </div>
    </div>
  );
}

function CodeViewer({ content }: { content: string }) {
  const lines = content ? content.split("\n") : ["Select a file from the explorer."];

  return (
    <pre className="min-h-0 flex-1 overflow-auto p-4 font-mono text-xs leading-6 text-zinc-100">
      {lines.map((line, index) => (
        <div key={`${index}-${line}`} className="flex min-w-max">
          <span className="mr-4 w-10 select-none text-right text-zinc-500">{index + 1}</span>
          <code>{line || " "}</code>
        </div>
      ))}
    </pre>
  );
}

function ExplorerTreeNode({
  node,
  depth,
  selectedFile,
  expandedFolders,
  previewFilePaths,
  onToggleFolder,
  onSelectFile,
}: {
  node: ExplorerNode;
  depth: number;
  selectedFile: string;
  expandedFolders: Set<string>;
  previewFilePaths?: Set<string>;
  onToggleFolder: (path: string) => void;
  onSelectFile: (file: ProjectFile) => void;
}) {
  const isFolder = node.type === "folder";
  const isExpanded = expandedFolders.has(node.path);
  const isSelected = node.file?.path === selectedFile;
  const hasPreviewChange = Boolean(node.file && previewFilePaths?.has(node.file.path));

  if (isFolder) {
    return (
      <div>
        <button
          onClick={() => onToggleFolder(node.path)}
          className="flex h-8 w-full min-w-0 items-center gap-1.5 rounded-md pr-2 text-left text-sm text-zinc-300 transition hover:bg-muted hover:text-zinc-100"
          style={{ paddingLeft: depth * 14 + 6 }}
          title={node.path || node.name}
        >
          {isExpanded ? <ChevronDown className="h-3.5 w-3.5 shrink-0" /> : <ChevronRight className="h-3.5 w-3.5 shrink-0" />}
          {isExpanded ? <FolderOpen className="h-4 w-4 shrink-0 text-sky-400" /> : <Folder className="h-4 w-4 shrink-0 text-sky-400" />}
          <span className="truncate">{node.name}</span>
        </button>
        {isExpanded && (
          <div>
            {node.children.map((child) => (
              <ExplorerTreeNode
                key={child.path}
                node={child}
                depth={depth + 1}
                selectedFile={selectedFile}
                expandedFolders={expandedFolders}
                previewFilePaths={previewFilePaths}
                onToggleFolder={onToggleFolder}
                onSelectFile={onSelectFile}
              />
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <button
      onClick={() => node.file && onSelectFile(node.file)}
      className={cn(
        "flex h-8 w-full min-w-0 items-center gap-1.5 rounded-md pr-2 text-left text-sm transition",
        isSelected
          ? "bg-primary text-primary-foreground"
          : hasPreviewChange
            ? "text-amber-300 hover:bg-muted hover:text-amber-200"
            : "text-zinc-300 hover:bg-muted hover:text-zinc-100"
      )}
      style={{ paddingLeft: depth * 14 + 24 }}
      title={node.path}
    >
      <FileCode2 className="h-4 w-4 shrink-0" />
      <span className="min-w-0 flex-1 truncate">{node.name}</span>
      {hasPreviewChange && !isSelected && (
        <span className="ml-1 h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400" />
      )}
    </button>
  );
}

function DiffViewer({ before, after }: { before: string; after: string }) {
  const diff = useMemo(() => buildLineDiff(before, after), [after, before]);

  return (
    <pre className="min-h-0 flex-1 overflow-auto p-4 font-mono text-xs leading-6 text-zinc-100">
      {diff.map((line, index) => (
        <div
          key={`${index}-${line.type}-${line.text}`}
          className={cn(
            "flex min-w-max border-l-2 px-2",
            line.type === "added" && "border-emerald-400 bg-emerald-500/15 text-emerald-100",
            line.type === "removed" && "border-red-400 bg-red-500/15 text-red-100",
            line.type === "same" && "border-transparent"
          )}
        >
          <span className="mr-3 w-9 select-none text-right text-zinc-500">{line.oldLine ?? ""}</span>
          <span className="mr-3 w-9 select-none text-right text-zinc-500">{line.newLine ?? ""}</span>
          <span className="mr-3 select-none">{line.type === "added" ? "+" : line.type === "removed" ? "-" : " "}</span>
          <code>{line.text || " "}</code>
        </div>
      ))}
    </pre>
  );
}

function ChangedFilesPopup({
  files,
  activeFile,
  onOpenFile,
  onClose,
}: {
  files: ChangedFile[];
  activeFile: string;
  onOpenFile: (file: ChangedFile) => void;
  onClose: () => void;
}) {
  return (
    <div className="absolute bottom-5 left-1/2 z-20 w-[min(760px,calc(100%-2rem))] -translate-x-1/2 rounded-xl border bg-card/95 p-3 shadow-2xl backdrop-blur">
      <div className="mb-2 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <CheckCircle2 className="h-4 w-4 text-emerald-500" />
          {files.length} file{files.length === 1 ? "" : "s"} changed
        </div>
        <button onClick={onClose} className="rounded-md p-1 text-muted-foreground transition hover:bg-muted hover:text-foreground">
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="flex gap-2 overflow-x-auto pb-1">
        {files.map((file) => (
          <button
            key={file.file}
            onClick={() => onOpenFile(file)}
            className={cn(
              "shrink-0 rounded-lg border px-3 py-2 text-left text-xs transition",
              activeFile === file.file ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-100" : "bg-background hover:bg-muted"
            )}
          >
            <div className="max-w-[220px] truncate font-semibold">{file.file}</div>
            <div className="mt-0.5 text-muted-foreground">{countDiffStats(file.before, file.after)}</div>
          </button>
        ))}
      </div>
    </div>
  );
}

function buildLineDiff(before: string, after: string): DiffLine[] {
  const beforeLines = before.split("\n");
  const afterLines = after.split("\n");
  const matrix = Array.from({ length: beforeLines.length + 1 }, () => Array(afterLines.length + 1).fill(0));

  for (let i = beforeLines.length - 1; i >= 0; i -= 1) {
    for (let j = afterLines.length - 1; j >= 0; j -= 1) {
      matrix[i][j] = beforeLines[i] === afterLines[j]
        ? matrix[i + 1][j + 1] + 1
        : Math.max(matrix[i + 1][j], matrix[i][j + 1]);
    }
  }

  const diff: DiffLine[] = [];
  let i = 0;
  let j = 0;
  let oldLine = 1;
  let newLine = 1;

  while (i < beforeLines.length && j < afterLines.length) {
    if (beforeLines[i] === afterLines[j]) {
      diff.push({ type: "same", text: beforeLines[i], oldLine, newLine });
      i += 1;
      j += 1;
      oldLine += 1;
      newLine += 1;
    } else if (matrix[i + 1][j] >= matrix[i][j + 1]) {
      diff.push({ type: "removed", text: beforeLines[i], oldLine });
      i += 1;
      oldLine += 1;
    } else {
      diff.push({ type: "added", text: afterLines[j], newLine });
      j += 1;
      newLine += 1;
    }
  }

  while (i < beforeLines.length) {
    diff.push({ type: "removed", text: beforeLines[i], oldLine });
    i += 1;
    oldLine += 1;
  }
  while (j < afterLines.length) {
    diff.push({ type: "added", text: afterLines[j], newLine });
    j += 1;
    newLine += 1;
  }

  return diff;
}

function countDiffStats(before: string, after: string) {
  const diff = buildLineDiff(before, after);
  const added = diff.filter((line) => line.type === "added").length;
  const removed = diff.filter((line) => line.type === "removed").length;
  return `+${added} / -${removed}`;
}

function buildExplorerTree(files: ProjectFile[]): ExplorerNode {
  const root: ExplorerNode = { name: "root", path: "", type: "folder", children: [] };

  for (const file of files) {
    const parts = file.path.split("/").filter(Boolean);
    let current = root;
    let currentPath = "";

    parts.forEach((part, index) => {
      currentPath = currentPath ? `${currentPath}/${part}` : part;
      const isFile = index === parts.length - 1;
      let child = current.children.find((item) => item.name === part && item.type === (isFile ? "file" : "folder"));

      if (!child) {
        child = {
          name: part,
          path: currentPath,
          type: isFile ? "file" : "folder",
          children: [],
          file: isFile ? file : undefined,
        };
        current.children.push(child);
      }

      current = child;
    });
  }

  sortExplorerNode(root);
  return root;
}

function sortExplorerNode(node: ExplorerNode) {
  node.children.sort((left, right) => {
    if (left.type !== right.type) {
      return left.type === "folder" ? -1 : 1;
    }
    return left.name.localeCompare(right.name);
  });
  node.children.forEach(sortExplorerNode);
}

function getFolderPaths(files: ProjectFile[]) {
  const paths = new Set<string>([""]);
  files.forEach((file) => {
    const parts = file.path.split("/").filter(Boolean);
    let current = "";
    parts.slice(0, -1).forEach((part) => {
      current = current ? `${current}/${part}` : part;
      paths.add(current);
    });
  });
  return paths;
}
