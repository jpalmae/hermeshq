import { useEffect } from "react";

import { resolveWsRoot } from "../lib/apiBase";
import { useRealtimeStore } from "../stores/realtimeStore";
import { useSessionStore } from "../stores/sessionStore";

function resolveWsUrl(token: string) {
  return `${resolveWsRoot()}/ws/stream?token=${encodeURIComponent(token)}`;
}

export function useWebSocket() {
  const token = useSessionStore((state) => state.token);
  const pushEvent = useRealtimeStore((state) => state.pushEvent);

  useEffect(() => {
    if (!token) {
      return;
    }
    const socket = new WebSocket(resolveWsUrl(token));
    socket.onmessage = (event) => {
      try {
        pushEvent(JSON.parse(event.data));
      } catch {
        // Ignore malformed event payloads from early dev runtimes.
      }
    };
    return () => {
      socket.close();
    };
  }, [token, pushEvent]);
}
