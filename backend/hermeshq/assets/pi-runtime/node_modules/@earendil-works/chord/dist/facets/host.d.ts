import { RemoteServiceProvider } from "../services/provider.ts";
import type { Facet, FacetOptions } from "../types.ts";
/** Private lifecycle and dependency kernel behind the atomic host entry point. */
export declare class FacetKernel {
    #private;
    constructor(options: FacetOptions);
    get provider(): RemoteServiceProvider;
    activate(): Promise<void>;
    reload(facets: readonly Facet[]): Promise<void>;
    dispose(): Promise<void>;
}
//# sourceMappingURL=host.d.ts.map