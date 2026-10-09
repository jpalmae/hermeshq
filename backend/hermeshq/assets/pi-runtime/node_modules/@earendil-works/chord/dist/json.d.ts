import type { JsonValue } from "./types.ts";
export type CopyJsonOptions = {
    /** Omit undefined object properties while preserving strict array semantics. */
    readonly omitUndefinedProperties?: boolean;
};
/** Copy a value into an alias-free strict-JSON tree owned by the caller. */
export declare function copyJson(value: unknown, options?: CopyJsonOptions): JsonValue;
/** Return whether a value is finite strict JSON with plain objects and no cycles. */
export declare function isJsonValue(value: unknown): value is JsonValue;
//# sourceMappingURL=json.d.ts.map