import type { JsonValue } from "../types.ts";
import { type Op } from "./index.ts";
/** Compute a compact operation batch from two immutable JSON revisions. */
export declare function diffRevisions(before: JsonValue, after: JsonValue): Op[];
//# sourceMappingURL=diff.d.ts.map