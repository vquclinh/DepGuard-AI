import { useState, useEffect } from "react";
import { FolderSearch, ShieldCheck, Activity } from "lucide-react";
import { scanProjectStream, getProviders, browseProject } from "@/hooks/useDepGuard";
import { HealthScore } from "@/components/HealthScore";
import { PackagesTable } from "@/components/PackagesTable";
import type { PackageData } from "@/components/PackagesTable";
import { UpdateLog } from "@/components/UpdateLog";
import type { LogEntry } from "@/components/UpdateLog";
import { ProjectDependencyGraph } from "@/components/ProjectDependencyGraph";

function App() {
  const [folderPath, setFolderPath] = useState("/mnt/vquclinh/PROJECT-CMAKE/DEPGUARD-AI/DepGuard-AI");
  const [isScanning, setIsScanning] = useState(false);
  const [providerStatuses, setProviderStatuses] = useState<any[]>([]);
  const [scanProgress, setScanProgress] = useState<{
    phase: string;
    message?: string;
    package?: string;
    total_packages?: number;
  } | null>(null);
  
  const [healthData, setHealthData] = useState<{
    score: number;
    stats: { critical: number; high: number; medium: number; low: number; unpinned: number; ok: number };
  } | null>(null);
  
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
    <div className="min-h-screen bg-[#0f0f0f] text-foreground font-sans flex flex-col">
      {/* Header */}
      <header className="border-b bg-card shadow-sm sticky top-0 z-10">
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

      {/* Main Content */}
      <main className="flex-1 container mx-auto px-6 py-8 flex flex-col gap-8 max-w-7xl">
        
        {/* Scanner Input */}
        <section className="bg-card border border-border/60 rounded-2xl p-6 shadow-sm relative overflow-hidden">
          <div className="flex flex-col md:flex-row gap-4 items-end relative z-10">
            <div className="flex-1 space-y-2 w-full">
              <label className="text-sm font-semibold ml-1 text-muted-foreground uppercase tracking-wider">Target Project Directory</label>
              <div className="flex gap-2">
                <div className="relative flex-1">
                  <FolderSearch className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-muted-foreground" />
                  <input
                    type="text"
                    value={folderPath}
                    onChange={(e) => setFolderPath(e.target.value)}
                    placeholder="e.g. /path/to/your/project"
                    className="w-full bg-background border border-border/60 rounded-lg pl-10 pr-4 py-3 text-sm focus:ring-2 focus:ring-primary focus:border-primary focus:outline-none transition-all"
                    onKeyDown={(e) => e.key === 'Enter' && handleScan()}
                    disabled={isScanning}
                  />
                </div>
                <button
                  onClick={handleBrowse}
                  disabled={isScanning}
                  className="bg-secondary text-secondary-foreground hover:bg-secondary/80 border border-border/60 px-4 py-3 rounded-lg font-semibold transition-all h-[46px] flex items-center justify-center shrink-0 disabled:opacity-50"
                >
                  Browse
                </button>
              </div>
            </div>
            <button
              onClick={handleScan}
              disabled={isScanning || !folderPath}
              className="bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed px-8 py-3 rounded-lg font-semibold transition-all h-[46px] flex items-center justify-center shrink-0 shadow-sm"
            >
              {isScanning ? (
                <span className="flex items-center gap-2">
                  <div className="w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin" />
                  Scanning...
                </span>
              ) : (
                "Scan Project"
              )}
            </button>
          </div>

          {/* Progress Overlay */}
          {isScanning && scanProgress && (
            <div className="mt-6 pt-6 border-t border-border/50 animate-in fade-in slide-in-from-top-2 duration-300">
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-2">
                  <Activity className="w-4 h-4 text-primary animate-pulse" />
                  <span className="text-sm font-semibold">{scanProgress.phase}</span>
                </div>
                {scanProgress.total_packages && (
                  <span className="text-xs text-muted-foreground font-mono">
                    Total Packages: {scanProgress.total_packages}
                  </span>
                )}
              </div>
              {scanProgress.message && (
                <p className="text-sm text-muted-foreground truncate font-mono bg-muted/30 px-3 py-1.5 rounded border border-border/40">
                  {scanProgress.message}
                </p>
              )}
              {scanProgress.package && (
                <p className="text-sm text-muted-foreground truncate font-mono bg-muted/30 px-3 py-1.5 rounded border border-border/40">
                  Analyzing: <span className="text-foreground">{scanProgress.package}</span>
                </p>
              )}
            </div>
          )}
        </section>

        <ProjectDependencyGraph folderPath={folderPath} />

        {/* Results Area */}
        <div className="flex flex-col lg:flex-row gap-8 items-start">
          
          <div className="w-full lg:w-1/3 flex flex-col gap-8 shrink-0">
            {healthData && (
              <section className="animate-in fade-in slide-in-from-bottom-4 duration-500">
                <HealthScore score={healthData.score} stats={healthData.stats} />
              </section>
            )}
            
            <section className="sticky top-24 w-full">
              <h3 className="text-sm font-semibold mb-3 ml-1 text-muted-foreground uppercase tracking-wider">Activity Log</h3>
              <UpdateLog logs={logs} />
            </section>
          </div>

          <div className="w-full lg:w-2/3 flex flex-col gap-8">
            {packages.length > 0 && (
              <section className="animate-in fade-in slide-in-from-bottom-6 duration-700">
                <h3 className="text-sm font-semibold mb-3 ml-1 text-muted-foreground uppercase tracking-wider">Dependencies ({packages.length})</h3>
                <PackagesTable folderPath={folderPath} packages={packages} onLog={addLog} />
              </section>
            )}
          </div>
          
        </div>
      </main>
    </div>
  );
}

export default App;
