import type { CodemodeExecuteOptions, CodemodeResult, CodemodeSandboxOptions, CodemodeTool } from "../types.ts";
/**
 * Runs JavaScript in a QuickJS VM (a separate wasm instance) inside a worker
 * thread. The script sees `tools.<name>(args)` for every registered tool, `ALL_TOOLS`,
 * the output helpers `text`, `image`, `exit`, and `console.*`, `store`/`load`, and the
 * configured globals; nothing else (no timers, `fetch`, `process`, `require`, modules).
 *
 * Each `execute()` gets its own worker and VM; the sandbox only holds the tool
 * table and defaults. `close()` aborts in-flight executions.
 */
export declare class CodemodeSandbox {
    private readonly toolsByName;
    private readonly globalsByName;
    private readonly timeoutMs;
    private readonly memoryLimitBytes;
    private readonly wasm;
    private readonly workerUrl;
    private readonly running;
    private closed;
    constructor(options?: CodemodeSandboxOptions);
    /** Throws if a tool with the same name is already registered. */
    registerTool(tool: CodemodeTool): void;
    unregisterTool(name: string): boolean;
    get tools(): CodemodeTool[];
    get globals(): CodemodeTool[];
    /**
     * `code` is an async function body: `return` and top-level `await` work.
     * Never rejects for script failures; those come back as `{ ok: false }`.
     * The script can use `store(key, value)` and `load(key)` on `options.store`.
     */
    execute(code: string, options?: CodemodeExecuteOptions): Promise<CodemodeResult>;
    /** Aborts in-flight executions (they resolve with `kind: "aborted"`) and rejects new ones. */
    close(): Promise<void>;
}
//# sourceMappingURL=host.d.ts.map