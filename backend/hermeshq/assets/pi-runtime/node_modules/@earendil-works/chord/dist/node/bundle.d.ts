import { type FacetBundleManifest } from "./manifest.ts";
export type FacetBundlePlatform = "node" | "browser" | "neutral";
export interface BundleFacetsOptions {
    readonly plugin: {
        readonly id: string;
        readonly version?: string;
    };
    /** Opaque application-selected entry names mapped to TypeScript or JavaScript source files. */
    readonly entries: Readonly<Record<string, string>>;
    readonly outdir: string;
    readonly workingDirectory?: string;
    /** Additional package imports intentionally left for the loading application to resolve. */
    readonly external?: readonly string[];
    readonly sourceMap?: boolean;
    readonly minify?: boolean;
    readonly define?: Readonly<Record<string, string>>;
    readonly platform?: FacetBundlePlatform;
    readonly target?: string | readonly string[];
}
export interface BundleFacetsResult {
    readonly manifest: FacetBundleManifest;
    readonly manifestPath: string;
}
/** Bundle each opaque facet entry into an independent content-addressed CommonJS file. */
export declare function bundleFacets(options: BundleFacetsOptions): Promise<BundleFacetsResult>;
//# sourceMappingURL=bundle.d.ts.map