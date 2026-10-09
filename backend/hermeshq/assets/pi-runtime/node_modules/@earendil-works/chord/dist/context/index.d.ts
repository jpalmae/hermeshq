import type { Context, ContextKey } from "../types.ts";
export declare const BACKGROUND_CONTEXT: Context;
export declare const TODO_CONTEXT: Context;
export declare function createContextKey<T>(description: string): ContextKey<T>;
/** Derive a context containing one additional or replaced value. */
export declare function withContextValue<T>(key: ContextKey<T>, value: T, parent: Context): Context;
/**
 * Derive a context cancelled by either the parent signal or the supplied signal.
 * The parent context remains unchanged.
 */
export declare function withAbortSignal(signal: AbortSignal, context: Context): Context;
/** Derive a context retaining all values except caller cancellation. Intended for mandatory cleanup only. */
export declare function withoutAbortSignal(context: Context): Context;
/** Derive an independently cancellable child context. */
export declare function withCancel(context: Context): {
    readonly context: Context;
    readonly cancel: (reason?: unknown) => void;
};
/**
 * Observe a promise until it settles or the invocation is cancelled.
 * Cancellation rejects only this waiter; it does not cancel the underlying promise.
 */
export declare function awaitWithContext<T>(promise: Promise<T>, context: Context): Promise<T>;
//# sourceMappingURL=index.d.ts.map