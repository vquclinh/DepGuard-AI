import { useEffect, useMemo, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
  type ReactFlowInstance,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { createPortal } from "react-dom";
import { AlertCircle, Loader2, Maximize2, RefreshCw, Search, Workflow, X } from "lucide-react";
import { getImpactGraph, type ProjectGraphNode, type ProjectGraphResponse } from "@/hooks/useDepGuard";
import { cn } from "@/lib/utils";

type GraphNodeData = {
  graphNode: ProjectGraphNode;
  visibleLabel: string;
  degree: number;
};

type FlowNode = Node<GraphNodeData, "depguardNode">;

const typeLabels: Record<ProjectGraphNode["type"], string> = {
  function: "Function",
  method: "Method",
  class: "Class",
  module_level: "Module",
  decorator: "Decorator",
};

const nodeStyles: Record<ProjectGraphNode["type"], { dot: string; ring: string; fill: string; minimap: string }> = {
  function: { dot: "bg-sky-500", ring: "ring-sky-500/45", fill: "bg-sky-500/18", minimap: "#0ea5e9" },
  method: { dot: "bg-violet-500", ring: "ring-violet-500/45", fill: "bg-violet-500/18", minimap: "#8b5cf6" },
  class: { dot: "bg-amber-500", ring: "ring-amber-500/55", fill: "bg-amber-500/20", minimap: "#f59e0b" },
  module_level: { dot: "bg-emerald-500", ring: "ring-emerald-500/45", fill: "bg-emerald-500/18", minimap: "#10b981" },
  decorator: { dot: "bg-rose-500", ring: "ring-rose-500/45", fill: "bg-rose-500/18", minimap: "#f43f5e" },
};

const nodeTypes = { depguardNode: DependencyNode };

interface ProjectDependencyGraphProps {
  folderPath: string;
}

function DependencyNode({ data, selected }: NodeProps<FlowNode>) {
  const node = data.graphNode;
  const style = nodeStyles[node.type];
  const size = Math.min(96, Math.max(54, 50 + data.degree * 6));

  return (
    <div className="group relative flex w-[150px] flex-col items-center gap-2 text-center">
      <Handle type="target" position={Position.Left} className="!h-2 !w-2 !border-0 !bg-transparent" />
      <button
        className={cn(
          "grid place-items-center rounded-full border border-white/15 shadow-[0_0_30px_rgb(255_255_255_/_0.08)] ring-2 transition duration-200",
          "backdrop-blur-md hover:scale-105",
          style.fill,
          style.ring,
          selected && "scale-110 ring-4 shadow-[0_0_34px_rgb(255_255_255_/_0.22)]"
        )}
        style={{ width: size, height: size }}
        title={`${data.visibleLabel} (${typeLabels[node.type]})`}
      >
        <span className={cn("h-3 w-3 rounded-full shadow-sm", style.dot)} />
      </button>
      <div
        className={cn(
          "max-w-[150px] rounded-full border border-white/10 bg-background/55 px-2.5 py-1 text-[11px] font-semibold leading-tight",
          "text-foreground/85 shadow-sm backdrop-blur-md transition group-hover:text-foreground",
          selected && "border-primary/40 bg-background/80 text-foreground"
        )}
        title={data.visibleLabel}
      >
        <div className="truncate">{data.visibleLabel}</div>
        <div className="truncate text-[10px] font-medium text-muted-foreground">{typeLabels[node.type]}</div>
      </div>
      <Handle type="source" position={Position.Right} className="!h-2 !w-2 !border-0 !bg-transparent" />
    </div>
  );
}

export function ProjectDependencyGraph({ folderPath }: ProjectDependencyGraphProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [graph, setGraph] = useState<ProjectGraphResponse | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [enabledTypes, setEnabledTypes] = useState<Record<ProjectGraphNode["type"], boolean>>({
    function: true,
    method: true,
    class: true,
    module_level: true,
    decorator: true,
  });
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState("");
  const [flowInstance, setFlowInstance] = useState<ReactFlowInstance<FlowNode, Edge> | null>(null);

  const loadGraph = async (forceRebuild = false) => {
    if (!folderPath) return;

    setIsLoading(true);
    setError("");
    try {
      const data = await getImpactGraph(folderPath, forceRebuild);
      setGraph(data);
      setSelectedNodeId((current) => (
        current && data.nodes.some((node) => node.id === current) ? current : data.nodes[0]?.id ?? null
      ));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load graph");
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    if (isOpen) {
      void loadGraph(false);
    }
  }, [folderPath, isOpen]);

  useEffect(() => {
    if (!isOpen) return;

    const originalOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = originalOverflow;
    };
  }, [isOpen]);

  const filteredGraph = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    const visibleNodes = (graph?.nodes ?? []).filter((node) => {
      if (!enabledTypes[node.type]) return false;
      if (!normalizedQuery) return true;

      const haystack = [
        node.id,
        node.label,
        node.file,
        node.parent ?? "",
        ...node.definesSymbols,
        ...node.referencesSymbols,
      ].join(" ").toLowerCase();

      return haystack.includes(normalizedQuery);
    });

    const visibleIds = new Set(visibleNodes.map((node) => node.id));
    const visibleEdges = (graph?.edges ?? []).filter((edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target));
    return { nodes: visibleNodes, edges: visibleEdges };
  }, [enabledTypes, graph, query]);

  const flowNodes = useMemo<FlowNode[]>(() => {
    const degreeByNode = new Map<string, number>();
    filteredGraph.nodes.forEach((node) => degreeByNode.set(node.id, 0));
    filteredGraph.edges.forEach((edge) => {
      degreeByNode.set(edge.source, (degreeByNode.get(edge.source) ?? 0) + 1);
      degreeByNode.set(edge.target, (degreeByNode.get(edge.target) ?? 0) + 1);
    });

    const nodes = [...filteredGraph.nodes].sort((a, b) => {
      const degreeDiff = (degreeByNode.get(b.id) ?? 0) - (degreeByNode.get(a.id) ?? 0);
      if (degreeDiff !== 0) return degreeDiff;
      return a.id.localeCompare(b.id);
    });

    const goldenAngle = Math.PI * (3 - Math.sqrt(5));
    return nodes.map((node, index) => {
      const degree = degreeByNode.get(node.id) ?? 0;
      const radius = 42 + Math.sqrt(index) * 95;
      const angle = index * goldenAngle;

      return {
        id: node.id,
        type: "depguardNode",
        position: {
          x: Math.cos(angle) * radius,
          y: Math.sin(angle) * radius,
        },
        data: {
          graphNode: node,
          visibleLabel: node.parent ? `${node.parent}.${node.label}` : node.label,
          degree,
        },
      };
    });
  }, [filteredGraph.edges, filteredGraph.nodes]);

  const flowEdges = useMemo<Edge[]>(() => (
    filteredGraph.edges.map((edge) => {
      const isConnected = selectedNodeId ? edge.source === selectedNodeId || edge.target === selectedNodeId : false;

      return {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        type: "smoothstep",
        animated: isConnected,
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: isConnected ? "#f8fafc" : "rgb(148 163 184 / 0.35)",
        },
        style: {
          stroke: isConnected ? "#f8fafc" : "rgb(148 163 184 / 0.25)",
          strokeOpacity: selectedNodeId && !isConnected ? 0.18 : 0.68,
          strokeWidth: isConnected ? 2 : 1,
        },
      };
    })
  ), [filteredGraph.edges, selectedNodeId]);

  const selectedNode = useMemo(
    () => graph?.nodes.find((node) => node.id === selectedNodeId) ?? null,
    [graph?.nodes, selectedNodeId]
  );

  useEffect(() => {
    if (!flowInstance || flowNodes.length === 0 || !isOpen) return;

    const frame = window.requestAnimationFrame(() => {
      void flowInstance.fitView({ padding: 0.18, duration: 450, maxZoom: 1.1 });
    });

    return () => window.cancelAnimationFrame(frame);
  }, [flowEdges.length, flowInstance, flowNodes.length, isOpen, query]);

  const toggleType = (type: ProjectGraphNode["type"]) => {
    setEnabledTypes((current) => ({ ...current, [type]: !current[type] }));
  };

  const graphOverlay = isOpen ? (
    <div className="fixed inset-0 z-[100] h-dvh overflow-hidden bg-background/95 backdrop-blur-md">
      <div className="flex h-full min-h-0 flex-col overflow-hidden">
        <div className="flex shrink-0 flex-col gap-3 border-b bg-card/85 px-4 py-3 shadow-sm lg:flex-row lg:items-center lg:justify-between">
          <div className="flex min-w-0 items-center gap-3">
            <div className="rounded-lg border bg-background p-2">
              <Workflow className="h-5 w-5" />
            </div>
            <div className="min-w-0">
              <h3 className="text-sm font-semibold uppercase tracking-wider text-muted-foreground">Project Graph</h3>
              <p className="truncate text-sm text-foreground">
                {graph ? `${graph.stats.nodes} nodes, ${graph.stats.edges} edges, ${graph.stats.files} files` : "Ready"}
              </p>
            </div>
          </div>

          <div className="flex min-w-0 flex-col gap-2 md:flex-row md:items-center">
            <div className="relative min-w-0 md:w-[280px]">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search graph"
                className="h-9 w-full rounded-lg border bg-background pl-9 pr-3 text-sm outline-none transition focus:border-primary"
              />
            </div>
            <button
              onClick={() => void loadGraph(true)}
              disabled={isLoading}
              title="Rebuild graph"
              className="inline-flex h-9 items-center justify-center gap-2 rounded-lg border bg-background px-3 text-sm font-semibold transition hover:bg-muted disabled:cursor-not-allowed disabled:opacity-60"
            >
              {isLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              Refresh
            </button>
            <button
              onClick={() => void flowInstance?.fitView({ padding: 0.18, duration: 350, maxZoom: 1.1 })}
              disabled={!flowInstance || flowNodes.length === 0}
              title="Fit graph to view"
              className="inline-flex h-9 items-center justify-center gap-2 rounded-lg border bg-background px-3 text-sm font-semibold transition hover:bg-muted disabled:cursor-not-allowed disabled:opacity-60"
            >
              <Maximize2 className="h-4 w-4" />
              Fit
            </button>
            <button
              onClick={() => setIsOpen(false)}
              title="Close graph"
              className="inline-flex h-9 items-center justify-center gap-2 rounded-lg border bg-background px-3 text-sm font-semibold transition hover:bg-muted"
            >
              <X className="h-4 w-4" />
              Close
            </button>
          </div>
        </div>

        <div className="grid min-h-0 flex-1 overflow-hidden lg:grid-cols-[minmax(0,1fr)_minmax(360px,30vw)]">
          <div className="flex min-h-0 flex-col overflow-hidden">
            <div className="flex shrink-0 flex-wrap items-center gap-2 border-b px-4 py-2">
              {(Object.keys(typeLabels) as ProjectGraphNode["type"][]).map((type) => (
                <button
                  key={type}
                  onClick={() => toggleType(type)}
                  className={cn(
                    "inline-flex h-8 items-center gap-2 rounded-md border px-2.5 text-xs font-medium transition",
                    enabledTypes[type] ? "bg-background text-foreground" : "bg-muted/40 text-muted-foreground opacity-60"
                  )}
                >
                  <span className={cn("h-2 w-2 rounded-full", nodeStyles[type].dot)} />
                  {typeLabels[type]}
                </button>
              ))}
            </div>

            <div className="relative min-h-0 flex-1 overflow-hidden bg-background">
              {error && (
                <div className="absolute left-4 top-4 z-10 flex max-w-lg items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-600">
                  <AlertCircle className="h-4 w-4 shrink-0" />
                  {error}
                </div>
              )}

              {isLoading && !graph ? (
                <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Loading graph
                </div>
              ) : (
                <ReactFlow
                  className="depguard-flow"
                  nodes={flowNodes}
                  edges={flowEdges}
                  nodeTypes={nodeTypes}
                  minZoom={0.18}
                  maxZoom={1.8}
                  onInit={setFlowInstance}
                  onNodeClick={(_, node) => setSelectedNodeId(node.id)}
                  onPaneClick={() => setSelectedNodeId(null)}
                  proOptions={{ hideAttribution: true }}
                >
                  <Background gap={28} size={1} color="rgb(148 163 184 / 0.12)" />
                  <Controls showInteractive={false} />
                  <MiniMap
                    pannable
                    zoomable
                    nodeColor={(node) => nodeStyles[(node.data as GraphNodeData).graphNode.type].minimap}
                    maskColor="rgb(0 0 0 / 0.08)"
                  />
                </ReactFlow>
              )}
            </div>
          </div>

          <aside className="min-h-0 min-w-0 overflow-hidden border-t bg-background/70 lg:border-l lg:border-t-0">
            <div className="h-full overflow-y-auto p-4">
              {selectedNode ? (
                <NodeInfoTable node={selectedNode} />
              ) : (
                <div className="flex h-full min-h-[240px] items-center justify-center rounded-lg border border-dashed text-sm text-muted-foreground">
                  Select a node
                </div>
              )}
            </div>
          </aside>
        </div>
      </div>
    </div>
  ) : null;

  return (
    <>
      <section className="rounded-xl border bg-card p-4">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div className="flex items-center gap-3">
            <div className="rounded-lg border bg-background p-2">
              <Workflow className="h-5 w-5" />
            </div>
            <div>
              <h3 className="text-sm font-semibold uppercase tracking-wider text-muted-foreground">Project Graph</h3>
              <p className="text-sm text-foreground">Open the interactive code dependency map in a focused view.</p>
            </div>
          </div>
          <button
            onClick={() => setIsOpen(true)}
            className="inline-flex h-10 items-center justify-center gap-2 rounded-lg bg-primary px-4 text-sm font-semibold text-primary-foreground transition hover:bg-primary/90"
          >
            <Workflow className="h-4 w-4" />
            Open Graph
          </button>
        </div>
      </section>

      {graphOverlay ? createPortal(graphOverlay, document.body) : null}
    </>
  );
}

function NodeInfoTable({ node }: { node: ProjectGraphNode }) {
  const rows = [
    { label: "Name", value: node.parent ? `${node.parent}.${node.label}` : node.label },
    { label: "Type", value: typeLabels[node.type] },
    { label: "File", value: node.file },
    { label: "Lines", value: `${node.startLine}-${node.endLine}` },
    { label: "Defines", value: node.definesSymbols },
    { label: "References", value: node.referencesSymbols },
    { label: "Calls", value: node.calls },
  ];

  return (
    <div className="space-y-4">
      <div>
        <h4 className="text-sm font-semibold uppercase tracking-wider text-muted-foreground">Selected Node</h4>
        <p className="mt-1 break-words text-lg font-semibold">{node.parent ? `${node.parent}.${node.label}` : node.label}</p>
      </div>

      <div className="overflow-x-auto rounded-lg border bg-card">
        <table className="w-full min-w-0 table-fixed text-left text-sm">
          <tbody className="divide-y">
            {rows.map((row) => (
              <tr key={row.label}>
                <th className="w-24 bg-muted/35 px-3 py-3 align-top text-xs font-semibold uppercase tracking-wider text-muted-foreground sm:w-32">
                  {row.label}
                </th>
                <td className="min-w-0 px-3 py-3">
                  {Array.isArray(row.value) ? <ValueChips values={row.value} /> : <span className="break-all sm:break-words">{row.value}</span>}
                </td>
              </tr>
            ))}
            <tr>
              <th className="w-24 bg-muted/35 px-3 py-3 align-top text-xs font-semibold uppercase tracking-wider text-muted-foreground sm:w-32">
                Return Usage
              </th>
              <td className="min-w-0 px-3 py-3">
                {Object.keys(node.callReturnUsage).length > 0 ? (
                  <div className="space-y-2">
                    {Object.entries(node.callReturnUsage).map(([call, attrs]) => (
                      <div key={call} className="min-w-0 rounded-md border bg-background p-2 text-xs">
                        <div className="break-all font-semibold">{call}</div>
                        <div className="mt-1 break-words text-muted-foreground">{attrs.join(", ")}</div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <span className="text-muted-foreground">None</span>
                )}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div>
        <h5 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">Source</h5>
        <pre className="max-h-[300px] overflow-auto rounded-lg border bg-card p-3 text-xs leading-relaxed text-card-foreground">
          <code>{node.source}</code>
        </pre>
      </div>
    </div>
  );
}

function ValueChips({ values }: { values: string[] }) {
  return values.length === 0 ? (
    <span className="text-muted-foreground">None</span>
  ) : (
    <div className="max-h-52 min-w-0 overflow-y-auto pr-1">
      <div className="flex min-w-0 flex-wrap content-start gap-1.5">
        {values.map((value) => (
        <span
          key={value}
          className="min-w-0 max-w-full whitespace-normal break-all rounded-md border bg-background px-2 py-1 text-xs leading-relaxed"
          title={value}
        >
          {value}
        </span>
      ))}
      </div>
    </div>
  );
}
