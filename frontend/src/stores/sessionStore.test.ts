import { describe, it, expect, beforeEach } from "vitest";
import type { User } from "../types/api";
import { useSessionStore } from "../stores/sessionStore";

describe("sessionStore", () => {
  beforeEach(() => {
    useSessionStore.getState().logout();
    localStorage.clear();
  });

  it("starts with null token and user", () => {
    const state = useSessionStore.getState();
    expect(state.token).toBeNull();
    expect(state.user).toBeNull();
  });

  it("setSession keeps token in memory only", () => {
    const fakeUser = { id: "u1", username: "admin", role: "admin" } as unknown as User;
    useSessionStore.getState().setSession("tok-123", fakeUser);
    expect(useSessionStore.getState().token).toBe("tok-123");
    expect(useSessionStore.getState().user).toEqual(fakeUser);
    expect(localStorage.getItem("hermeshq.token")).toBeNull();
  });

  it("logout clears token and user", () => {
    useSessionStore.getState().setSession("tok-123", null);
    useSessionStore.getState().logout();
    expect(useSessionStore.getState().token).toBeNull();
    expect(useSessionStore.getState().user).toBeNull();
    expect(localStorage.getItem("hermeshq.token")).toBeNull();
  });

  it("setToken updates token without touching user", () => {
    const fakeUser = { id: "u1", username: "admin" } as unknown as User;
    useSessionStore.getState().setSession("tok-1", fakeUser);
    useSessionStore.getState().setToken("tok-2");
    expect(useSessionStore.getState().token).toBe("tok-2");
    expect(useSessionStore.getState().user).toEqual(fakeUser);
  });

  it("setUser updates user without touching token", () => {
    useSessionStore.getState().setSession("tok-1", null);
    const fakeUser = { id: "u1", username: "admin" } as unknown as User;
    useSessionStore.getState().setUser(fakeUser);
    expect(useSessionStore.getState().token).toBe("tok-1");
    expect(useSessionStore.getState().user).toEqual(fakeUser);
  });
});
