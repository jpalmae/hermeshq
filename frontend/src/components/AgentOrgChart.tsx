import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { useUpdateAgent } from "../api/agents";
import { AgentAvatar } from "./AgentAvatar";
import type { Agent } from "../types/api";

const BASE_CARD_WIDTH = 300;
const BASE_CARD_HEIGHT = 190;
const MAP_PADDING = 80;
const STORAGE_KEY = "hermeshq.agentMapLayout.v1";

type Position = { x: number; y: number };
type LayoutState = {
  positions: Record<string, Position>;
  height: number;
  zoom: number;
  nodeScale: number;
};

function statusClass(status: string) {
  if (status === "running") return "bg-[var(--success)]";
  if (status === "stopped") return "bg-[var(--text-disabled)]";
  return "bg-[var(--warning)]";
}

function avatarShapeClasses(runtimeProfile: string, hasReports: boolean) {
  const shapeClass =
    runtimeProfile === "security"
      ? "rounded-none org-chart-avatar--security"
      : runtimeProfile === "technical"
        ? "rounded-[1rem] org-chart-avatar--technical"
        : "rounded-full org-chart-avatar--standard";
  return `${shapeClass} ${hasReports ? "org-chart-avatar--supervisor" : ""}`.trim();
}

function buildForest(agents: Agent[]) {
  const childrenByParent = new Map<string | null, Agent[]>();
  for (const agent of agents) {
    const key = agent.supervisor_agent_id ?? null;
    const list = childrenByParent.get(key) ?? [];
    list.push(agent);
    childrenByParent.set(key, list);
  }
  for (const list of childrenByParent.values()) {
    list.sort((left, right) => left.name.localeCompare(right.name));
  }
  return childrenByParent;
}

function buildDefaultPositions(
  agents: Agent[],
  cardWidth: number,
  cardHeight: number,
): Record<string, Position> {
  const tree = buildForest(agents);
  const roots = tree.get(null) ?? [];
  const positions: Record<string, Position> = {};
  let cursorX = MAP_PADDING;
  const columnGap = Math.max(cardWidth + 60, 280);
  const rowGap = Math.max(cardHeight + 70, 240);

  const placeNode = (agent: Agent, depth: number): number => {
    const children = tree.get(agent.id) ?? [];
    if (!children.length) {
      positions[agent.id] = {
        x: cursorX,
        y: MAP_PADDING + depth * rowGap,
      };
      cursorX += columnGap;
      return positions[agent.id].x;
    }

    const childXs = children.map((child) => placeNode(child, depth + 1));
    const minX = Math.min(...childXs);
    const maxX = Math.max(...childXs);
    positions[agent.id] = {
      x: minX + (maxX - minX) / 2,
      y: MAP_PADDING + depth * rowGap,
    };
    return positions[agent.id].x;
  };

  roots.forEach((root) => placeNode(root, 0));

  for (const agent of agents) {
    if (!positions[agent.id]) {
      positions[agent.id] = {
        x: cursorX,
        y: MAP_PADDING,
      };
      cursorX += columnGap;
    }
  }

  return positions;
}

function readStoredLayout(): LayoutState {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return { positions: {}, height: 720, zoom: 1, nodeScale: 1 };
    }
    const parsed = JSON.parse(raw) as Partial<LayoutState>;
    return {
      positions: parsed.positions ?? {},
      height: typeof parsed.height === "number" ? parsed.height : 720,
      zoom: typeof parsed.zoom === "number" ? parsed.zoom : 1,
      nodeScale: typeof parsed.nodeScale === "number" ? parsed.nodeScale : 1,
    };
  } catch {
    return { positions: {}, height: 720, zoom: 1, nodeScale: 1 };
  }
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function computeBounds(
  agents: Agent[],
  positions: Record<string, Position>,
  cardWidth: number,
  cardHeight: number,
) {
  if (!agents.length) {
    return { minX: 0, minY: 0, maxX: 0, maxY: 0 };
  }
  const xs = agents.map((agent) => positions[agent.id]?.x ?? 0);
  const ys = agents.map((agent) => positions[agent.id]?.y ?? 0);
  return {
    minX: Math.min(...xs),
    minY: Math.min(...ys),
    maxX: Math.max(...xs) + cardWidth,
    maxY: Math.max(...ys) + cardHeight,
  };
}

export function AgentOrgChart({
  agents,
  editable = true,
}: {
  agents: Agent[];
  editable?: boolean;
}) {
  const updateAgent = useUpdateAgent();
  const shellRef = useRef<HTMLDivElement | null>(null);
  const resizeState = useRef<{ startY: number; startHeight: number } | null>(null);
  const dragState = useRef<{ agentId: string; startX: number; startY: number; origin: Position } | null>(null);
  const [{ positions, height, zoom, nodeScale }, setLayout] = useState<LayoutState>(() => readStoredLayout());
  const cardWidth = Math.round(BASE_CARD_WIDTH * nodeScale);
  const cardHeight = Math.round(BASE_CARD_HEIGHT * nodeScale);
  const hierarchy = useMemo(() => buildForest(agents), [agents]);
  const subordinateCountByAgent = useMemo(() => {
    const counts = new Map<string, number>();
    for (const agent of agents) {
      counts.set(agent.id, (hierarchy.get(agent.id) ?? []).length);
    }
    return counts;
  }, [agents, hierarchy]);

  const mergedPositions = useMemo(() => {
    const defaults = buildDefaultPositions(agents, cardWidth, cardHeight);
    const next: Record<string, Position> = {};
    for (const agent of agents) {
      next[agent.id] = positions[agent.id] ?? defaults[agent.id];
    }
    return next;
  }, [agents, positions, cardWidth, cardHeight]);

  const bounds = useMemo(
    () => computeBounds(agents, mergedPositions, cardWidth, cardHeight),
    [agents, mergedPositions, cardWidth, cardHeight],
  );

  useEffect(() => {
    window.localStorage.setItem(
      STORAGE_KEY,
        JSON.stringify({
          positions: mergedPositions,
          height,
          zoom,
          nodeScale,
        }),
    );
  }, [mergedPositions, height, zoom, nodeScale]);

  useEffect(() => {
    const onPointerMove = (event: PointerEvent) => {
      if (dragState.current) {
        const { agentId, startX, startY, origin } = dragState.current;
        const deltaX = (event.clientX - startX) / zoom;
        const deltaY = (event.clientY - startY) / zoom;
        setLayout((current) => ({
          ...current,
          positions: {
            ...current.positions,
            [agentId]: {
              x: clamp(origin.x + deltaX, 0, 3200),
              y: clamp(origin.y + deltaY, 0, 3200),
            },
          },
        }));
      }

      if (resizeState.current) {
        const { startY, startHeight } = resizeState.current;
        setLayout((current) => ({
          ...current,
          height: clamp(startHeight + (event.clientY - startY), 420, 1400),
        }));
      }
    };

    const onPointerUp = () => {
      dragState.current = null;
      resizeState.current = null;
    };

    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    return () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
    };
  }, [zoom]);

  function setZoom(nextZoom: number) {
    setLayout((current) => ({ ...current, zoom: clamp(nextZoom, 0.55, 1.8) }));
  }

  function setNodeScale(nextScale: number) {
    setLayout((current) => ({ ...current, nodeScale: clamp(nextScale, 0.72, 1.45) }));
  }

  function fitMap() {
    const shell = shellRef.current;
    if (!shell || !agents.length) {
      return;
    }
    const availableWidth = Math.max(shell.clientWidth - 32, 480);
    const availableHeight = Math.max(height - 32, 360);
    const contentWidth = Math.max(bounds.maxX - bounds.minX + MAP_PADDING * 2, 480);
    const contentHeight = Math.max(bounds.maxY - bounds.minY + MAP_PADDING * 2, 320);
    const nextZoom = Math.min(availableWidth / contentWidth, availableHeight / contentHeight);
    setZoom(nextZoom);
  }

  function resetLayout() {
    setLayout({
      positions: buildDefaultPositions(agents, BASE_CARD_WIDTH, BASE_CARD_HEIGHT),
      height: 720,
      zoom: 1,
      nodeScale: 1,
    });
  }

  function startResize(event: React.PointerEvent<HTMLButtonElement>) {
    resizeState.current = { startY: event.clientY, startHeight: height };
  }

  function startDrag(event: React.PointerEvent<HTMLDivElement>, agentId: string) {
    const origin = mergedPositions[agentId];
    dragState.current = {
      agentId,
      startX: event.clientX,
      startY: event.clientY,
      origin,
    };
  }

  async function onSupervisorChange(agentId: string, value: string) {
    await updateAgent.mutateAsync({
      agentId,
      payload: {
        supervisor_agent_id: value || null,
      },
    });
  }

  const canvasWidth = Math.max(bounds.maxX + MAP_PADDING, 1600);
  const canvasHeight = Math.max(bounds.maxY + MAP_PADDING, height - 48);

  if (!agents.length) {
    return <p className="panel-inline-status">[EMPTY] create agents to build hierarchy</p>;
  }

  return (
    <div className="org-map-shell">
      <div className="org-map-toolbar">
        <div>
          <p className="panel-label">Dependency map</p>
          <p className="mt-2 text-sm text-[var(--text-secondary)]">
            Drag nodes freely, resize the canvas and define reporting lines per agent.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" className="panel-button-secondary !min-h-0 px-4 py-2" onClick={() => setZoom(zoom + 0.12)}>
            +
          </button>
          <button type="button" className="panel-button-secondary !min-h-0 px-4 py-2" onClick={() => setZoom(zoom - 0.12)}>
            -
          </button>
          <button
            type="button"
            className="panel-button-secondary !min-h-0 px-4 py-2"
            onClick={() => setNodeScale(nodeScale + 0.08)}
          >
            Node +
          </button>
          <button
            type="button"
            className="panel-button-secondary !min-h-0 px-4 py-2"
            onClick={() => setNodeScale(nodeScale - 0.08)}
          >
            Node -
          </button>
          <button type="button" className="panel-button-secondary !min-h-0 px-4 py-2" onClick={fitMap}>
            Fit
          </button>
          <button type="button" className="panel-button-secondary !min-h-0 px-4 py-2" onClick={resetLayout}>
            Reset
          </button>
        </div>
      </div>

      <div ref={shellRef} className="org-map-viewport" style={{ height }}>
        <div className="org-map-stage" style={{ width: canvasWidth * zoom, height: canvasHeight * zoom }}>
          <div
            className="org-map-canvas"
            style={{
              width: canvasWidth,
              height: canvasHeight,
              transform: `scale(${zoom})`,
              transformOrigin: "top left",
            }}
          >
            <svg className="org-map-lines" width={canvasWidth} height={canvasHeight}>
              {agents.map((agent) => {
                if (!agent.supervisor_agent_id || !mergedPositions[agent.supervisor_agent_id]) {
                  return null;
                }
                const from = mergedPositions[agent.supervisor_agent_id];
                const to = mergedPositions[agent.id];
                const startX = from.x + cardWidth / 2;
                const startY = from.y + cardHeight;
                const endX = to.x + cardWidth / 2;
                const endY = to.y;
                const midY = startY + (endY - startY) / 2;
                return (
                  <path
                    key={`${agent.supervisor_agent_id}-${agent.id}`}
                    d={`M ${startX} ${startY} L ${startX} ${midY} L ${endX} ${midY} L ${endX} ${endY}`}
                    fill="none"
                    stroke="var(--border-visible)"
                    strokeWidth="1.5"
                  />
                );
              })}
            </svg>

            {agents.map((agent) => {
              const position = mergedPositions[agent.id];
              const options = agents.filter((item) => item.id !== agent.id);
              const hasReports = (subordinateCountByAgent.get(agent.id) ?? 0) > 0;
              const avatarVariantClass = avatarShapeClasses(agent.runtime_profile, hasReports);

              return (
                <article
                  key={agent.id}
                  className="org-map-card"
                  style={{
                    left: position.x,
                    top: position.y,
                    width: cardWidth,
                    minHeight: cardHeight,
                  }}
                >
                  <div
                    className={`org-map-handle ${editable ? "cursor-grab" : "cursor-default"}`}
                    onPointerDown={editable ? (event) => startDrag(event, agent.id) : undefined}
                  >
                    <span className="panel-label">Move</span>
                    <span className="panel-label">{agent.status}</span>
                  </div>

                  <div className="flex items-start justify-between gap-3">
                    <div className="flex min-w-0 items-start gap-4">
                      <div className="relative">
                        <AgentAvatar
                          agent={agent}
                          sizeClass="h-12 w-12"
                          roundedClass=""
                          variantClass={avatarVariantClass}
                          className="org-chart-avatar"
                        />
                        <span className={`org-chart-status ${statusClass(agent.status)}`} />
                      </div>
                      <div className="min-w-0">
                        <Link to={`/agents/${agent.id}`} className="block text-xl text-[var(--text-display)]">
                          {agent.friendly_name || agent.name}
                        </Link>
                        <p className="mt-1 text-sm text-[var(--text-secondary)]">
                          {agent.friendly_name && agent.friendly_name !== agent.name
                            ? `${agent.name} / ${agent.slug}`
                            : agent.slug}
                        </p>
                        <p className="mt-3 text-sm text-[var(--text-primary)]">{agent.model}</p>
                        <p className="mt-1 font-mono text-xs uppercase tracking-[0.12em] text-[var(--text-disabled)]">
                          {agent.provider} / {agent.run_mode}
                        </p>
                      </div>
                    </div>
                    <div className="text-right">
                      <p className="panel-label">Tasks</p>
                      <p className="mt-2 text-sm text-[var(--text-display)]">{agent.total_tasks}</p>
                    </div>
                  </div>

                  <label className="mt-5 block">
                    <span className="panel-label">Reports To</span>
                    <select
                      className="mt-1"
                      value={agent.supervisor_agent_id ?? ""}
                      onChange={(event) => onSupervisorChange(agent.id, event.target.value)}
                      disabled={updateAgent.isPending}
                    >
                      <option value="">No supervisor</option>
                      {options.map((option) => (
                        <option key={option.id} value={option.id}>
                          {option.name}
                        </option>
                      ))}
                    </select>
                  </label>
                </article>
              );
            })}
          </div>
        </div>
      </div>

      <div className="org-map-footer">
        <p className="panel-label">
          zoom {Math.round(zoom * 100)}% / node {Math.round(nodeScale * 100)}% / height {Math.round(height)}px / {agents.length} agents
        </p>
        <button type="button" className="org-map-resize-handle" onPointerDown={startResize}>
          Resize
        </button>
      </div>
    </div>
  );
}
