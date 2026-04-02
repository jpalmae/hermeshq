import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { useEffect, useMemo, useRef, useState } from "react";

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

function resolvePtyUrl(agentId: string) {
  const apiBase = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api";
  const root = apiBase.replace(/\/api$/, "");
  return `${root.replace(/^http/, "ws")}/ws/pty/${agentId}`;
}

export function AgentTerminal({ agentId, mode }: { agentId: string; mode: string }) {
  const [connected, setConnected] = useState(false);
  const socketRef = useRef<WebSocket | null>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const readOnly = useMemo(() => mode === "hybrid" || mode === "interactive", [mode]);

  useEffect(() => {
    if (mode === "headless") {
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

    const socket = new WebSocket(resolvePtyUrl(agentId));
    socketRef.current = socket;
    socket.onopen = () => {
      setConnected(true);
      fitAddon.fit();
      socket.send(
        JSON.stringify({
          type: "resize",
          cols: terminal.cols,
          rows: terminal.rows,
        }),
      );
    };
    socket.onclose = () => {
      setConnected(false);
      terminal.writeln("");
      terminal.writeln("[PTY CLOSED]");
    };
    socket.onmessage = (event) => {
      const payload = JSON.parse(event.data) as { type: string; data?: string };
      if (payload.type === "output" && payload.data) {
        terminal.write(decodeChunk(payload.data));
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
      resizeObserver.disconnect();
      onDataDispose.dispose();
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "detach" }));
      }
      socket.close();
      terminal.dispose();
      terminalRef.current = null;
      fitAddonRef.current = null;
    };
  }, [agentId, mode]);

  return (
    <section className="panel-frame p-6">
      <div className="flex items-center justify-between gap-4 border-b border-[var(--border)] pb-4">
        <div>
          <p className="panel-label">Terminal</p>
          <p className="mt-2 text-lg text-[var(--text-display)]">
            {connected ? "Hermes TUI attached" : "Hermes TUI offline"}
          </p>
          <p className="mt-2 max-w-[44rem] text-sm leading-6 text-[var(--text-secondary)]">
            This is the real Hermes terminal for this agent installation. It runs with the agent&apos;s own{" "}
            <code>HERMES_HOME</code>, config, sessions and installed skills.
          </p>
        </div>
        <p className={`panel-label ${connected ? "text-[var(--success)]" : "text-[var(--warning)]"}`}>
          {connected ? "[LIVE]" : "[IDLE]"}
        </p>
      </div>

      <div className="mt-4">
        {readOnly ? (
          <div ref={containerRef} className="terminal-shell h-[28rem] overflow-hidden border border-[var(--border)] bg-black" />
        ) : (
          <div className="border border-[var(--border)] bg-black/40 p-4 font-mono text-sm text-[var(--text-secondary)]">
            Terminal unavailable in this mode.
          </div>
        )}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <button
          className="panel-button-secondary"
          type="button"
          onClick={() => terminalRef.current?.clear()}
          disabled={!readOnly}
        >
          Clear
        </button>
        <p className="panel-inline-status">
          {connected ? "[LIVE] running hermes in the agent installation" : "[BOOT] opening Hermes terminal"}
        </p>
      </div>
    </section>
  );
}
