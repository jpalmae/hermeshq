# SPEC: Task Analytics Dashboard (Issue #8)

## 1. Problem

The current `/dashboard/tasks/stats` endpoint (`dashboard.py:118–131`) loads **every Task row** into Python memory, then iterates to count statuses. With 3274+ tasks in production this is already measurable latency; it grows linearly with every new task. Beyond raw counts there are zero operational metrics — no time-series, no completion latency, no per-agent breakdown. An operator cannot detect degradation or spot failure patterns without manually scrolling the kanban board.

### Existing code that must be replaced

```python
# backend/hermeshq/routers/dashboard.py:118-131
all_tasks = (await db.execute(statement)).scalars().all()
counts: dict[str, int] = {}
for task in all_tasks:
    counts[task.status] = counts.get(task.status, 0) + 1
return {"counts": counts, "total": len(all_tasks)}
```

This must be replaced with a single aggregate endpoint that uses SQL `GROUP BY` — zero rows loaded into ORM.

---

## 2. Architecture Decisions

| Decision | Rationale |
|---|---|
| New endpoint `GET /dashboard/tasks/analytics` | Separate from `/overview` (5s polling); analytics queries are heavier → 30s polling |
| SQL `GROUP BY` + `date_trunc` | Zero ORM rows; DB does the aggregation |
| PostgreSQL `percentile_cont` for P50/P95 | Native function, no Python statistics needed |
| Pure CSS bar chart | No chart library added — consistent with existing dashboard CSS-only style |
| New section below Fleet Health on `DashboardPage.tsx` | Logical grouping; Issue #7 fleet-health section is above |
| 30-second `refetchInterval` | Analytics data is historical, sub-second freshness unnecessary |

---

## 3. Backend Changes

### 3.1 Endpoint

`GET /dashboard/tasks/analytics` — auth via `get_current_user`, scoped to `get_accessible_agent_ids`.

### 3.2 Queries

#### A. Task volume time-series (14 days, stacked by status)

```python
from datetime import timedelta
from sqlalchemy import func, select, literal_column

fourteen_days_ago = datetime.utcnow() - timedelta(days=14)

volume_query = (
    select(
        func.date_trunc("day", Task.created_at).label("day"),
        Task.status,
        func.count().label("count"),
    )
    .where(Task.created_at >= fourteen_days_ago, task_scope)
    .group_by(func.date_trunc("day", Task.created_at), Task.status)
    .order_by(func.date_trunc("day", Task.created_at))
)
# Returns: [(day, status, count), ...]
```

#### B. Completion metrics (avg, P50, P95 — last 7 days)

```python
from sqlalchemy import func, literal

seven_days_ago = datetime.utcnow() - timedelta(days=7)
completed_scope = Task.status == "completed", Task.completed_at >= seven_days_ago, task_scope

duration_expr = func.extract("epoch", Task.completed_at - Task.started_at)

avg_seconds = await db.scalar(
    select(func.avg(duration_expr)).where(*completed_scope)
)

p50_seconds = await db.scalar(
    select(func.percentile_cont(0.5).within_group(duration_expr)).where(*completed_scope)
)

p95_seconds = await db.scalar(
    select(func.percentile_cont(0.95).within_group(duration_expr)).where(*completed_scope)
)
```

> Uses `started_at` → `completed_at` (not `queued_at`) to measure actual execution time.

#### C. Top failing agents (last 7 days)

```python
failure_query = (
    select(
        Task.agent_id,
        Agent.name.label("agent_name"),
        func.count().label("failures"),
    )
    .join(Agent, Task.agent_id == Agent.id)
    .where(Task.status == "failed", Task.created_at >= seven_days_ago, task_scope)
    .group_by(Task.agent_id, Agent.name)
    .order_by(func.count().desc())
    .limit(5)
)
```

#### D. Success rate (last 7 days)

```python
completed_count = await db.scalar(
    select(func.count()).select_from(Task).where(Task.status == "completed", task_scope)
) or 0
failed_count = await db.scalar(
    select(func.count()).select_from(Task).where(Task.status == "failed", task_scope)
) or 0
success_rate = completed_count / (completed_count + failed_count) if (completed_count + failed_count) > 0 else None
```

### 3.3 Response JSON

```json
{
  "volume_timeseries": [
    { "date": "2026-05-08", "completed": 12, "failed": 2, "queued": 1, "running": 0 },
    { "date": "2026-05-09", "completed": 18, "failed": 0, "queued": 3, "running": 1 }
  ],
  "completion_metrics": {
    "avg_seconds": 34.2,
    "p50_seconds": 28.0,
    "p95_seconds": 87.6,
    "sample_size": 142
  },
  "top_failing_agents": [
    { "agent_id": "abc-123", "agent_name": "DataSync", "failures": 7 },
    { "agent_id": "def-456", "agent_name": "EmailBot", "failures": 3 }
  ],
  "success_rate": 0.941
}
```

### 3.4 Implementation location

Append new endpoint to `backend/hermeshq/routers/dashboard.py` after the existing `/tasks/stats` handler (~line 135). The old `/tasks/stats` endpoint is **kept** for backward compatibility but marked `deprecated` in the docstring.

---

## 4. Frontend Changes

### 4.1 TypeScript types

```typescript
// frontend/src/api/dashboard.ts

export interface DailyVolume {
  date: string;            // "YYYY-MM-DD"
  completed: number;
  failed: number;
  queued: number;
  running: number;
}

export interface CompletionMetrics {
  avg_seconds: number | null;
  p50_seconds: number | null;
  p95_seconds: number | null;
  sample_size: number;
}

export interface FailingAgent {
  agent_id: string;
  agent_name: string;
  failures: number;
}

export interface TaskAnalytics {
  volume_timeseries: DailyVolume[];
  completion_metrics: CompletionMetrics;
  top_failing_agents: FailingAgent[];
  success_rate: number | null;
}
```

### 4.2 React Query hook

```typescript
// frontend/src/api/dashboard.ts — append

export function useTaskAnalytics() {
  return useQuery({
    queryKey: ["dashboard", "tasks", "analytics"],
    queryFn: async () => {
      const { data } = await apiClient.get<TaskAnalytics>("/dashboard/tasks/analytics");
      return data;
    },
    refetchInterval: 30_000,
    staleTime: 25_000,
  });
}
```

### 4.3 Component structure

New inline component `TaskAnalyticsPanel` rendered in `DashboardPage.tsx` below the Fleet Health section (~after the `{channels?...}` block, before closing `</div>`).

```
TaskAnalyticsPanel (props: { data: TaskAnalytics | undefined })
├── Section header: "Task Analytics" + success rate badge
├── 14-day volume chart (pure CSS)
│   └── flex row, one bar per day, stacked completed (green) / failed (red)
│       height = (count / maxCount) * 100%
│       date label below each bar (MM-DD)
├── Completion metrics row (3 cards)
│   ├── Avg time
│   ├── P50 time
│   └── P95 time
│   Each: formatted as Xm Ys or Ns
└── Top failing agents list
    └── agent name + failure count, max 5 items
```

Props interface:

```typescript
interface TaskAnalyticsPanelProps {
  data: TaskAnalytics | undefined;
}
```

### 4.4 i18n keys

```json
// EN (~20 keys)
{
  "dashboard.analytics.title": "Task Analytics",
  "dashboard.analytics.successRate": "Success Rate",
  "dashboard.analytics.last14Days": "Last 14 Days",
  "dashboard.analytics.volumeChart": "Daily Task Volume",
  "dashboard.analytics.completed": "Completed",
  "dashboard.analytics.failed": "Failed",
  "dashboard.analytics.completionTime": "Completion Time",
  "dashboard.analytics.avgTime": "Avg",
  "dashboard.analytics.p50Time": "P50",
  "dashboard.analytics.p95Time": "P95",
  "dashboard.analytics.sampleSize": "sample size",
  "dashboard.analytics.topFailingAgents": "Top Failing Agents",
  "dashboard.analytics.failures": "failures",
  "dashboard.analytics.noData": "Not enough data yet",
  "dashboard.analytics.tasks": "tasks",
  "dashboard.analytics.day": "day",
  "dashboard.analytics.days": "days",
  "dashboard.analytics.hours": "hours",
  "dashboard.analytics.minutes": "minutes",
  "dashboard.analytics.seconds": "seconds"
}
```

```json
// ES (~20 keys)
{
  "dashboard.analytics.title": "Analítica de Tareas",
  "dashboard.analytics.successRate": "Tasa de Éxito",
  "dashboard.analytics.last14Days": "Últimos 14 Días",
  "dashboard.analytics.volumeChart": "Volumen Diario de Tareas",
  "dashboard.analytics.completed": "Completadas",
  "dashboard.analytics.failed": "Fallidas",
  "dashboard.analytics.completionTime": "Tiempo de Completado",
  "dashboard.analytics.avgTime": "Prom",
  "dashboard.analytics.p50Time": "P50",
  "dashboard.analytics.p95Time": "P95",
  "dashboard.analytics.sampleSize": "tamaño de muestra",
  "dashboard.analytics.topFailingAgents": "Agentes con Más Fallos",
  "dashboard.analytics.failures": "fallos",
  "dashboard.analytics.noData": "Datos insuficientes",
  "dashboard.analytics.tasks": "tareas",
  "dashboard.analytics.day": "día",
  "dashboard.analytics.days": "días",
  "dashboard.analytics.hours": "horas",
  "dashboard.analytics.minutes": "minutos",
  "dashboard.analytics.seconds": "segundos"
}
```

### 4.5 Exact file changes

| File | Change | Est. lines |
|---|---|---|
| `frontend/src/api/dashboard.ts` | Add 4 interfaces + `useTaskAnalytics` hook | +45 |
| `frontend/src/pages/DashboardPage.tsx` | Add `TaskAnalyticsPanel` component (~60 lines) + render call below channels section | +70 |
| `frontend/src/i18n/locales/en.json` | Add 20 keys under `dashboard.analytics.*` | +20 |
| `frontend/src/i18n/locales/es.json` | Add 20 keys under `dashboard.analytics.*` | +20 |

---

## 5. Performance Analysis

Estimated for production data: **3,274 tasks, 17 agents**.

| Query | Rows scanned | Index used | Est. latency |
|---|---|---|---|
| Volume time-series (14d GROUP BY) | ~400 rows (last 14d) | `ix_tasks_created_at` (inferred) | < 5 ms |
| Completion metrics (7d, percentile_cont) | ~200 completed rows | `status` index + filter | < 10 ms |
| Top failing agents (7d, failed only) | ~30 failed rows | `status` index + `agent_id` FK | < 3 ms |
| Success rate (2 COUNT queries) | Index-only scans | `status` index | < 2 ms |
| **Total endpoint** | | | **< 25 ms** |

Compare to current `/tasks/stats`: loads 3,274 ORM rows → **~800 ms** in production.

All queries use indexed columns (`status`, `created_at`, `agent_id`). No full-table scans. The `percentile_cont` query is the most expensive but operates on a small filtered set (~200 rows). No pagination needed — results are bounded (14 days × ~4 statuses, 5 agents).

---

## 6. Testing Plan

### Backend tests

| Test | Description |
|---|---|
| `test_analytics_empty_db` | No tasks → returns empty timeseries, null metrics, empty failing agents, null success rate |
| `test_analytics_volume_timeseries` | Seed tasks across 3 days with mixed statuses → verify correct grouping and date format |
| `test_analytics_completion_metrics` | Seed 10 completed tasks with known durations → verify avg, P50, P95 within tolerance |
| `test_analytics_top_failing_agents` | Seed 5 agents with varying failure counts → verify top-5 ordered by failures desc |
| `test_analytics_success_rate` | Seed 90 completed + 10 failed → verify 0.9 |
| `test_analytics_scope_isolation` | Non-admin user with restricted agent access → only sees their agents' tasks |

### Frontend tests

| Test | Description |
|---|---|
| `useTaskAnalytics` polling | Verify hook calls endpoint with 30s refetchInterval |
| `TaskAnalyticsPanel` render | Renders without crash with undefined data (loading state) |
| `TaskAnalyticsPanel` chart | Verify correct bar heights for known volume data |
| `TaskAnalyticsPanel` metrics | Verify formatted duration strings (e.g., "1m 28s") |

### Manual smoke test

1. Seed 50 tasks with timestamps spanning last 14 days (mix of statuses).
2. Navigate to `/dashboard`.
3. Verify analytics section appears below channels.
4. Verify bar chart shows 14 bars with stacked colors.
5. Verify completion metrics show reasonable values.
6. Verify top failing agents matches DB query.
7. Open Network tab → confirm 30s polling interval.

---

## 7. Files Changed Summary

| File | Action | Lines changed |
|---|---|---|
| `backend/hermeshq/routers/dashboard.py` | Add `/tasks/analytics` endpoint | +80 |
| `frontend/src/api/dashboard.ts` | Add types + `useTaskAnalytics` hook | +45 |
| `frontend/src/pages/DashboardPage.tsx` | Add `TaskAnalyticsPanel` component + render | +70 |
| `frontend/src/i18n/locales/en.json` | Add analytics keys | +20 |
| `frontend/src/i18n/locales/es.json` | Add analytics keys | +20 |
| `backend/tests/test_dashboard_analytics.py` | New test file | +120 |
| **Total** | | **~355** |
