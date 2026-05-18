import { Code2, FolderSearch } from "lucide-react";
import { HealthScore } from "@/components/HealthScore";
import { PackagesTable, type PackageData } from "@/components/PackagesTable";
import { ProjectDependencyGraph } from "@/components/ProjectDependencyGraph";
import { UpdateLog, type LogEntry } from "@/components/UpdateLog";

export interface HealthData {
  score: number;
  stats: { critical: number; high: number; medium: number; low: number; unpinned: number; ok: number };
}

interface DashboardViewProps {
  folderPath: string;
  setFolderPath: (path: string) => void;
  isScanning: boolean;
  healthData: HealthData | null;
  packages: PackageData[];
  logs: LogEntry[];
  onBrowse: () => void;
  onScan: () => void;
  onLog: (message: string, type: "info" | "success" | "error") => void;
  onOpenIde: () => void;
}

export function DashboardView({
  folderPath,
  setFolderPath,
  isScanning,
  healthData,
  packages,
  logs,
  onBrowse,
  onScan,
  onLog,
  onOpenIde,
}: DashboardViewProps) {
  return (
    <main className="container mx-auto flex max-w-7xl flex-col gap-8 px-6 py-8">
      <section className="relative overflow-hidden rounded-2xl border border-border/60 bg-card p-6 shadow-sm">
        <div className="relative z-10 flex flex-col items-end gap-4 md:flex-row">
          <div className="w-full flex-1 space-y-2">
            <label className="ml-1 text-sm font-semibold uppercase tracking-wider text-muted-foreground">Target Project Directory</label>
            <div className="flex gap-2">
              <div className="relative flex-1">
                <FolderSearch className="absolute left-3 top-1/2 h-5 w-5 -translate-y-1/2 text-muted-foreground" />
                <input
                  type="text"
                  value={folderPath}
                  onChange={(event) => setFolderPath(event.target.value)}
                  placeholder="e.g. /path/to/your/project"
                  className="w-full rounded-lg border border-border/60 bg-background py-3 pl-10 pr-4 text-sm transition-all focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary"
                  onKeyDown={(event) => event.key === "Enter" && onScan()}
                  disabled={isScanning}
                />
              </div>
              <button
                onClick={onBrowse}
                disabled={isScanning}
                className="flex h-[46px] shrink-0 items-center justify-center rounded-lg border border-border/60 bg-secondary px-4 py-3 font-semibold text-secondary-foreground transition-all hover:bg-secondary/80 disabled:opacity-50"
              >
                Browse
              </button>
            </div>
          </div>
          <button
            onClick={onScan}
            disabled={isScanning || !folderPath}
            className="flex h-[46px] shrink-0 items-center justify-center rounded-lg bg-primary px-8 py-3 font-semibold text-primary-foreground shadow-sm transition-all hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {isScanning ? (
              <span className="flex items-center gap-2">
                <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                Scanning...
              </span>
            ) : (
              "Scan Project"
            )}
          </button>
        </div>

      </section>

      <div className="grid gap-4 lg:grid-cols-[1fr_320px]">
        <ProjectDependencyGraph folderPath={folderPath} />
        <section className="rounded-xl border bg-card p-4">
          <div className="flex h-full flex-col justify-between gap-4">
            <div className="flex items-center gap-3">
              <div className="rounded-lg border bg-background p-2">
                <Code2 className="h-5 w-5" />
              </div>
              <div>
                <h3 className="text-sm font-semibold uppercase tracking-wider text-muted-foreground">IDE Workspace</h3>
                <p className="text-sm text-foreground">Review files and dependencies in a focused coding layout.</p>
              </div>
            </div>
            <button
              onClick={onOpenIde}
              className="inline-flex h-10 items-center justify-center gap-2 rounded-lg bg-primary px-4 text-sm font-semibold text-primary-foreground transition hover:bg-primary/90"
            >
              <Code2 className="h-4 w-4" />
              Open IDE Workspace
            </button>
          </div>
        </section>
      </div>

      <div className="flex flex-col items-start gap-8 lg:flex-row">
        <div className="flex w-full shrink-0 flex-col gap-8 lg:w-1/3">
          {healthData && (
            <section className="duration-500 animate-in fade-in slide-in-from-bottom-4">
              <HealthScore score={healthData.score} stats={healthData.stats} />
            </section>
          )}

          <section className="sticky top-24 w-full">
            <h3 className="mb-3 ml-1 text-sm font-semibold uppercase tracking-wider text-muted-foreground">Activity Log</h3>
            <UpdateLog logs={logs} />
          </section>
        </div>

        <div className="flex w-full flex-col gap-8 lg:w-2/3">
          {packages.length > 0 && (
            <section className="duration-700 animate-in fade-in slide-in-from-bottom-6">
              <h3 className="mb-3 ml-1 text-sm font-semibold uppercase tracking-wider text-muted-foreground">Dependencies ({packages.length})</h3>
              <PackagesTable folderPath={folderPath} packages={packages} onLog={onLog} />
            </section>
          )}
        </div>
      </div>
    </main>
  );
}
