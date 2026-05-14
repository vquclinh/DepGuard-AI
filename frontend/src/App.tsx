import { useState, useEffect } from "react";
import { ShieldCheck, Activity } from "lucide-react";
import { scanProjectStream, getProviders, browseProject } from "@/hooks/useDepGuard";
import type { PackageData } from "@/components/PackagesTable";
import type { LogEntry } from "@/components/UpdateLog";
import { DashboardView, type HealthData, type ScanProgress } from "@/components/DashboardView";
import { IdeWorkspaceView } from "@/components/IdeWorkspaceView";
import { cn } from "@/lib/utils";

type ViewMode = "dashboard" | "ide";

function App() {
  const [viewMode, setViewMode] = useState<ViewMode>("dashboard");
  const [folderPath, setFolderPath] = useState("/mnt/vquclinh/PROJECT-CMAKE/DEPGUARD-AI/DepGuard-AI");
  const [isScanning, setIsScanning] = useState(false);
  const [providerStatuses, setProviderStatuses] = useState<any[]>([]);
  const [scanProgress, setScanProgress] = useState<ScanProgress | null>(null);
  
  const [healthData, setHealthData] = useState<HealthData | null>(null);
  
  const [packages, setPackages] = useState<PackageData[]>([]);
  const [logs, setLogs] = useState<LogEntry[]>([]);

  const addLog = (message: string, type: "info" | "success" | "error" = "info") => {
    setLogs((prev) => [
      ...prev,
      {
        id: Math.random().toString(36).substr(2, 9),
        time: new Date().toLocaleTimeString('en-US', { hour12: false }),
        message,
        type,
      },
    ]);
  };

  useEffect(() => {
    getProviders()
      .then(data => {
        if (data && data.providers) {
          setProviderStatuses(data.providers);
        }
      })
      .catch(err => console.error("Failed to load providers:", err));
  }, []);

  const handleBrowse = async () => {
    try {
      const result = await browseProject();
      if (result && result.path) {
        setFolderPath(result.path);
        addLog(`Selected directory: ${result.path}`, "info");
      }
    } catch (e: any) {
      addLog(`Browse failed: ${e.message}`, "error");
    }
  };

  const handleScan = () => {
    if (!folderPath) return;
    
    setIsScanning(true);
    setScanProgress({ phase: "Initializing..." });
    addLog(`Initiating scan for: ${folderPath}`, "info");
    
    scanProjectStream(
      folderPath,
      (data) => {
        if (data.phase === "Completed") {
          setHealthData({
            score: data.health_score,
            stats: {
              critical: data.critical,
              high: data.high,
              medium: data.medium,
              low: data.low,
              unpinned: data.unpinned,
              ok: data.ok
            }
          });
          setPackages(data.packages);
          addLog(`Scan complete. Found ${data.total_packages} packages.`, "success");
          setIsScanning(false);
          setScanProgress(null);
        } else {
          setScanProgress(data);
        }
      },
      (error) => {
        addLog(`Scan failed: ${error}`, "error");
        setIsScanning(false);
        setScanProgress(null);
      }
    );
  };

  return (
    <div
      className={cn(
        "bg-[#0f0f0f] text-foreground font-sans flex flex-col",
        viewMode === "ide" ? "h-screen overflow-hidden" : "min-h-screen"
      )}
    >
      <header className="border-b bg-card shadow-sm sticky top-0 z-10 shrink-0">
        <div className="container mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="bg-primary text-primary-foreground p-2 rounded-lg">
              <ShieldCheck className="w-6 h-6" />
            </div>
            <div>
              <h1 className="text-xl font-bold tracking-tight">DepGuard AI</h1>
              <p className="text-xs text-muted-foreground font-medium">Autonomous Dependency Architect</p>
            </div>
          </div>
          
          {providerStatuses.length > 0 && (
            <div className="flex items-center gap-3 text-sm bg-muted/50 px-4 py-2 rounded-full border">
              <span className="font-semibold text-muted-foreground flex items-center gap-1.5">
                <Activity className="w-4 h-4" /> LLM Status:
              </span>
              <div className="flex items-center gap-3 ml-1">
                {providerStatuses.map(p => {
                  const isAvail = p.status === "available";
                  return (
                    <span key={p.name} className={`flex items-center gap-1 ${isAvail ? 'text-green-500 font-medium' : 'text-muted-foreground'}`}>
                      {p.name.charAt(0).toUpperCase() + p.name.slice(1)} {isAvail ? "✅" : (p.name === "qwen" ? "⚡ Kaggle" : "❌")}
                    </span>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      </header>

      <div
        className={cn(
          "relative flex-1 min-h-0",
          viewMode === "ide" ? "overflow-hidden" : "overflow-x-hidden"
        )}
      >
        <div
          className={cn(
            "transition-all duration-300 ease-out",
            viewMode === "dashboard"
              ? "relative translate-x-0 opacity-100"
              : "pointer-events-none absolute inset-x-0 top-0 -translate-x-4 opacity-0",
            viewMode === "dashboard" ? "" : "h-full"
          )}
          aria-hidden={viewMode !== "dashboard"}
        >
          <DashboardView
            folderPath={folderPath}
            setFolderPath={setFolderPath}
            isScanning={isScanning}
            scanProgress={scanProgress}
            healthData={healthData}
            packages={packages}
            logs={logs}
            onBrowse={handleBrowse}
            onScan={handleScan}
            onLog={addLog}
            onOpenIde={() => setViewMode("ide")}
          />
        </div>

        <div
          className={cn(
            "transition-all duration-300 ease-out",
            viewMode === "ide"
              ? "relative h-full translate-x-0 opacity-100"
              : "pointer-events-none absolute inset-x-0 top-0 h-full translate-x-4 opacity-0"
          )}
          aria-hidden={viewMode !== "ide"}
        >
          <IdeWorkspaceView
            folderPath={folderPath}
            packages={packages}
            onLog={addLog}
            onBack={() => setViewMode("dashboard")}
          />
        </div>
      </div>
    </div>
  );
}

export default App;
