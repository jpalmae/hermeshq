import { type BundleFacetsResult } from "./bundle.ts";
export interface BundleFacetPackageOptions {
    /** Plugin package directory or its package.json path. */
    readonly packagePath: string;
    readonly outdir: string;
    /** Application conventions applied when the corresponding source file exists. */
    readonly defaultFacets?: Readonly<Record<string, string>>;
}
export interface BundleFacetPackageResult extends BundleFacetsResult {
    readonly packageDirectory: string;
    readonly packageJsonPath: string;
}
/** Build a plugin package using package.json metadata and application-provided facet conventions. */
export declare function bundleFacetPackage(options: BundleFacetPackageOptions): Promise<BundleFacetPackageResult>;
//# sourceMappingURL=package.d.ts.map