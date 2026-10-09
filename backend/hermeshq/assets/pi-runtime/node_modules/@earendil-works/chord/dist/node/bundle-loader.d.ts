import type { FacetLoader } from "../types.ts";
import { type FacetBundleArtifact, type FacetBundleManifest } from "./manifest.ts";
export type FacetBundleExternalResolver = (specifier: string) => string | URL | undefined;
export interface FacetBundleLoaderOptions {
    readonly manifestPath: string | URL;
    readonly entry: string;
    /** Verify the entry's SHA-256 integrity before evaluating it. Defaults to true. */
    readonly verifyIntegrity?: boolean;
    /** Resolve host-provided external imports when the bundle is outside the host's package tree. */
    readonly resolveExternal?: FacetBundleExternalResolver;
}
export interface FacetBundleArtifactLoaderOptions {
    readonly artifact: unknown;
    /** Resolve host-provided external imports against the receiving application. */
    readonly resolveExternal?: FacetBundleExternalResolver;
    /** Parent directory for materialized module generations. Defaults to the operating system temp directory. */
    readonly temporaryDirectory?: string;
}
/** Read and validate a versioned facet bundle manifest. */
export declare function readFacetBundleManifest(path: string | URL): Promise<FacetBundleManifest>;
/** Read and verify one transportable entry from a facet bundle on disk. */
export declare function readFacetBundleArtifact(options: {
    readonly manifestPath: string | URL;
    readonly entry: string;
}): Promise<FacetBundleArtifact>;
/** Materialize a transported artifact and create a fresh VM-compiled CommonJS generation for each load. */
export declare function createFacetBundleArtifactLoader(options: FacetBundleArtifactLoaderOptions): FacetLoader;
/** Create a reusable loader for one opaque entry in a facet bundle manifest. */
export declare function createFacetBundleLoader(options: FacetBundleLoaderOptions): FacetLoader;
//# sourceMappingURL=bundle-loader.d.ts.map