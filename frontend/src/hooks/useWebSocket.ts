import { useCallback, useEffect, useRef, useState } from "react";

import { resolveWsRoot } from "../lib/apiBase";
import { useRealtimeStore } from "../stores/realtimeStore";
import { useSessionStore } from "../stores/sessionStore";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ConnectionState =
  | "connecting"
  | "connected"
  | "disconnected"
  | "reconnecting";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Delay sequence (ms) for exponential back-off: 1s → 2s → 4s → 8s → 16s → 30s max */
const BACKOFF_DELAYS = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000];

/** How often we send a ping to keep the connection alive. */
const HEARTBEAT_INTERVAL_MS = 30_000;

/** If no pong (or any message) arrives within this window, consider the connection dead. */
const HEARTBEAT_TIMEOUT_MS = 10_000;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function resolveWsUrl(): string {
  return `${resolveWsRoot()}/ws/stream`;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useWebSocket() {
  const token = useSessionStore((state) => state.token);
  const pushEvent = useRealtimeStore((state) => state.pushEvent);

  const [connectionState, setConnectionState] =
    useState<ConnectionState>("disconnected");

  // --- Mutable refs that outlive re-renders but should NOT trigger them ---
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const heartbeatTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const heartbeatTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const backoffIndexRef = useRef(0);
  const intentionalCloseRef = useRef(false);

  // -----------------------------------------------------------------------
  // Cleanup helper – clears *all* pending timers and the socket.
  // -----------------------------------------------------------------------
  const cleanup = useCallback(() => {
    // Timers
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (heartbeatTimerRef.current !== null) {
      clearInterval(heartbeatTimerRef.current);
      heartbeatTimerRef.current = null;
    }
    if (heartbeatTimeoutRef.current !== null) {
      clearTimeout(heartbeatTimeoutRef.current);
      heartbeatTimeoutRef.current = null;
    }

    // Socket
    if (socketRef.current) {
      // Prevent the onclose handler from triggering another reconnect.
      intentionalCloseRef.current = true;
      socketRef.current.close();
      socketRef.current = null;
    }
  }, []);

  // -----------------------------------------------------------------------
  // Heartbeat helpers
  // -----------------------------------------------------------------------
  const resetHeartbeatTimeout = useCallback(() => {
    if (heartbeatTimeoutRef.current !== null) {
      clearTimeout(heartbeatTimeoutRef.current);
    }
    heartbeatTimeoutRef.current = setTimeout(() => {
      // No message received within the timeout window – connection is stale.
      console.warn("[WS] Heartbeat timeout – closing stale connection");
      cleanup();
      setConnectionState("reconnecting");
      scheduleReconnect();
    }, HEARTBEAT_TIMEOUT_MS);
  }, [cleanup]);

  const startHeartbeat = useCallback(() => {
    // Clear any previous interval
    if (heartbeatTimerRef.current !== null) {
      clearInterval(heartbeatTimerRef.current);
    }

    resetHeartbeatTimeout();

    heartbeatTimerRef.current = setInterval(() => {
      const ws = socketRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "ping" }));
      }
      resetHeartbeatTimeout();
    }, HEARTBEAT_INTERVAL_MS);
  }, [resetHeartbeatTimeout]);

  const stopHeartbeat = useCallback(() => {
    if (heartbeatTimerRef.current !== null) {
      clearInterval(heartbeatTimerRef.current);
      heartbeatTimerRef.current = null;
    }
    if (heartbeatTimeoutRef.current !== null) {
      clearTimeout(heartbeatTimeoutRef.current);
      heartbeatTimeoutRef.current = null;
    }
  }, []);

  // -----------------------------------------------------------------------
  // Schedule a reconnect with exponential back-off
  // -----------------------------------------------------------------------
  // We store `connect` in a ref so that `scheduleReconnect` can reference it
  // without needing it as a dependency (which would create a circular ref).
  const connectRef = useRef<() => void>(() => {});

  const scheduleReconnect = useCallback(() => {
    const delay = BACKOFF_DELAYS[Math.min(backoffIndexRef.current, BACKOFF_DELAYS.length - 1)];
    backoffIndexRef.current += 1;

    console.info(`[WS] Reconnecting in ${delay / 1_000}s …`);

    reconnectTimerRef.current = setTimeout(() => {
      reconnectTimerRef.current = null;
      connectRef.current();
    }, delay);
  }, []);

  // -----------------------------------------------------------------------
  // Connect
  // -----------------------------------------------------------------------
  const connect = useCallback(() => {
    // Close any existing socket first
    if (socketRef.current) {
      intentionalCloseRef.current = true;
      socketRef.current.close();
      socketRef.current = null;
    }

    if (!token) {
      setConnectionState("disconnected");
      return;
    }

    setConnectionState(backoffIndexRef.current > 0 ? "reconnecting" : "connecting");

    const ws = new WebSocket(resolveWsUrl());
    socketRef.current = ws;

    // ------ open ------
    ws.onopen = () => {
      // Send authentication as the first message instead of a query param.
      ws.send(JSON.stringify({ type: "auth", token }));

      // Reset back-off on successful connection.
      backoffIndexRef.current = 0;
      setConnectionState("connected");

      // Start heartbeat keep-alive.
      startHeartbeat();
    };

    // ------ message ------
    ws.onmessage = (event) => {
      // Any inbound message proves the connection is alive.
      resetHeartbeatTimeout();

      try {
        const data = JSON.parse(event.data);

        // Handle pong responses from the server (heartbeat).
        if (data?.type === "pong") {
          return;
        }

        pushEvent(data);
      } catch {
        // Ignore malformed event payloads.
      }
    };

    // ------ close ------
    ws.onclose = () => {
      stopHeartbeat();

      if (intentionalCloseRef.current) {
        intentionalCloseRef.current = false;
        return;
      }

      setConnectionState("reconnecting");
      scheduleReconnect();
    };

    // ------ error ------
    ws.onerror = () => {
      // The `onclose` handler will fire after an error, which handles reconnect.
    };
  }, [token, pushEvent, startHeartbeat, stopHeartbeat, resetHeartbeatTimeout, scheduleReconnect]);

  // Keep the ref in sync so `scheduleReconnect` always calls the latest `connect`.
  connectRef.current = connect;

  // -----------------------------------------------------------------------
  // Effect – connect when we have a token; clean up on unmount / token change.
  // -----------------------------------------------------------------------
  useEffect(() => {
    connect();

    return () => {
      intentionalCloseRef.current = true;
      cleanup();
      setConnectionState("disconnected");
    };
    // We intentionally only depend on `token` so that:
    //   – a token change reconnects,
    //   – store reference changes (pushEvent) do NOT tear down the socket.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  return { connectionState };
}
