import type { AttachedReplicatedState, Facet, FacetHost, FacetLoader, FacetOptions, MutableReplicatedState, RemoteServiceBinding, RemoteServiceBindingOptions, RemoteServiceContract, ReplicatedStateSource, ReplicatedStateSourceOptions, Service } from "./types.ts";
/** Create an active host for one complete set of facets. */
export declare function createFacetHost(options: FacetOptions): Promise<FacetHost>;
export declare function createStaticFacetLoader(facets: readonly Facet[]): FacetLoader;
export declare function combineFacetLoaders(loaders: readonly FacetLoader[]): FacetLoader;
export declare function defineFacet(facet: Facet): Facet;
export declare function defineService<T>(id: string, options: {
    readonly local: true;
}): Service<T>;
export declare function defineService<T>(id: string, ...options: [RemoteServiceContract<T>] extends [never] ? readonly [options: never] : readonly [options?: {
    readonly local?: false;
}]): Service<T>;
export declare function createRemoteServiceBinding(options: RemoteServiceBindingOptions): RemoteServiceBinding;
export declare function replicatedState<T>(source: ReplicatedStateSource<T>, options?: ReplicatedStateSourceOptions): AttachedReplicatedState<T>;
/**
 * Create authoritative state by taking immutable ownership of an alias-free strict-JSON root.
 * The caller must not mutate `initial` after this call.
 */
export declare function replicatedState<T extends object>(initial: T): MutableReplicatedState<T>;
//# sourceMappingURL=api.d.ts.map