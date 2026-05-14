import { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  ChevronDown,
  ChevronRight,
  CheckCircle2,
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
  updatePackage,
  type ChangedFile,
  type ProjectFile,
} from "@/hooks/useDepGuard";
import { cn } from "@/lib/utils";

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

  const visiblePackages = packages.slice(0, 12);

  const loadFiles = async () => {
    if (!folderPath) return;

    setIsLoadingFiles(true);
    try {
      const data = await getProjectFiles(folderPath);
      setFiles(data.files);
      if (!selectedFile && data.files.length > 0) {
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
    void loadFiles();
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
    onLog(`IDE workspace update started for ${pkg.name}.`, "info");
    try {
      const result = await updatePackage(folderPath, pkg);
      const filesChanged = result.changed_files ?? [];
      setChangedFiles(filesChanged);
      if (filesChanged.length > 0) {
        setActiveChangedFile(filesChanged[0].file);
        setSelectedFile(filesChanged[0].file);
      }

      if (result.status === "success" || result.status === "updated_version_only") {
        onLog(`Successfully updated ${pkg.name}.`, "success");
      } else {
        onLog(`Update failed for ${pkg.name}.`, "error");
      }
      void loadFiles();
    } catch (error) {
      onLog(`Error updating ${pkg.name}: ${error instanceof Error ? error.message : "Unknown error"}`, "error");
    } finally {
      setUpdatingPackage("");
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
    <main className="flex min-h-[calc(100vh-73px)] flex-col bg-background">
      <div className="flex items-center justify-between border-b bg-card px-5 py-3">
        <button
          onClick={onBack}
          className="inline-flex h-10 items-center gap-2 rounded-lg border bg-background px-3 text-sm font-semibold transition hover:bg-muted"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to Dashboard
        </button>
        <div className="hidden min-w-0 text-right md:block">
          <p className="truncate text-sm font-medium">{folderPath}</p>
          <p className="text-xs text-muted-foreground">IDE Workspace</p>
        </div>
      </div>

      <div className="grid min-h-0 flex-1 lg:grid-cols-[20%_55%_25%]">
        <aside className="min-h-[260px] min-w-0 border-b bg-card/70 lg:border-b-0 lg:border-r">
          <div className="flex items-center gap-2 border-b px-4 py-3">
            <FolderTree className="h-4 w-4" />
            <h2 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Explorer</h2>
            <button
              onClick={() => void loadFiles()}
              className="ml-auto rounded-md p-1 text-muted-foreground transition hover:bg-muted hover:text-foreground"
              title="Refresh files"
            >
              {isLoadingFiles ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            </button>
          </div>
          <div className="border-b p-3">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <input
                value={fileQuery}
                onChange={(event) => setFileQuery(event.target.value)}
                placeholder="Search files"
                className="h-9 w-full rounded-md border bg-background pl-8 pr-2 text-sm outline-none transition focus:border-primary"
              />
            </div>
          </div>
          <div className="max-h-[calc(100vh-220px)] overflow-y-auto px-2 py-3">
            {explorerTree.children.length === 0 ? (
              <div className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">No files found.</div>
            ) : (
              explorerTree.children.map((node) => (
                <ExplorerTreeNode
                  key={node.path}
                  node={node}
                  depth={0}
                  selectedFile={selectedFile}
                  expandedFolders={expandedFolders}
                  onToggleFolder={toggleFolder}
                  onSelectFile={(file) => {
                    setActiveChangedFile("");
                    setSelectedFile(file.path);
                  }}
                />
              ))
            )}
          </div>
        </aside>

        <section className="relative flex min-h-[480px] min-w-0 flex-col border-b bg-[#101010] lg:border-b-0 lg:border-r">
          <div className="flex items-center justify-between border-b bg-card px-4 py-3">
            <div className="flex min-w-0 items-center gap-2">
              <GitCompareArrows className="h-4 w-4 shrink-0" />
              <div className="min-w-0">
                <h2 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  {currentChangedFile ? "Diff Viewer" : "Editor"}
                </h2>
                <p className="truncate text-xs text-muted-foreground">{selectedFile || "Select a file"}</p>
              </div>
            </div>
            <button
              onClick={() => selectedFile && loadContent(selectedFile)}
              disabled={!selectedFile || isLoadingContent}
              className="inline-flex h-8 items-center gap-2 rounded-md border bg-background px-2 text-xs font-semibold transition hover:bg-muted disabled:opacity-50"
            >
              {isLoadingContent ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
              Reload
            </button>
          </div>

          {isLoadingContent ? (
            <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
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

        <aside className="min-h-[320px] overflow-y-auto bg-card/70">
          <div className="flex items-center gap-2 border-b px-4 py-3">
            <PackageOpen className="h-4 w-4" />
            <h2 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Dependencies</h2>
          </div>
          <div className="space-y-3 p-3">
            {visiblePackages.length === 0 ? (
              <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
                Run a scan from the dashboard to populate dependency actions.
              </div>
            ) : (
              visiblePackages.map((pkg) => (
                <div key={`${pkg.ecosystem}-${pkg.name}`} className="rounded-lg border bg-background p-3">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-semibold">{pkg.name}</div>
                    <div className="mt-1 text-xs text-muted-foreground">
                      {pkg.current_version} → {pkg.latest_version}
                    </div>
                  </div>
                  <div className="mt-3 flex items-center justify-between gap-2">
                    <span className="rounded-md border px-2 py-1 text-xs text-muted-foreground">{pkg.severity}</span>
                    <button
                      onClick={() => void handleUpdate(pkg)}
                      disabled={Boolean(updatingPackage)}
                      className="inline-flex h-8 items-center rounded-md bg-primary px-3 text-xs font-semibold text-primary-foreground transition hover:bg-primary/90 disabled:opacity-60"
                    >
                      {updatingPackage === pkg.name ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "Update"}
                    </button>
                  </div>
                </div>
              ))
            )}
          </div>
        </aside>
      </div>
    </main>
  );
}

function CodeViewer({ content }: { content: string }) {
  const lines = content ? content.split("\n") : ["Select a file from the explorer."];

  return (
    <pre className="flex-1 overflow-auto p-4 font-mono text-xs leading-6 text-zinc-300">
      {lines.map((line, index) => (
        <div key={`${index}-${line}`} className="flex min-w-max">
          <span className="mr-4 w-10 select-none text-right text-zinc-600">{index + 1}</span>
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
  onToggleFolder,
  onSelectFile,
}: {
  node: ExplorerNode;
  depth: number;
  selectedFile: string;
  expandedFolders: Set<string>;
  onToggleFolder: (path: string) => void;
  onSelectFile: (file: ProjectFile) => void;
}) {
  const isFolder = node.type === "folder";
  const isExpanded = expandedFolders.has(node.path);
  const isSelected = node.file?.path === selectedFile;

  if (isFolder) {
    return (
      <div>
        <button
          onClick={() => onToggleFolder(node.path)}
          className="flex h-8 w-full min-w-0 items-center gap-1.5 rounded-md pr-2 text-left text-sm text-muted-foreground transition hover:bg-muted hover:text-foreground"
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
        isSelected ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground"
      )}
      style={{ paddingLeft: depth * 14 + 24 }}
      title={node.path}
    >
      <FileCode2 className="h-4 w-4 shrink-0" />
      <span className="truncate">{node.name}</span>
    </button>
  );
}

function DiffViewer({ before, after }: { before: string; after: string }) {
  const diff = useMemo(() => buildLineDiff(before, after), [after, before]);

  return (
    <pre className="flex-1 overflow-auto p-4 font-mono text-xs leading-6 text-zinc-300">
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
          <span className="mr-3 w-9 select-none text-right text-zinc-600">{line.oldLine ?? ""}</span>
          <span className="mr-3 w-9 select-none text-right text-zinc-600">{line.newLine ?? ""}</span>
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
