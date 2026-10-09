import type { JsonValue } from "../types.ts";
export type { JsonValue } from "../types.ts";
export type Seg = string | number;
export type Path = readonly Seg[];
export type NonEmptyPath = readonly [Seg, ...Seg[]];
/** A path inline, or an id assigned by the encoder on second use. */
export type PathRef<P extends Path = Path> = P | number;
/**
 * Tuples are the form — in memory, on the wire, on disk.
 *
 * `r` is the ONLY op that replaces a whole value. `s`/`d`/`a`/`t` cannot target
 * the root: the type forbids it. `p` and `m` may, because a tracked value can
 * itself be an array. Operation shape is not canonical: an array may be emptied by either
 * a replacement or a root splice.
 *
 * `Op` knows nothing about the path dictionary. Interning, id references and
 * omitted paths live in `WireOp` and exist only between `encode` and `decode`.
 */
export type Op = readonly ["r", JsonValue] | readonly ["s", NonEmptyPath, JsonValue] | readonly ["d", NonEmptyPath] | readonly ["a", NonEmptyPath, string] | readonly ["t", NonEmptyPath, number] | readonly ["p", Path, number, number, JsonValue[]]
/** Reorder an array in place: `new[i] = old[permutation[i]]`. */
 | readonly ["m", Path, number[]];
/**
 * What crosses a boundary. Adds two compressions and nothing else:
 *
 *   ["#", id, path]    defines an id, emitted on a path's SECOND use
 *   a numeric PathRef  references a previously defined id
 *   a shortened tuple  reuses the previous op's path; arity disambiguates
 *
 * ["r", value] carries no path, so it encodes to itself — which is why isBase
 * works unchanged on either vocabulary.
 */
export type WireOp = readonly ["r", JsonValue] | readonly ["s", PathRef<NonEmptyPath>, JsonValue] | readonly ["s", JsonValue] | readonly ["d", PathRef<NonEmptyPath>] | readonly ["d"] | readonly ["a", PathRef<NonEmptyPath>, string] | readonly ["a", string] | readonly ["t", PathRef<NonEmptyPath>, number] | readonly ["t", number] | readonly ["p", PathRef, number, number, JsonValue[]] | readonly ["p", number, number, JsonValue[]] | readonly ["m", PathRef, number[]] | readonly ["m", number[]] | readonly ["#", number, Path];
export declare const isReplace: (op: Op | WireOp) => boolean;
/**
 * A batch begins with a replacement. Flush guarantees `r` is at index 0 or absent,
 * so this is exact rather than a heuristic.
 */
export declare const isBase: (ops: readonly (Op | WireOp)[]) => boolean;
/**
 * Longest suffix of `a` that is a prefix of `b`. Probes with indexOf and verifies
 * exact substring equality, so the hot loops are native. A hand-written KMP is
 * asymptotically equivalent and much slower in practice.
 *
 * Always correct: the returned n satisfies a.slice(a.length - n) === b.slice(0, n).
 */
export declare function overlap(a: string, b: string, scan: number, probe?: number, maxCandidates?: number): number;
export { diffRevisions } from "./diff.ts";
export type { Draft } from "./draft.ts";
export { type Change, type Prepared, type Tracker, track } from "./tracker.ts";
/**
 * Segments that reach the prototype chain.
 *
 * `JSON.parse` is safe on its own — it makes `__proto__` an own property. What is
 * not safe is `parent[key] = value`, which is exactly what an applier does, and
 * paths are data: `["s", ["__proto__", "isAdmin"], true]` pollutes
 * `Object.prototype` for the whole process.
 *
 * Ops arrive from a facet, a plugin compartment, or a tool whose details may echo
 * model output, so none of it is trusted input.
 */
export declare const RESERVED_SEGMENTS: ReadonlySet<string>;
export declare class UnsafePathError extends Error {
    readonly segment: Seg;
    constructor(segment: Seg);
}
/**
 * Verb, arity and payload shape for a **decoded** op: paths inline, no `#`, no
 * short forms. `apply` uses this.
 *
 * Validating `Op` against the wire grammar would be laxer than the type: a
 * two-element `["s", value]` would pass, and `apply` would then read the value as
 * a path. Each vocabulary gets the validator that matches it.
 */
export declare function assertValidOp(op: unknown): asserts op is Op;
/** The same, for the wire grammar: ids and short forms are legal here. */
export declare function assertValidWireOp(op: unknown): asserts op is WireOp;
export declare function assertSafePath(path: Path): void;
export declare class PathError extends Error {
    readonly path: Path | number;
    constructor(path: Path | number);
}
/**
 * Apply ops to a plain mutable value. Returns the value, because `r` replaces it
 * outright and cannot be done in place.
 *
 * Takes decoded ops. Path ids and omitted paths are a wire concern — run
 * `decode` first if the ops came from a boundary.
 */
export declare function apply<T>(target: T | undefined, ops: readonly Op[]): T;
/** Apply one decoded operation batch without mutating the previous immutable value. */
export declare function applyImmutable<T>(target: T | undefined, ops: readonly Op[]): T;
/**
 * Apply decoded operation batches as one final-result-only replay.
 *
 * Containers copied for an earlier batch may be mutated while applying a later
 * batch, so this deliberately exposes no intermediate revisions.
 */
export declare function applyImmutableBatches<T>(target: T | undefined, batches: Iterable<readonly Op[]>): T;
export interface Encoder {
    encode(ops: readonly Op[]): WireOp[];
}
/**
 * Intern on SECOND use. A definition costs more than the path it replaces, so
 * interning on first use loses on the many paths written exactly once.
 */
export declare function encoder(): Encoder;
export interface Decoder {
    decode(wire: readonly WireOp[]): Op[];
}
export declare function decoder(): Decoder;
//# sourceMappingURL=index.d.ts.map