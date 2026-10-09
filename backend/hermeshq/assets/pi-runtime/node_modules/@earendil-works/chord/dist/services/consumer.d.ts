import type { Context, RemoteServiceBinding, RemoteServiceBindingOptions, Service } from "../types.ts";
export declare class RemoteServiceBindingImpl implements RemoteServiceBinding {
    #private;
    constructor(options: RemoteServiceBindingOptions);
    use<T>(service: Service<T>): T;
    observe<T>(service: Service<T>, handler: (service: T, context: Context) => void | Promise<void>): () => void;
    ready(context: Context): Promise<void>;
    rebind(bound: boolean, context: Context): Promise<void>;
    dispose(context: Context): Promise<void>;
}
//# sourceMappingURL=consumer.d.ts.map