import React, { useState, useMemo } from "react";
import { cn } from "@/lib/utils";
import { previewUpdate, rollbackPackage, type ApplyResponse, type PreviewResponse } from "@/hooks/useDepGuard";
import { AlertCircle, CheckCircle2, RotateCcw, ChevronRight, ChevronDown, MoreHorizontal, ExternalLink } from "lucide-react";
import { DiffReviewPanel } from "@/components/DiffReviewPanel";

const getRegistryUrl = (ecosystem: string, packageName: string) => {
  const name = encodeURIComponent(packageName);
  switch (ecosystem.toLowerCase()) {
    case 'npm': return `https://www.npmjs.com/package/${packageName}`;
    case 'pypi': return `https://pypi.org/project/${packageName}/`;
    case 'cargo': return `https://crates.io/crates/${packageName}`;
    case 'go': return `https://pkg.go.dev/${packageName}`;
    case 'maven': return `https://mvnrepository.com/artifact/${packageName.replace(':', '/')}`;
    default: return `https://deps.dev/${ecosystem.toLowerCase()}/${name}`;
  }
};

export interface PackageData {
  name: string;
  current_version: string;
  latest_version: string;
  ecosystem: string;
  severity: string;
  file_path: string;
  resolved_from?: string;
  cves?: { cve_id: string; summary: string; severity?: string }[];
  message?: string;
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
  UNPINNED: "bg-amber-500/10 text-amber-500 border-amber-500/20",
  OK: "bg-green-500/10 text-green-500 border-green-500/20"
};

export function PackagesTable({ folderPath, packages, onLog }: PackagesTableProps) {
  const [updating, setUpdating] = useState<Record<string, boolean>>({});
  const [statuses, setStatuses] = useState<Record<string, { type: "success" | "error", error?: string, checkpoint?: string, provider?: string }>>({});
  const [expandedRows, setExpandedRows] = useState<Record<string, boolean>>({});
  const [activePopup, setActivePopup] = useState<string | null>(null);
  const [previewData, setPreviewData] = useState<PreviewResponse | null>(null);
  const [previewPackageName, setPreviewPackageName] = useState("");

  // Filtering & Sorting State
  const [filterEcosystem, setFilterEcosystem] = useState<string>("All");
  const [filterSeverity, setFilterSeverity] = useState<string>("All");
  const [filterVulnerable, setFilterVulnerable] = useState(false);
  const [filterOutdated, setFilterOutdated] = useState(false);
  const [filterUnpinned, setFilterUnpinned] = useState(false);
  const [sortBy, setSortBy] = useState<string>("severity");

  const toggleRow = (name: string) => setExpandedRows(p => ({ ...p, [name]: !p[name] }));

  // Derived state
  const ecosystems = useMemo(() => Array.from(new Set(packages.map(p => p.ecosystem))), [packages]);
  
  const processedPackages = useMemo(() => {
    let filtered = packages.filter(pkg => {
      if (filterEcosystem !== "All" && pkg.ecosystem !== filterEcosystem) return false;
      if (filterSeverity !== "All" && pkg.severity !== filterSeverity) return false;
      if (filterVulnerable && (!pkg.cves || pkg.cves.length === 0)) return false;
      if (filterOutdated) {
        // Strip out 'v' prefixes and npm semver characters (^, ~, >=, etc) for clean comparison
        const cur = (pkg.current_version || "").replace(/^[\^~=<>v\s]+/i, "");
        const lat = (pkg.latest_version || "").replace(/^[\^~=<>v\s]+/i, "");
        if (cur === lat) return false;
      }
      if (filterUnpinned && pkg.severity !== "UNPINNED") return false;
      return true;
    });

    const severityOrder: Record<string, number> = { CRITICAL: 5, HIGH: 4, UNPINNED: 3.5, MEDIUM: 3, LOW: 2, OK: 1 };
    
    filtered.sort((a, b) => {
      if (sortBy === "severity") {
        const diff = (severityOrder[b.severity] || 0) - (severityOrder[a.severity] || 0);
        if (diff !== 0) return diff;
      }
      return a.name.localeCompare(b.name);
    });

    return filtered;
  }, [packages, filterEcosystem, filterSeverity, filterVulnerable, filterOutdated, filterUnpinned, sortBy]);

  const handleUpdate = async (pkg: PackageData) => {
    setUpdating(prev => ({ ...prev, [pkg.name]: true }));
    onLog(`Preparing preview for ${pkg.name} (${pkg.current_version} -> ${pkg.latest_version})...`, "info");
    
    try {
      const result = await previewUpdate(folderPath, pkg);
      if (result.files.length === 0) {
        setStatuses(prev => ({ ...prev, [pkg.name]: { type: "success" } }));
        onLog(`No file changes needed for ${pkg.name}.`, "success");
      } else {
        setPreviewData(result);
        setPreviewPackageName(pkg.name);
        onLog(`Preview ready for ${pkg.name}: ${result.summary.total_files_changed} file(s) changed.`, "info");
      }
    } catch (e: any) {
      setStatuses(prev => ({ ...prev, [pkg.name]: { type: "error", error: e.message } }));
      onLog(`Error previewing ${pkg.name}: ${e.message}`, "error");
    } finally {
      setUpdating(prev => ({ ...prev, [pkg.name]: false }));
    }
  };

  const handlePreviewApplied = (result: ApplyResponse) => {
    if (previewPackageName) {
      setStatuses(prev => ({ ...prev, [previewPackageName]: { type: "success" } }));
      onLog(`Applied ${previewPackageName}: ${result.files_accepted.length} file(s) accepted.`, "success");
      if (result.verification?.status === "passed") {
        onLog(`Checker passed for ${previewPackageName}.`, "success");
      } else if (result.repair?.status === "success") {
        onLog(`Repair Agent fixed ${previewPackageName} after checker feedback.`, "success");
      } else if (result.verification?.status === "failed") {
        onLog(`Checker still reports errors for ${previewPackageName}. Review the pipeline output.`, "error");
      }
    }
    setPreviewData(null);
    setPreviewPackageName("");
  };

  const handlePreviewDiscarded = () => {
    if (previewPackageName) {
      onLog(`Discarded preview for ${previewPackageName}.`, "info");
    }
    setPreviewData(null);
    setPreviewPackageName("");
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
    <div className="space-y-4">
      {previewData && (
        <DiffReviewPanel
          preview={previewData}
          onApplied={handlePreviewApplied}
          onDiscarded={handlePreviewDiscarded}
          onError={(message) => onLog(message, "error")}
          onPreviewUpdated={setPreviewData}
        />
      )}
      {/* Controls Bar */}
      <div className="flex flex-wrap gap-4 items-center justify-between bg-card p-4 rounded-xl border">
        <div className="flex flex-wrap gap-3 items-center">
          <select 
            value={filterEcosystem} onChange={e => setFilterEcosystem(e.target.value)}
            className="bg-background border rounded-md px-3 py-1.5 text-sm outline-none focus:border-primary"
          >
            <option value="All">All Ecosystems</option>
            {ecosystems.map(e => <option key={e} value={e}>{e}</option>)}
          </select>
          
          <select 
            value={filterSeverity} onChange={e => setFilterSeverity(e.target.value)}
            className="bg-background border rounded-md px-3 py-1.5 text-sm outline-none focus:border-primary"
          >
            <option value="All">All Severities</option>
            <option value="CRITICAL">Critical</option>
            <option value="HIGH">High</option>
            <option value="MEDIUM">Medium</option>
            <option value="LOW">Low</option>
            <option value="UNPINNED">Unpinned</option>
            <option value="OK">OK</option>
          </select>

          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <input type="checkbox" checked={filterVulnerable} onChange={e => setFilterVulnerable(e.target.checked)} className="rounded border-input" />
            Vulnerable
          </label>
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <input type="checkbox" checked={filterOutdated} onChange={e => setFilterOutdated(e.target.checked)} className="rounded border-input" />
            Outdated
          </label>
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <input type="checkbox" checked={filterUnpinned} onChange={e => setFilterUnpinned(e.target.checked)} className="rounded border-input" />
            Unpinned
          </label>
        </div>
        
        <div className="flex items-center gap-2">
          <span className="text-sm text-muted-foreground">Sort by:</span>
          <select 
            value={sortBy} onChange={e => setSortBy(e.target.value)}
            className="bg-background border rounded-md px-3 py-1.5 text-sm outline-none focus:border-primary"
          >
            <option value="severity">Severity</option>
            <option value="alpha">Alphabetical</option>
          </select>
        </div>
      </div>

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
            {processedPackages.length === 0 ? (
              <tr>
                <td colSpan={6} className="px-6 py-8 text-center text-muted-foreground">
                  No packages match the current filters.
                </td>
              </tr>
            ) : (
              Object.entries(
                processedPackages.reduce((acc, pkg) => {
                  const eco = pkg.ecosystem || "Unknown";
                  if (!acc[eco]) acc[eco] = [];
                  acc[eco].push(pkg);
                  return acc;
                }, {} as Record<string, PackageData[]>)
              )
                .sort(([a], [b]) => a.localeCompare(b))
                .map(([ecosystem, ecoPackages]) => (
                  <React.Fragment key={ecosystem}>
                    {/* Ecosystem Header Row */}
                    <tr className="bg-muted/20">
                      <td colSpan={6} className="px-6 py-2 text-xs font-bold text-muted-foreground uppercase tracking-wider bg-muted/10 border-y">
                        {ecosystem} Ecosystem
                      </td>
                    </tr>
                    {/* Ecosystem Packages */}
                    {ecoPackages.map((pkg) => {
              const status = statuses[pkg.name];
              const isUpdating = updating[pkg.name];
              const isOk = pkg.severity === "OK";
              const isUnpinned = pkg.severity === "UNPINNED";
              const btnLabel = isUnpinned ? "Scan & Pin" : "Update";

              const isExpanded = expandedRows[pkg.name];
              const hasDetails = (pkg.cves && pkg.cves.length > 0) || pkg.message || isUnpinned;

              return (
                <React.Fragment key={pkg.name}>
                  <tr className="hover:bg-muted/30 transition-colors group">
                    <td className="px-6 py-4 font-medium flex items-center gap-2">
                      <button 
                        onClick={() => hasDetails && toggleRow(pkg.name)}
                        className={cn("p-1 rounded-md transition-colors", hasDetails ? "hover:bg-muted text-foreground" : "text-transparent cursor-default")}
                      >
                        {isExpanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                      </button>
                      <div className="flex flex-col">
                        <div className="flex items-center gap-2">
                          <span>{pkg.name}</span>
                          <div className="relative">
                            <button 
                              onClick={(e) => { e.stopPropagation(); setActivePopup(activePopup === pkg.name ? null : pkg.name); }}
                              className={cn(
                                "p-1.5 rounded-md transition-colors", 
                                activePopup === pkg.name ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted/50 hover:text-foreground"
                              )}
                              title="More information"
                            >
                              <MoreHorizontal className="w-4 h-4" />
                            </button>
                            
                            {activePopup === pkg.name && (
                              <>
                                <div className="fixed inset-0 z-40" onClick={(e) => { e.stopPropagation(); setActivePopup(null); }} />
                                <div className="absolute left-0 top-full mt-2 w-64 p-3 bg-popover text-popover-foreground text-sm rounded-lg shadow-lg border z-50 animate-in fade-in zoom-in-95 duration-200 cursor-default" onClick={e => e.stopPropagation()}>
                                  <div className="font-semibold border-b border-border/50 pb-2 mb-2">Package Details</div>
                                  <div className="space-y-2">
                                    <p className="flex justify-between"><span className="text-muted-foreground">Source:</span> <span>{pkg.file_path.split('/').pop()}</span></p>
                                    <p className="flex justify-between"><span className="text-muted-foreground">Resolved via:</span> <span className="capitalize">{pkg.resolved_from || "manifest"}</span></p>
                                    <p className="flex justify-between"><span className="text-muted-foreground">Ecosystem:</span> <span className="capitalize">{ecosystem}</span></p>
                                    
                                    <div className="pt-2 mt-2 border-t border-border/50 flex flex-col gap-2">
                                      <a 
                                        href={getRegistryUrl(ecosystem, pkg.name)} 
                                        target="_blank" 
                                        rel="noopener noreferrer"
                                        className="text-primary hover:underline flex items-center gap-1.5"
                                      >
                                        <ExternalLink className="w-3.5 h-3.5" /> Registry Changelog
                                      </a>
                                      <a 
                                        href={ecosystem.toLowerCase() === 'maven' ? getRegistryUrl(ecosystem, pkg.name) : `https://deps.dev/${ecosystem.toLowerCase()}/${encodeURIComponent(pkg.name)}`} 
                                        target="_blank" 
                                        rel="noopener noreferrer"
                                        className="text-primary hover:underline flex items-center gap-1.5"
                                      >
                                        <ExternalLink className="w-3.5 h-3.5" /> Deps.dev Insight
                                      </a>
                                    </div>
                                  </div>
                                </div>
                              </>
                            )}
                          </div>
                        </div>
                        {isUnpinned && (
                          <div className="text-xs text-amber-500/80 mt-0.5">
                            ⚠ Version not pinned
                          </div>
                        )}
                      </div>
                    </td>
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
                          "px-4 py-1.5 text-sm font-medium rounded-md transition-all duration-200",
                          isOk 
                            ? "bg-muted text-muted-foreground cursor-not-allowed" 
                            : "bg-primary text-primary-foreground hover:bg-primary/90 shadow-sm"
                        )}
                      >
                        {btnLabel}
                      </button>
                    )}
                  </td>
                </tr>
                {/* Expanded Details Row */}
                {isExpanded && hasDetails && (
                  <tr className="bg-muted/10 border-b border-border/50">
                    <td colSpan={6} className="px-14 py-4 text-sm">
                      <div className="space-y-4 max-w-4xl">
                        {pkg.message && (
                          <div className="bg-background border rounded-md p-3 text-muted-foreground font-mono text-xs">
                            {pkg.message}
                          </div>
                        )}
                        {pkg.cves && pkg.cves.length > 0 && (
                          <div>
                            <h4 className="font-semibold mb-2 flex items-center gap-2">
                              <AlertCircle className="w-4 h-4 text-red-500" /> Vulnerabilities ({pkg.cves.length})
                            </h4>
                            <div className="grid gap-2">
                              {pkg.cves.map(cve => (
                                <div key={cve.cve_id} className="bg-background border rounded-md p-3">
                                  <div className="font-bold text-red-500 text-xs mb-1">{cve.cve_id} {cve.severity ? `(${cve.severity})` : ''}</div>
                                  <p className="text-muted-foreground text-xs">{cve.summary}</p>
                                </div>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    </td>
                  </tr>
                )}
                </React.Fragment>
              );
            })}
                  </React.Fragment>
                ))
            )}
          </tbody>
        </table>
      </div>
    </div>
    </div>
  );
}
