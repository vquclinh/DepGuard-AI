import { useState } from "react";
import { cn } from "@/lib/utils";
import { updatePackage, rollbackPackage } from "@/hooks/useDepGuard";
import { AlertCircle, CheckCircle2, RotateCcw } from "lucide-react";

export interface PackageData {
  name: string;
  current_version: string;
  latest_version: string;
  ecosystem: string;
  severity: string;
  file_path: string;
}

interface PackagesTableProps {
  folderPath: string;
  packages: PackageData[];
  onLog: (msg: string, type: "info" | "success" | "error") => void;
}

const severityColors: Record<string, string> = {
  CRITICAL: "bg-red-500/10 text-red-500 border-red-500/20",
  HIGH: "bg-orange-500/10 text-orange-500 border-orange-500/20",
  MEDIUM: "bg-yellow-500/10 text-yellow-500 border-yellow-500/20",
  LOW: "bg-blue-500/10 text-blue-500 border-blue-500/20",
  OK: "bg-green-500/10 text-green-500 border-green-500/20"
};

export function PackagesTable({ folderPath, packages, onLog }: PackagesTableProps) {
  const [updating, setUpdating] = useState<Record<string, boolean>>({});
  const [statuses, setStatuses] = useState<Record<string, { type: "success" | "error", error?: string, checkpoint?: string, provider?: string }>>({});

  const handleUpdate = async (pkg: PackageData) => {
    setUpdating(prev => ({ ...prev, [pkg.name]: true }));
    onLog(`Starting update for ${pkg.name} (${pkg.current_version} -> ${pkg.latest_version})...`, "info");
    
    try {
      const result = await updatePackage(folderPath, pkg);
      if (result.status === "success" || result.status === "updated_version_only") {
        setStatuses(prev => ({ ...prev, [pkg.name]: { type: "success", provider: result.llm_provider } }));
        onLog(`Successfully updated ${pkg.name}.`, "success");
      } else {
        setStatuses(prev => ({ ...prev, [pkg.name]: { type: "error", error: "Failed", checkpoint: result.checkpoint_id } }));
        onLog(`Update failed for ${pkg.name}.`, "error");
      }
    } catch (e: any) {
      setStatuses(prev => ({ ...prev, [pkg.name]: { type: "error", error: e.message } }));
      onLog(`Error updating ${pkg.name}: ${e.message}`, "error");
    } finally {
      setUpdating(prev => ({ ...prev, [pkg.name]: false }));
    }
  };

  const handleRollback = async (pkgName: string, checkpointId?: string) => {
    if (!checkpointId) {
      onLog(`No checkpoint ID available to rollback ${pkgName}.`, "error");
      return;
    }
    
    onLog(`Rolling back ${pkgName} using checkpoint ${checkpointId}...`, "info");
    try {
      await rollbackPackage(checkpointId, folderPath);
      onLog(`Rollback successful for ${pkgName}.`, "success");
      setStatuses(prev => {
        const next = { ...prev };
        delete next[pkgName]; // Reset status
        return next;
      });
    } catch (e: any) {
      onLog(`Rollback failed for ${pkgName}: ${e.message}`, "error");
    }
  };

  return (
    <div className="w-full overflow-hidden border rounded-xl bg-card">
      <div className="overflow-x-auto">
        <table className="w-full text-sm text-left">
          <thead className="text-xs uppercase bg-muted/50 border-b">
            <tr>
              <th className="px-6 py-4 font-medium">Package</th>
              <th className="px-6 py-4 font-medium">Current</th>
              <th className="px-6 py-4 font-medium">Latest</th>
              <th className="px-6 py-4 font-medium">Severity</th>
              <th className="px-6 py-4 font-medium text-right">Action</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {packages.map((pkg) => {
              const status = statuses[pkg.name];
              const isUpdating = updating[pkg.name];
              const isOk = pkg.severity === "OK";

              return (
                <tr key={pkg.name} className="hover:bg-muted/30 transition-colors">
                  <td className="px-6 py-4 font-medium">{pkg.name}</td>
                  <td className="px-6 py-4 font-mono text-muted-foreground">{pkg.current_version}</td>
                  <td className="px-6 py-4 font-mono text-foreground">{pkg.latest_version}</td>
                  <td className="px-6 py-4">
                    <span className={cn("px-2.5 py-1 text-xs font-semibold rounded-md border", severityColors[pkg.severity] || severityColors.OK)}>
                      {pkg.severity}
                    </span>
                  </td>
                  <td className="px-6 py-4 flex items-center justify-end gap-3">
                    {isUpdating && (
                      <span className="text-muted-foreground flex items-center gap-2 animate-pulse">
                        <div className="w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin" />
                        Updating...
                      </span>
                    )}
                    
                    {!isUpdating && status?.type === "success" && (
                      <span className="text-green-500 flex items-center gap-1">
                        <CheckCircle2 className="w-5 h-5" /> Updated
                        {status.provider && status.provider !== "none" && (
                          <span className="ml-1 text-xs px-2 py-0.5 bg-muted text-muted-foreground rounded-full border">
                            via {status.provider.charAt(0).toUpperCase() + status.provider.slice(1)}
                            {status.provider === "qwen" && " ⚡"}
                          </span>
                        )}
                      </span>
                    )}

                    {!isUpdating && status?.type === "error" && (
                      <div className="flex items-center gap-3">
                        <span className="text-red-500 flex items-center gap-1" title={status.error}>
                          <AlertCircle className="w-4 h-4" /> Error
                        </span>
                        {status.checkpoint && (
                          <button
                            onClick={() => handleRollback(pkg.name, status.checkpoint)}
                            className="flex items-center gap-1 px-3 py-1.5 text-xs font-medium text-white bg-red-600 hover:bg-red-700 rounded-md transition-colors"
                          >
                            <RotateCcw className="w-3.5 h-3.5" /> Rollback
                          </button>
                        )}
                      </div>
                    )}

                    {!isUpdating && !status && (
                      <button
                        onClick={() => handleUpdate(pkg)}
                        disabled={isOk}
                        className={cn(
                          "px-4 py-1.5 text-sm font-medium rounded-md transition-colors",
                          isOk 
                            ? "bg-muted text-muted-foreground cursor-not-allowed" 
                            : "bg-primary text-primary-foreground hover:bg-primary/90"
                        )}
                      >
                        Update
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
            
            {packages.length === 0 && (
              <tr>
                <td colSpan={5} className="px-6 py-8 text-center text-muted-foreground">
                  No packages found. Scan a project first.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
