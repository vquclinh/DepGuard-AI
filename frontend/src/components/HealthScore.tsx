import { cn } from "@/lib/utils";

interface HealthScoreProps {
  score: number;
  stats: {
    critical: number;
    high: number;
    medium: number;
    low: number;
    unpinned: number;
    ok: number;
  };
}

export function HealthScore({ score, stats }: HealthScoreProps) {
  const getColor = (s: number) => {
    if (s >= 80) return "text-green-500";
    if (s >= 50) return "text-orange-500";
    return "text-red-500";
  };

  const getStrokeColor = (s: number) => {
    if (s >= 80) return "stroke-green-500";
    if (s >= 50) return "stroke-orange-500";
    return "stroke-red-500";
  };

  const circumference = 2 * Math.PI * 45;
  const strokeDashoffset = circumference - (score / 100) * circumference;

  return (
    <div className="flex flex-col items-center justify-center p-6 bg-card rounded-xl border">
      <h3 className="text-lg font-semibold mb-6">Project Health</h3>
      
      <div className="relative w-40 h-40 flex items-center justify-center mb-8">
        <svg className="w-full h-full transform -rotate-90" viewBox="0 0 100 100">
          <circle
            cx="50"
            cy="50"
            r="45"
            className="stroke-muted fill-none"
            strokeWidth="10"
          />
          <circle
            cx="50"
            cy="50"
            r="45"
            className={cn("fill-none transition-all duration-1000 ease-in-out", getStrokeColor(score))}
            strokeWidth="10"
            strokeDasharray={circumference}
            strokeDashoffset={strokeDashoffset}
            strokeLinecap="round"
          />
        </svg>
        <div className="absolute flex flex-col items-center justify-center">
          <span className={cn("text-4xl font-bold", getColor(score))}>{score}</span>
          <span className="text-xs text-muted-foreground uppercase tracking-wider mt-1">Score</span>
        </div>
      </div>

      <div className="flex flex-wrap gap-3 justify-center">
        <Badge label="Critical" count={stats.critical} color="bg-red-500/10 text-red-500 border-red-500/20" />
        <Badge label="High" count={stats.high} color="bg-orange-500/10 text-orange-500 border-orange-500/20" />
        <Badge label="Medium" count={stats.medium} color="bg-yellow-500/10 text-yellow-500 border-yellow-500/20" />
        <Badge label="Low" count={stats.low} color="bg-blue-500/10 text-blue-500 border-blue-500/20" />
        <Badge label="Unpinned" count={stats.unpinned} color="bg-amber-500/10 text-amber-500 border-amber-500/20" />
        <Badge label="OK" count={stats.ok} color="bg-green-500/10 text-green-500 border-green-500/20" />
      </div>
    </div>
  );
}

function Badge({ label, count, color }: { label: string; count: number; color: string }) {
  return (
    <div className={cn("px-3 py-1.5 rounded-md border flex items-center gap-2 text-sm font-medium", color)}>
      <span>{label}</span>
      <span className="bg-background/50 px-1.5 py-0.5 rounded text-xs">{count}</span>
    </div>
  );
}
