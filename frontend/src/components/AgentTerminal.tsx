import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { useEffect, useMemo, useRef, useState } from "react";

import { resolveWsRoot } from "../lib/apiBase";
import { useI18n } from "../lib/i18n";
import { useSessionStore } from "../stores/sessionStore";

function decodeChunk(data: string) {
  const binary = window.atob(data);
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

function encodeChunk(data: string) {
  const bytes = new TextEncoder().encode(data);
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return window.btoa(binary);
}

function resolvePtyUrl(agentId: string, token: string) {
  return `${resolveWsRoot()}/ws/pty/${agentId}?token=${encodeURIComponent(token)}`;
}

export function AgentTerminal({
  agentId,
  mode,
  runtimeProfile,
}: {
  agentId: string;
  mode: string;
  runtimeProfile?: string;
}) {
  const { t } = useI18n();
  const token = useSessionStore((state) => state.token);
  const [connected, setConnected] = useState(false);
  const [connectionMessage, setConnectionMessage] = useState<string | null>(null);
  const [reconnectNonce, setReconnectNonce] = useState(0);
  const [isFloating, setIsFloating] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const socketRef = useRef<WebSocket | null>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const shouldReconnectRef = useRef(true);
  const shouldAutoScrollRef = useRef(true);
  const terminalDisabledByProfile = runtimeProfile === "standard";
  const readOnly = useMemo(() => mode === "hybrid" || mode === "interactive", [mode]);

  const isTerminalNearBottom = () => {
    const buffer = terminalRef.current?.buffer.active;
    if (!buffer) {
      return true;
    }
    return buffer.baseY - buffer.viewportY <= 2;
  };

  const scrollTerminalToBottom = (force = false) => {
    if (!force && !shouldAutoScrollRef.current) {
      return;
    }
    window.requestAnimationFrame(() => {
      terminalRef.current?.scrollToBottom();
    });
  };

  useEffect(() => {
    if (mode === "headless" || terminalDisabledByProfile) {
      return;
    }
    if (!containerRef.current) {
      return;
    }

    const terminal = new Terminal({
      fontFamily: '"Space Mono", monospace',
      fontSize: 13,
      lineHeight: 1.3,
      cursorBlink: true,
      theme: {
        background: "#080808",
        foreground: "#e8e8e8",
        cursor: "#ffffff",
        cursorAccent: "#000000",
        selectionBackground: "rgba(255,255,255,0.18)",
        black: "#000000",
        brightBlack: "#666666",
        red: "#d71921",
        brightRed: "#ff4d55",
        green: "#4a9e5c",
        brightGreen: "#7fd78f",
        yellow: "#d4a843",
        brightYellow: "#f0c86b",
        blue: "#5b9bf6",
        brightBlue: "#8fbdff",
        magenta: "#999999",
        brightMagenta: "#c9c9c9",
        cyan: "#b8b8b8",
        brightCyan: "#ffffff",
        white: "#cccccc",
        brightWhite: "#ffffff",
      },
    });
    const fitAddon = new FitAddon();
    terminal.loadAddon(fitAddon);
    terminal.open(containerRef.current);
    fitAddon.fit();
    terminalRef.current = terminal;
    fitAddonRef.current = fitAddon;

    if (!token) {
      terminal.writeln("[SESSION REQUIRED]");
      setConnectionMessage(t("agent.sessionRequired"));
      return () => {
        terminal.dispose();
        terminalRef.current = null;
        fitAddonRef.current = null;
      };
    }

    shouldReconnectRef.current = true;
    setConnectionMessage(null);
    const socket = new WebSocket(resolvePtyUrl(agentId, token));
    socketRef.current = socket;
    socket.onopen = () => {
      setConnected(true);
      setConnectionMessage(null);
      fitAddon.fit();
      shouldAutoScrollRef.current = true;
      scrollTerminalToBottom(true);
      socket.send(
        JSON.stringify({
          type: "resize",
          cols: terminal.cols,
          rows: terminal.rows,
        }),
      );
    };
    socket.onclose = (event) => {
      setConnected(false);
      terminal.writeln("");
      terminal.writeln("[PTY CLOSED]");
      const shouldRetry = shouldReconnectRef.current && ![4400, 4401, 4403, 4404].includes(event.code);
      if ([4401, 4403].includes(event.code)) {
        setConnectionMessage(t("agent.terminalAccessDenied"));
      } else if (event.code === 4404) {
        setConnectionMessage(t("agent.terminalNotFound"));
      } else if (shouldRetry) {
        setConnectionMessage(t("agent.ptyLost"));
        reconnectTimerRef.current = window.setTimeout(() => {
          setReconnectNonce((value) => value + 1);
        }, 1200);
      } else {
        setConnectionMessage(t("agent.ptyClosed"));
      }
    };
    socket.onmessage = (event) => {
      const payload = JSON.parse(event.data) as { type: string; data?: string };
      if (payload.type === "output" && payload.data) {
        shouldAutoScrollRef.current = isTerminalNearBottom();
        terminal.write(decodeChunk(payload.data));
        scrollTerminalToBottom();
      }
    };

    const onDataDispose = terminal.onData((data) => {
      if (!socketRef.current || socketRef.current.readyState !== WebSocket.OPEN) {
        return;
      }
      socketRef.current.send(
        JSON.stringify({
          type: "input",
          data: encodeChunk(data),
        }),
      );
    });

    const onScrollDispose = terminal.onScroll(() => {
      shouldAutoScrollRef.current = isTerminalNearBottom();
    });

    const resizeObserver = new ResizeObserver(() => {
      fitAddon.fit();
      if (socketRef.current?.readyState === WebSocket.OPEN) {
        socketRef.current.send(
          JSON.stringify({
            type: "resize",
            cols: terminal.cols,
            rows: terminal.rows,
          }),
        );
      }
    });
    resizeObserver.observe(containerRef.current);

    return () => {
      shouldReconnectRef.current = false;
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      resizeObserver.disconnect();
      onScrollDispose.dispose();
      onDataDispose.dispose();
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "detach" }));
      }
      socket.close();
      terminal.dispose();
      terminalRef.current = null;
      fitAddonRef.current = null;
    };
  }, [agentId, mode, token, reconnectNonce, terminalDisabledByProfile]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      fitAddonRef.current?.fit();
      const terminal = terminalRef.current;
      if (shouldAutoScrollRef.current) {
        terminal?.scrollToBottom();
      }
      if (terminal && socketRef.current?.readyState === WebSocket.OPEN) {
        socketRef.current.send(
          JSON.stringify({
            type: "resize",
            cols: terminal.cols,
            rows: terminal.rows,
          }),
        );
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [isFloating, isFullscreen]);

  const shellClassName = isFloating
    ? isFullscreen
      ? "fixed inset-4 z-[80] flex flex-col"
      : "fixed left-1/2 top-1/2 z-[80] flex h-[82vh] w-[min(96vw,1400px)] -translate-x-1/2 -translate-y-1/2 flex-col"
    : "";

  const panelClassName = `panel-frame p-6 ${isFloating ? "flex h-full min-h-0 flex-col shadow-2xl" : ""}`;
  const terminalBodyClassName = isFloating ? "mt-4 min-h-0 flex-1" : "mt-4";
  const terminalViewportClassName = isFloating
    ? "terminal-shell h-full overflow-hidden border border-[var(--border)]"
    : "terminal-shell h-[28rem] overflow-hidden border border-[var(--border)]";

  const terminalSection = (
    <section className={`${panelClassName} ${shellClassName}`.trim()}>
      <div className="flex items-center justify-between gap-4 border-b border-[var(--border)] pb-4">
        <div>
          <p className="panel-label">{t("agent.terminal")}</p>
          <p className="mt-2 text-lg text-[var(--text-display)]">
            {terminalDisabledByProfile
              ? t("agent.tuiDisabledByProfile")
              : connected
                ? t("agent.tuiAttached")
                : t("agent.tuiOffline")}
          </p>
          <p className="mt-2 max-w-[44rem] text-sm leading-6 text-[var(--text-secondary)]">
            {terminalDisabledByProfile ? t("agent.terminalProfileCopy") : t("agent.terminalCopy")}
          </p>
        </div>
        <p className={`panel-label ${connected ? "text-[var(--success)]" : "text-[var(--warning)]"}`}>
          {terminalDisabledByProfile ? t("agent.disabled") : connected ? t("agent.live") : t("agent.idle")}
        </p>
      </div>

      <div className={terminalBodyClassName}>
        {terminalDisabledByProfile ? (
          <div className="border border-[var(--border)] bg-[var(--surface-raised)] p-4 font-mono text-sm text-[var(--text-secondary)]">
            {t("agent.terminalDisabledByProfile")}
          </div>
        ) : readOnly ? (
          <div ref={containerRef} className={terminalViewportClassName} />
        ) : (
          <div className="border border-[var(--border)] bg-[var(--surface-raised)] p-4 font-mono text-sm text-[var(--text-secondary)]">
            {t("agent.terminalUnavailable")}
          </div>
        )}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <button
          className="panel-button-secondary"
          type="button"
          onClick={() => terminalRef.current?.clear()}
          disabled={!readOnly || terminalDisabledByProfile}
        >
          {t("agent.clear")}
        </button>
        <button
          className="panel-button-secondary"
          type="button"
          onClick={() => {
            setConnectionMessage(t("agent.reattaching"));
            setReconnectNonce((value) => value + 1);
          }}
          disabled={!readOnly || terminalDisabledByProfile}
        >
          {t("agent.reconnect")}
        </button>
        <button
          className="panel-button-secondary"
          type="button"
          onClick={() => {
            if (isFloating) {
              setIsFullscreen(false);
            }
            setIsFloating((value) => !value);
          }}
          disabled={!readOnly || terminalDisabledByProfile}
        >
          {isFloating ? t("agent.dock") : t("agent.float")}
        </button>
        <button
          className="panel-button-secondary"
          type="button"
          onClick={() => {
            if (!isFloating) {
              setIsFloating(true);
              setIsFullscreen(true);
              return;
            }
            setIsFullscreen((value) => !value);
          }}
          disabled={!readOnly || terminalDisabledByProfile}
        >
          {isFullscreen ? t("agent.windowed") : t("agent.fullscreen")}
        </button>
        <p className="panel-inline-status">
          {terminalDisabledByProfile
            ? t("agent.terminalDisabledByProfile")
            : connected
            ? t("agent.liveHermes")
            : connectionMessage || t("agent.bootTerminal")}
        </p>
      </div>
    </section>
  );

  return (
    <>
      {isFloating ? (
        <div
          className="fixed inset-0 z-[70] bg-[var(--overlay)]"
          onClick={() => {
            setIsFullscreen(false);
            setIsFloating(false);
          }}
        />
      ) : null}
      {terminalSection}
    </>
  );
}
