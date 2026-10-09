import type { Op } from "./index.ts";
/** Apply self-produced trusted operations while copying each touched container once. */
export declare function applyImmutableTrusted<T>(target: T, operations: readonly Op[]): T;
//# sourceMappingURL=apply-immutable-trusted.d.ts.map