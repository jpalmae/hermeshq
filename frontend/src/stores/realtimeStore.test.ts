import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useRealtimeStore } from "./realtimeStore";

const progress = (taskId: string, message: string) => ({
  type: "task.progress",
  task_id: taskId,
  agent_id: "a1",
  message,
});

describe("realtimeStore", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useRealtimeStore.getState().clear();
  });

  afterEach(() => {
    useRealtimeStore.getState().clear();
    vi.useRealTimers();
  });

  it("pushes lifecycle events immediately", () => {
    useRealtimeStore.getState().pushEvent({ type: "task.started", task_id: "t1" });
    expect(useRealtimeStore.getState().events.map((e) => e.type)).toEqual(["task.started"]);
  });

  it("coalesces a burst of progress deltas into one event per flush", () => {
    const { pushEvent } = useRealtimeStore.getState();
    let updates = 0;
    const unsubscribe = useRealtimeStore.subscribe(() => {
      updates += 1;
    });
    for (let i = 0; i < 200; i += 1) pushEvent(progress("t1", `w${i} `));
    expect(useRealtimeStore.getState().events).toHaveLength(0);
    vi.advanceTimersByTime(500);
    unsubscribe();
    const events = useRealtimeStore.getState().events;
    expect(updates).toBe(1);
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("task.progress");
    expect(events[0].message?.endsWith("w199 ")).toBe(true);
  });

  it("caps the merged progress message", () => {
    const { pushEvent } = useRealtimeStore.getState();
    pushEvent(progress("t1", "x".repeat(1000)));
    pushEvent(progress("t1", "fin"));
    vi.advanceTimersByTime(500);
    const message = useRealtimeStore.getState().events[0].message ?? "";
    expect(message.length).toBe(400);
    expect(message.endsWith("fin")).toBe(true);
  });

  it("keeps progress of different tasks separate", () => {
    const { pushEvent } = useRealtimeStore.getState();
    pushEvent(progress("t1", "uno"));
    pushEvent(progress("t2", "dos"));
    vi.advanceTimersByTime(500);
    const events = useRealtimeStore.getState().events;
    expect(events).toHaveLength(2);
    expect(events.map((e) => e.task_id).sort()).toEqual(["t1", "t2"]);
  });

  it("flushes pending progress before a lifecycle event so order is preserved", () => {
    const { pushEvent } = useRealtimeStore.getState();
    pushEvent(progress("t1", "casi listo"));
    pushEvent({ type: "task.completed", task_id: "t1" });
    const types = useRealtimeStore.getState().events.map((e) => e.type);
    expect(types).toEqual(["task.completed", "task.progress"]);
    vi.advanceTimersByTime(1000);
    expect(useRealtimeStore.getState().events).toHaveLength(2);
  });

  it("clear drops pending progress", () => {
    const { pushEvent, clear } = useRealtimeStore.getState();
    pushEvent(progress("t1", "pendiente"));
    clear();
    vi.advanceTimersByTime(1000);
    expect(useRealtimeStore.getState().events).toHaveLength(0);
  });

  it("never keeps more than 60 events", () => {
    const { pushEvent } = useRealtimeStore.getState();
    for (let i = 0; i < 100; i += 1) pushEvent({ type: "agent.status_changed", agent_id: `a${i}` });
    expect(useRealtimeStore.getState().events).toHaveLength(60);
  });
});
