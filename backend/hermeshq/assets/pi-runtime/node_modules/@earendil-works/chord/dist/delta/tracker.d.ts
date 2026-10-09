import type { JsonValue } from "../types.ts";
import type { Draft } from "./draft.ts";
import { type Op } from "./index.ts";
export type { Draft, JsonValue, Op };
export interface Prepared<T extends object> {
    readonly base: T;
    readonly value: T;
    readonly ops: readonly Op[];
    readonly baseRevision: number;
    abort(): void;
}
export interface Change<T extends object> {
    readonly state: Draft<T>;
    prepare(): Prepared<T>;
    abort(): void;
}
export interface Tracker<T extends object> {
    readonly value: T;
    readonly revision: number;
    beginChange(): Change<T>;
    prepareReplace(value: T): Prepared<T>;
    adopt(prepared: Prepared<T>): void;
}
/** Take immutable ownership of an alias-free strict-JSON root in O(1). */
export declare function track<T extends object>(initial: T): Tracker<T>;
//# sourceMappingURL=tracker.d.ts.map