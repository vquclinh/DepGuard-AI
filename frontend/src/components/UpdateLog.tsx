import { useEffect, useRef } from "react";
import { cn } from "@/lib/utils";
import { Terminal } from "lucide-react";

export interface LogEntry {
  id: string;
  time: string;
  message: string;
  type: "info" | "success" | "error";
}

export function UpdateLog({ logs }: { logs: LogEntry[] }) {
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [logs]);

  return (
    <div className="w-full h-64 border rounded-xl bg-black/90 text-gray-300 font-mono text-sm overflow-hidden flex flex-col shadow-inner">
      <div className="bg-muted/10 px-4 py-2 border-b flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        <Terminal className="w-4 h-4" /> System Logs
      </div>
      <div ref={scrollRef} className="flex-1 overflow-y-auto p-4 space-y-2">
        {logs.map((log) => (
          <div key={log.id} className="flex gap-3 leading-relaxed">
            <span className="text-gray-500 shrink-0">[{log.time}]</span>
            <span
              className={cn(
                "break-all",
                log.type === "success" && "text-green-400",
                log.type === "error" && "text-red-400",
                log.type === "info" && "text-gray-300"
              )}
            >
              {log.message}
            </span>
          </div>
        ))}
        {logs.length === 0 && (
          <div className="text-gray-600 italic">Waiting for operations...</div>
        )}
      </div>
    </div>
  );
}
