import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  ChevronDown,
  ChevronRight,
  FileCode2,
  Folder,
  FolderOpen,
  FolderTree,
  GitCompareArrows,
  Loader2,
  PackageOpen,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import { type PackageData } from "@/components/PackagesTable";
import {
  getFileContent,
  getProjectFiles,
  previewUpdateStream,
  type ApplyResponse,
  type ChangedFile,
  type PreviewResponse,
  type PreviewStreamEvent,
  type ProjectFile,
} from "@/hooks/useDepGuard";
import { cn } from "@/lib/utils";
import {
  DiffReviewActivityPanel,
  DiffReviewPanel,
  type DiffReviewActivityState,
} from "@/components/DiffReviewPanel";

interface IdeWorkspaceViewProps {
  folderPath: string;
  packages: PackageData[];
  onBack: () => void;
  onLog: (message: string, type: "info" | "success" | "error") => void;
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
  status: "running" | "success" | "error" | "info" | "sub";
};

type ExplorerNode = {
  name: string;
  path: string;
  type: "folder" | "file";
  children: ExplorerNode[];
  file?: ProjectFile;
};

export function IdeWorkspaceView({ folderPath, packages, onBack, onLog }: IdeWorkspaceViewProps) {
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
  const [rightPanelMode, setRightPanelMode] = useState<"dependencies" | "progress">("dependencies");

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

  useEffect(() => {
    if (fileQuery.trim()) {
      setExpandedFolders(getFolderPaths(filteredFiles));
    }
  }, [fileQuery, filteredFiles]);

  const currentChangedFile = useMemo(
    () => changedFiles.find((file) => file.file === activeChangedFile) ?? null,
    [activeChangedFile, changedFiles]
  );

  const handleUpdate = async (pkg: PackageData) => {
    setUpdatingPackage(pkg.name);
    setRightPanelMode("progress");
    setReviewActivity(null);
    setStreamLines([]);
    onLog(`Preparing IDE preview for ${pkg.name}.`, "info");

    const appendLine = (line: Omit<StreamLine, "id">) =>
      setStreamLines((prev) => [...prev, { ...line, id: prev.length }]);

    const onEvent = (event: PreviewStreamEvent) => {
      if (event.event === "patch_file_start") return;
      const msg = event.message ?? "";
      if (event.event === "phase") {
        appendLine({ message: msg, status: "running" });
      } else if (event.event === "ast_done" || event.event === "scout_done" || event.event === "done") {
        appendLine({ message: msg, status: "success" });
      } else if (event.event === "patch_file_done") {
        appendLine({ message: msg, status: event.success ? "sub" : "error" });
      } else if (event.event === "error") {
        appendLine({ message: msg, status: "error" });
      } else {
        appendLine({ message: msg, status: "info" });
      }
    };

    try {
      const result = await previewUpdateStream(folderPath, pkg, onEvent);
      if (result.files.length === 0) {
        onLog(`No file changes needed for ${pkg.name}.`, "success");
      } else {
        setPreviewData(result);
        setPreviewPackageName(pkg.name);
        setPreviewActiveFile(result.files[0]?.file_path ?? "");
        setSelectedFile(result.files[0]?.file_path ?? "");
        setRightPanelMode("progress");
        onLog(`Preview ready for ${pkg.name}: ${result.summary.total_files_changed} file(s) changed.`, "info");
      }
      void loadFiles();
    } catch (error) {
      onLog(`Error previewing ${pkg.name}: ${error instanceof Error ? error.message : "Unknown error"}`, "error");
    } finally {
      setUpdatingPackage("");
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
    setPreviewData(null);
    setPreviewPackageName("");
    setPreviewActiveFile("");
    setReviewActivity(null);
    setRightPanelMode("dependencies");
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

      <div className="grid min-h-0 flex-1 overflow-hidden lg:grid-cols-[20%_55%_25%]">
        <aside className="flex min-h-0 min-w-0 flex-col overflow-hidden border-b bg-card/70 lg:border-b-0 lg:border-r">
          <div className="flex h-10 shrink-0 items-center gap-2 border-b px-3">
            <FolderTree className="h-4 w-4" />
            <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-100">Explorer</h2>
            <button
              onClick={() => void loadFiles()}
              className="ml-auto rounded-md p-1 text-muted-foreground transition hover:bg-muted hover:text-foreground"
              title="Refresh files"
            >
              {isLoadingFiles ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
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
                  previewFilePaths={previewFilePaths}
                  onToggleFolder={toggleFolder}
                  onSelectFile={(file) => {
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
        </aside>

        <section className="relative flex min-h-0 min-w-0 flex-col overflow-hidden border-b bg-[#101010] lg:border-b-0 lg:border-r">
          {!previewData && (
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
              <button
                onClick={() => selectedFile && loadContent(selectedFile)}
                disabled={!selectedFile || isLoadingContent}
                className="inline-flex h-7 items-center gap-2 rounded-md border bg-background px-2 text-xs font-semibold transition hover:bg-muted disabled:opacity-50"
              >
                {isLoadingContent ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                Reload
              </button>
            </div>
          )}

          {previewData ? (
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
            updatingPackage && !previewData ? (
              <StreamingProgressPanel lines={streamLines} packageName={updatingPackage} />
            ) : (
              <DiffReviewActivityPanel activity={reviewActivity} />
            )
          ) : (
            <DependenciesPanel
              packages={packages}
              updatingPackage={updatingPackage}
              onUpdate={(pkg) => void handleUpdate(pkg)}
            />
          )}
        </aside>
      </div>
    </main>
  );
}

function StreamingProgressPanel({ lines, packageName }: { lines: StreamLine[]; packageName: string }) {
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length]);

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="flex h-9 shrink-0 items-center gap-2 border-b px-3">
        <Loader2 className="h-3.5 w-3.5 animate-spin text-sky-400" />
        <span className="text-xs font-semibold text-zinc-200">Preparing — {packageName}</span>
      </div>
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto p-3 font-mono text-xs">
        <div className="space-y-1">
          {lines.length === 0 && (
            <p className="text-zinc-500">Starting pipeline…</p>
          )}
          {lines.map((line, idx) => {
            const isActiveLast = line.status === "running" && idx === lines.length - 1;
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
                <span className={cn(
                  "min-w-0 break-all leading-5",
                  isActiveLast ? "text-zinc-100" : "text-zinc-400",
                  line.status === "success" && "text-zinc-300",
                  line.status === "error"   && "text-red-400",
                  line.status === "sub"     && "text-zinc-400",
                )}>
                  {line.message}
                </span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function DependenciesPanel({
  packages,
  updatingPackage,
  onUpdate,
}: {
  packages: PackageData[];
  updatingPackage: string;
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
        if (filterMode === "outdated") {
          const current = (pkg.current_version || "").replace(/^[\^~=<>v\s]+/i, "");
          const latest = (pkg.latest_version || "").replace(/^[\^~=<>v\s]+/i, "");
          return Boolean(current && latest && current !== latest);
        }
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
        <div className="flex gap-1 overflow-x-auto pb-0.5">
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
            {ecosystemPackages.map((pkg) => (
              <div key={`${pkg.ecosystem}-${pkg.name}-${pkg.file_path}`} className="rounded-lg border bg-background p-3">
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold" title={pkg.name}>{pkg.name}</div>
                  <div className="mt-1 truncate text-xs text-muted-foreground" title={`${pkg.current_version} -> ${pkg.latest_version}`}>
                    {pkg.current_version} → {pkg.latest_version}
                  </div>
                </div>
                <div className="mt-3 flex items-center justify-between gap-2">
                  <span className="rounded-md border px-2 py-1 text-xs text-muted-foreground">{pkg.severity}</span>
                  <button
                    onClick={() => onUpdate(pkg)}
                    disabled={Boolean(updatingPackage)}
                    className="inline-flex h-8 items-center rounded-md bg-primary px-3 text-xs font-semibold text-primary-foreground transition hover:bg-primary/90 disabled:opacity-60"
                  >
                    {updatingPackage === pkg.name ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "Update"}
                  </button>
                </div>
              </div>
            ))}
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
