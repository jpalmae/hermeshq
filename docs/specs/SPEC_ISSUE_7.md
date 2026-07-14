# SPEC: Fleet Health Observability Dashboard (Issue #7)

**Version**: 1.0  
**Date**: 2026-05-21  
**Status**: Draft  
**Depends on**: None (standalone)  
**Blocks**: Issue #8 (Task Analytics) — health panel establishes the dashboard section layout pattern

---

## 1. Problem

The dashboard shows `active_agents` as a single number with no breakdown. An operator cannot answer:
- "How many agents are running vs stopped vs errored?"
- "What tasks are failing right now?"
- "Which agents had errors in the last 24 hours?"

The `ActivityLog` model already captures `severity="error"` events from `AgentSupervisor._log()`, but the dashboard never queries them.

---

## 2. Architecture Decisions

### 2.1 Endpoint: `GET /dashboard/health`

**Why a new endpoint?**  
The existing `/dashboard/overview` returns `stats` (4 counters) and `activity` (last 12 entries). Adding health data there would bloat the response and the 5-second polling would run expensive GROUP BY queries unnecessarily.

**Decision**: Dedicated endpoint with **10-second polling** (same as Resources tab). Separate from the fast 5-second `/overview` cycle.

### 2.2 SQL Performance

All queries use `GROUP BY` — no row loading. For 3274 tasks and 17 agents, these queries return in <5ms.

### 2.3 No new Pydantic schemas

Existing pattern: dashboard endpoints return raw `dict`. Consistent with current code. Will add schemas in a future cleanup pass across all dashboard endpoints.

### 2.4 No new components folder

Current pattern: all dashboard UI is inline in `DashboardPage.tsx`. We extract two new sub-components inline (not in a separate folder) to keep consistency.

---

## 3. Backend Changes

### 3.1 File: `backend/hermeshq/routers/dashboard.py`

**Add imports** (top of file):
```python
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, true, false
from hermeshq.models.activity import ActivityLog
```

**Add endpoint** (after existing endpoints):
```python
@router.get("/health")
async def fleet_health(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """
    Fleet-wide health summary: agent status breakdown, task outcomes,
    and recent errors from the last 24 hours.
    
    Polled every 10 seconds by the frontend.
    """
    accessible_ids = await get_accessible_agent_ids(db, current_user)
    agent_scope = Agent.id.in_(accessible_ids) if accessible_ids else false()
    task_scope = Task.agent_id.in_(accessible_ids) if accessible_ids else false()

    # --- Agent status breakdown ---
    by_status = await db.execute(
        select(Agent.status, func.count())
        .where(Agent.is_archived == false(), agent_scope)
        .group_by(Agent.status)
    )
    status_breakdown = dict(by_status.all())

    # --- Task outcome summary (all time) ---
    task_outcomes = await db.execute(
        select(Task.status, func.count())
        .where(task_scope)
        .group_by(Task.status)
    )
    task_summary = dict(task_outcomes.all())

    # --- Recent errors (last 24h) ---
    twenty_four_hours_ago = datetime.now(timezone.utc) - timedelta(hours=24)
    recent_errors = await db.execute(
        select(
            ActivityLog.agent_id,
            ActivityLog.message,
            ActivityLog.created_at,
        )
        .where(
            ActivityLog.severity == "error",
            ActivityLog.created_at >= twenty_four_hours_ago,
        )
        .order_by(ActivityLog.created_at.desc())
        .limit(10)
    )

    # Resolve agent names from the error rows
    error_agent_ids = {r[0] for r in recent_errors.all() if r[0]}
    agent_names = {}
    if error_agent_ids:
        name_rows = await db.execute(
            select(Agent.id, Agent.name).where(Agent.id.in_(error_agent_ids))
        )
        agent_names = dict(name_rows.all())

    # Re-execute for results (SQLAlchemy result was consumed above)
    # Actually we need to collect results before the name query. Let me fix:
    # NOTE: We'll refactor to collect rows first, then resolve names.

    return {
        "status_breakdown": status_breakdown,
        "task_summary": task_summary,
        "recent_errors": [
            {
                "agent_id": r.agent_id,
                "agent_name": agent_names.get(r.agent_id, "Unknown"),
                "message": r.message,
                "timestamp": r.created_at.isoformat() if r.created_at else None,
            }
            for r in error_rows
        ],
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
```

**Implementation note**: The `recent_errors` query needs to be executed once, rows collected into a list, then agent names resolved. The pseudo-code above shows the intent; actual implementation must:

```python
# Correct pattern:
error_rows = (await db.execute(
    select(
        ActivityLog.agent_id,
        ActivityLog.message,
        ActivityLog.created_at,
    )
    .where(
        ActivityLog.severity == "error",
        ActivityLog.created_at >= twenty_four_hours_ago,
    )
    .order_by(ActivityLog.created_at.desc())
    .limit(10)
)).all()

error_agent_ids = {r[0] for r in error_rows if r[0]}
agent_names = {}
if error_agent_ids:
    name_rows = await db.execute(
        select(Agent.id, Agent.name).where(Agent.id.in_(error_agent_ids))
    )
    agent_names = dict(name_rows.all())

recent_errors = [
    {
        "agent_id": r[0],
        "agent_name": agent_names.get(r[0], "Unknown"),
        "message": r[1],
        "timestamp": r[2].isoformat() if r[2] else None,
    }
    for r in error_rows
]
```

### 3.2 No other backend files change

No model changes. No migration needed. No schema changes.

---

## 4. Frontend Changes

### 4.1 File: `frontend/src/api/dashboard.ts`

**Add types**:
```typescript
export interface FleetHealthStatus {
  status_breakdown: Record<string, number>;
  task_summary: Record<string, number>;
  recent_errors: Array<{
    agent_id: string | null;
    agent_name: string;
    message: string | null;
    timestamp: string | null;
  }>;
  last_updated: string;
}
```

**Add hook**:
```typescript
export function useFleetHealth() {
  return useQuery({
    queryKey: ["dashboard", "health"],
    queryFn: async () => {
      const { data } = await apiClient.get<FleetHealthStatus>("/dashboard/health");
      return data;
    },
    refetchInterval: 10_000,
  });
}
```

### 4.2 File: `frontend/src/lib/i18n/locales/en/dashboard.ts`

**Add keys** (to existing `dashboard` object):
```typescript
fleetHealth: "Fleet Health",
statusBreakdown: "Agent Status Breakdown",
runningAgents: "Running",
stoppedAgents: "Stopped", 
errorAgents: "Error",
taskOutcomes: "Task Outcomes",
completed: "Completed",
failed: "Failed",
queued: "Queued",
running: "Running",
recentErrors: "Recent Errors ({count})",
noRecentErrors: "No errors in the last 24 hours",
lastUpdated: "Last updated",
agent: "Agent",
message: "Message",
time: "Time",
healthyFleet: "All agents healthy — no errors in the last 24h",
totalAgents: "Total agents",
```

### 4.3 File: `frontend/src/lib/i18n/locales/es/dashboard.ts`

**Add keys** (Spanish translations):
```typescript
fleetHealth: "Salud de la Flota",
statusBreakdown: "Desglose de Estado de Agentes",
runningAgents: "Ejecutándose",
stoppedAgents: "Detenidos",
errorAgents: "Error",
taskOutcomes: "Resultado de Tareas",
completed: "Completadas",
failed: "Fallidas",
queued: "En cola",
running: "Ejecutándose",
recentErrors: "Errores Recientes ({count})",
noRecentErrors: "Sin errores en las últimas 24 horas",
lastUpdated: "Última actualización",
agent: "Agente",
message: "Mensaje",
time: "Hora",
healthyFleet: "Todos los agentes están saludables — sin errores en las últimas 24h",
totalAgents: "Total de agentes",
```

### 4.4 File: `frontend/src/pages/DashboardPage.tsx`

**Changes**:

1. **Import** the new `useFleetHealth` hook
2. **Add inline component** `FleetHealthPanel` before the main `DashboardPage` component:

```tsx
function FleetHealthPanel({ health }: { health: FleetHealthStatus }) {
  const { t } = useI18n();
  
  const statusColors: Record<string, string> = {
    running: "text-[var(--success)]",
    stopped: "text-[var(--text-disabled)]",
    error: "text-[var(--accent)]",
    crashed: "text-[var(--accent)]",
  };

  const taskColors: Record<string, string> = {
    completed: "text-[var(--success)]",
    failed: "text-[var(--accent)]",
    queued: "text-[var(--warning)]",
    running: "text-[var(--info)]",
  };

  return (
    <div className="dashboard-health-card panel-frame p-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <p className="panel-label">{t("dashboard.fleetHealth")}</p>
        <span className="text-xs text-[var(--text-disabled)]">
          {t("dashboard.lastUpdated")}: {new Date(health.last_updated).toLocaleTimeString()}
        </span>
      </div>

      {/* Agent Status Breakdown - horizontal chips */}
      <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-4">
        {Object.entries(health.status_breakdown).map(([status, count]) => (
          <div key={status} className="dashboard-metric-chip">
            <p className="panel-label">{t(`dashboard.${status}Agents`) || status}</p>
            <p className={`mt-1 text-2xl ${statusColors[status] || "text-[var(--text-display)]"}`}>
              {count}
            </p>
          </div>
        ))}
      </div>

      {/* Task Outcomes - horizontal mini stats */}
      <div className="mt-3 flex gap-4 border-t border-[var(--border)] pt-3">
        <p className="panel-label self-center">{t("dashboard.taskOutcomes")}:</p>
        {Object.entries(health.task_summary).map(([status, count]) => (
          <div key={status} className="flex items-center gap-1">
            <span className={`text-sm font-medium ${taskColors[status] || ""}`}>
              {count}
            </span>
            <span className="text-xs text-[var(--text-disabled)]">
              {t(`dashboard.${status}`) || status}
            </span>
          </div>
        ))}
      </div>

      {/* Recent Errors */}
      {health.recent_errors.length > 0 ? (
        <div className="mt-3 border-t border-[var(--border)] pt-3">
          <p className="panel-label text-[var(--accent)]">
            {t("dashboard.recentErrors", { count: health.recent_errors.length })}
          </p>
          <div className="mt-2 max-h-40 overflow-y-auto">
            {health.recent_errors.map((err, i) => (
              <div key={i} className="flex items-start gap-2 py-1 text-sm">
                <span className="text-[var(--accent)]">⚠</span>
                <span className="shrink-0 text-[var(--text-disabled)]">
                  [{err.agent_name}]
                </span>
                <span className="text-[var(--text-secondary)] truncate">
                  {err.message || t("dashboard.noMessage")}
                </span>
                <span className="ml-auto shrink-0 text-xs text-[var(--text-disabled)]">
                  {err.timestamp
                    ? new Date(err.timestamp).toLocaleTimeString()
                    : ""}
                </span>
              </div>
            ))}
          </div>
        </div>
      ) : (
        <p className="mt-3 border-t border-[var(--border)] pt-3 text-sm text-[var(--success)]">
          ✅ {t("dashboard.healthyFleet")}
        </p>
      )}
    </div>
  );
}
```

3. **Insert into the dashboard grid** — add between the Primary Readout and Live Feed sections:

```tsx
// Inside DashboardPage component:
const { data: fleetHealth } = useFleetHealth();

// In the JSX grid, after the primary readout card:
{fleetHealth && <FleetHealthPanel health={fleetHealth} />}
```

### 4.5 File: `frontend/src/index.css` (or equivalent styles)

**Add CSS** (optional — can use Tailwind only):
```css
.dashboard-health-card {
  /* Uses existing panel-frame styling */
}
.dashboard-metric-chip {
  @apply rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-2 text-center;
}
```

---

## 5. API Response Example

```json
{
  "status_breakdown": {
    "running": 5,
    "stopped": 8,
    "error": 2
  },
  "task_summary": {
    "completed": 2841,
    "failed": 127,
    "queued": 15,
    "running": 3
  },
  "recent_errors": [
    {
      "agent_id": "abc-123",
      "agent_name": "support-bot",
      "message": "Provider rate limit exceeded",
      "timestamp": "2026-05-21T14:32:00Z"
    }
  ],
  "last_updated": "2026-05-21T14:35:00Z"
}
```

---

## 6. Performance Analysis

| Query | Rows Scanned | Estimated Time |
|-------|-------------|----------------|
| Agent status GROUP BY | 17 agents (indexed `status`) | <1ms |
| Task outcomes GROUP BY | 3274 tasks (indexed `status`) | <5ms |
| Recent errors (indexed `severity` + `created_at`) | ~50-100 error rows/24h | <2ms |
| Agent name resolution | 1-10 IDs | <1ms |

**Total endpoint**: <10ms for production data sizes. Safe for 10-second polling.

---

## 7. Testing Plan

### Backend Tests
1. `GET /dashboard/health` with admin user → 200, all fields present
2. `GET /dashboard/health` with non-admin user → only sees assigned agents
3. Empty fleet → `status_breakdown: {}`, `task_summary: {}`, `recent_errors: []`
4. Verify `status_breakdown` sums to total non-archived agents
5. Verify `task_summary` sums to total tasks for accessible agents

### Frontend Tests
1. Health panel renders with data
2. Status chips show correct colors
3. Empty errors shows "All agents healthy" message
4. Error list scrolls when >5 errors
5. Last updated timestamp refreshes

---

## 8. Files Changed Summary

| File | Action | Lines Changed |
|------|--------|--------------|
| `backend/hermeshq/routers/dashboard.py` | Add endpoint + imports | ~60 lines added |
| `frontend/src/api/dashboard.ts` | Add type + hook | ~25 lines added |
| `frontend/src/pages/DashboardPage.tsx` | Add FleetHealthPanel + hook usage | ~80 lines added |
| `frontend/src/lib/i18n/locales/en/dashboard.ts` | Add 16 keys | ~16 lines added |
| `frontend/src/lib/i18n/locales/es/dashboard.ts` | Add 16 keys | ~16 lines added |

**Total**: ~197 lines across 5 files. No database changes. No migrations.
