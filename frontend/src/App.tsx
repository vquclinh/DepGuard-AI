import { useState } from "react";
import { FolderSearch, ShieldCheck } from "lucide-react";
import { scanProject } from "@/hooks/useDepGuard";
import { HealthScore } from "@/components/HealthScore";
import { PackagesTable } from "@/components/PackagesTable";
import type { PackageData } from "@/components/PackagesTable";
import { UpdateLog } from "@/components/UpdateLog";
import type { LogEntry } from "@/components/UpdateLog";

function App() {
  const [folderPath, setFolderPath] = useState("/mnt/vquclinh/PROJECT-CMAKE/DEPGUARD-AI/DepGuard-AI");
  const [isScanning, setIsScanning] = useState(false);
  
  const [healthData, setHealthData] = useState<{
    score: number;
    stats: { critical: number; high: number; medium: number; low: number; ok: number };
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

  const handleScan = async () => {
    if (!folderPath) return;
    
    setIsScanning(true);
    addLog(`Initiating scan for: ${folderPath}`, "info");
    
    try {
      const result = await scanProject(folderPath);
      
      setHealthData({
        score: result.health_score,
        stats: {
          critical: result.critical,
          high: result.high,
          medium: result.medium,
          low: result.low,
          ok: result.ok
        }
      });
      
      setPackages(result.packages);
      addLog(`Scan complete. Found ${result.total_packages} packages.`, "success");
    } catch (e: any) {
      addLog(`Scan failed: ${e.message}`, "error");
    } finally {
      setIsScanning(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#0f0f0f] text-foreground font-sans flex flex-col">
      {/* Header */}
      <header className="border-b bg-card shadow-sm sticky top-0 z-10">
        <div className="container mx-auto px-6 py-4 flex items-center gap-3">
          <div className="bg-primary text-primary-foreground p-2 rounded-lg">
            <ShieldCheck className="w-6 h-6" />
          </div>
          <div>
            <h1 className="text-xl font-bold tracking-tight">DepGuard AI</h1>
            <p className="text-xs text-muted-foreground font-medium">Autonomous Dependency Architect</p>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="flex-1 container mx-auto px-6 py-8 flex flex-col gap-8 max-w-6xl">
        
        {/* Scanner Input */}
        <section className="bg-card border rounded-2xl p-6 shadow-sm">
          <div className="flex flex-col md:flex-row gap-4 items-end">
            <div className="flex-1 space-y-2 w-full">
              <label className="text-sm font-semibold ml-1">Target Project Directory</label>
              <div className="relative">
                <FolderSearch className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-muted-foreground" />
                <input
                  type="text"
                  value={folderPath}
                  onChange={(e) => setFolderPath(e.target.value)}
                  placeholder="e.g. /path/to/your/project"
                  className="w-full bg-background border rounded-lg pl-10 pr-4 py-3 text-sm focus:ring-2 focus:ring-primary focus:outline-none transition-all"
                  onKeyDown={(e) => e.key === 'Enter' && handleScan()}
                />
              </div>
            </div>
            <button
              onClick={handleScan}
              disabled={isScanning || !folderPath}
              className="bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed px-8 py-3 rounded-lg font-semibold transition-all h-[46px] flex items-center justify-center shrink-0"
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
        </section>

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
