import type { Op } from "../delta/index.ts";
import type { Context } from "../types.ts";
export interface ReplicatedStateInternals {
    /** Atomically capture the immutable value and its matching publication sequence. */
    snapshot(): {
        readonly value: unknown;
        readonly sequence: number;
    };
    subscribe(listener: (ops: readonly Op[], sequence: number, context: Context) => void): () => void;
}
export declare function registerReplicatedStateInternals(value: object, internals: ReplicatedStateInternals): void;
export declare function getReplicatedStateInternals(value: unknown): ReplicatedStateInternals | undefined;
//# sourceMappingURL=state-internals.d.ts.map