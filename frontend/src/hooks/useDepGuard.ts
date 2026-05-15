export async function scanProject(folderPath: string) {
  const response = await fetch('/api/scan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder_path: folderPath })
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to scan project');
  }
  return response.json();
}

export function scanProjectStream(folderPath: string, onMessage: (msg: any) => void, onError: (err: string) => void) {
  const url = `/api/scan-stream?folder_path=${encodeURIComponent(folderPath)}`;
  const eventSource = new EventSource(url);

  eventSource.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      if (data.error) {
        onError(data.error);
        eventSource.close();
      } else if (data.phase === "Completed") {
        onMessage(data);
        eventSource.close();
      } else {
        onMessage(data);
      }
    } catch (err) {
      onError('Failed to parse stream event');
      eventSource.close();
    }
  };

  eventSource.onerror = () => {
    onError('Connection to scan stream lost.');
    eventSource.close();
  };

  return () => {
    eventSource.close();
  };
}

export interface ChangedFile {
  file: string;
  before: string;
  after: string;
  status: string;
}

export interface UpdatePackageResponse {
  package: string;
  status: string;
  files_patched?: { file: string; lines_changed?: number[]; status: string; error?: string }[];
  changed_files?: ChangedFile[];
  checkpoint_id?: string;
  llm_provider?: string;
  fallback_used?: boolean;
  latency_ms?: number;
}

export async function updatePackage(folderPath: string, packageInfo: object): Promise<UpdatePackageResponse> {
  const response = await fetch('/api/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder_path: folderPath, package_info: packageInfo })
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to update package');
  }
  return response.json();
}

export type DiffChangeType = "context" | "deletion" | "addition";

export interface PreviewChange {
  type: DiffChangeType;
  line_number_old: number | null;
  line_number_new: number | null;
  content: string;
}

export interface PreviewHunk {
  hunk_id: string;
  old_start: number;
  old_lines: number;
  new_start: number;
  new_lines: number;
  changes: PreviewChange[];
}

export interface PreviewFile {
  file_path: string;
  relative_path: string;
  status: string;
  additions: number;
  deletions: number;
  hunks: PreviewHunk[];
}

export interface PreviewResponse {
  session_id: string;
  package: string;
  from_version: string;
  to_version: string;
  summary: {
    total_files_changed: number;
    total_additions: number;
    total_deletions: number;
  };
  files: PreviewFile[];
}

export interface VerificationCommandResult {
  name: string;
  command: string;
  status: "passed" | "failed" | "skipped";
  stdout?: string;
  stderr?: string;
  exit_code?: number | null;
}

export interface VerificationReport {
  status: "passed" | "failed" | "skipped";
  message: string;
  commands: VerificationCommandResult[];
}

export interface RepairAttempt {
  attempt: number;
  status: "success" | "failed" | "skipped";
  files_repaired?: string[];
  error?: string | null;
  final_verification?: VerificationReport | null;
}

export interface RepairReport {
  status: "success" | "failed" | "skipped";
  attempts: RepairAttempt[];
  final_verification?: VerificationReport | null;
}

export interface ApplyResponse {
  status: string;
  files_accepted: string[];
  files_rejected: string[];
  dependency_file_updated: string;
  verification?: VerificationReport | null;
  repair?: RepairReport | null;
}

export async function previewUpdate(folderPath: string, packageInfo: object): Promise<PreviewResponse> {
  const response = await fetch('/api/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder_path: folderPath, package_info: packageInfo })
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to preview update');
  }
  return response.json();
}

export async function applyPreview(sessionId: string, decisions: object): Promise<ApplyResponse> {
  const response = await fetch('/api/apply', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, decisions })
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to apply preview');
  }
  return response.json();
}

export async function discardPreview(sessionId: string): Promise<void> {
  const response = await fetch(`/api/preview/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE'
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to discard preview');
  }
}

export async function rollbackPackage(checkpointId: string, folderPath: string) {
  const response = await fetch('/api/rollback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ checkpoint_id: checkpointId, folder_path: folderPath })
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to rollback package');
  }
  return response.json();
}

export async function getProviders() {
  const response = await fetch('/api/providers');
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to fetch providers');
  }
  return response.json();
}

export async function browseProject() {
  const response = await fetch('/api/browse');
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to open folder browser');
  }
  return response.json();
}

export interface ProjectFile {
  path: string;
  name: string;
  extension: string;
  size: number;
}

export async function getProjectFiles(folderPath: string): Promise<{ files: ProjectFile[] }> {
  const response = await fetch(`/api/files?folder_path=${encodeURIComponent(folderPath)}`);
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to list project files');
  }
  return response.json();
}

export async function getFileContent(folderPath: string, filePath: string): Promise<{ path: string; content: string; size: number }> {
  const response = await fetch('/api/file-content', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder_path: folderPath, file_path: filePath })
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to read file');
  }
  return response.json();
}

export interface ProjectGraphNode {
  id: string;
  label: string;
  file: string;
  type: "function" | "method" | "class" | "module_level" | "decorator";
  parent: string | null;
  startLine: number;
  endLine: number;
  source: string;
  calls: string[];
  referencesSymbols: string[];
  definesSymbols: string[];
  callReturnUsage: Record<string, string[]>;
}

export interface ProjectGraphEdge {
  id: string;
  source: string;
  target: string;
  type: string;
}

export interface ProjectGraphResponse {
  nodes: ProjectGraphNode[];
  edges: ProjectGraphEdge[];
  stats: {
    nodes: number;
    edges: number;
    files: number;
  };
}

export async function getImpactGraph(folderPath: string, forceRebuild = false): Promise<ProjectGraphResponse> {
  const response = await fetch('/api/impact-graph', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder_path: folderPath, force_rebuild: forceRebuild })
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to fetch impact graph');
  }
  return response.json();
}
