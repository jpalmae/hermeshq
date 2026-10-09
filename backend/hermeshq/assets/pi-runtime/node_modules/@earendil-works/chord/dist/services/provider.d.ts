import type { Context, JsonValue, RemoteServiceContract, Service, ServiceCall, ServiceCatalogueEntry, ServiceMode, ServiceProviderUpdate, ServiceSubscription } from "../types.ts";
interface ServiceProviderDefinition {
    readonly service: {
        readonly id: string;
        readonly local?: boolean;
    };
    readonly mode: ServiceMode;
}
export type ServiceUpdatePublisher = (subscriptionId: string, update: ServiceProviderUpdate, context: Context) => void | Promise<void>;
/** Hosts one provider for one remote consumer and owns that consumer's subscriptions. */
export interface RemoteServiceEndpoint {
    invoke(call: ServiceCall, publish: ServiceUpdatePublisher, context: Context): Promise<JsonValue | undefined>;
    dispose(): void;
}
export declare class RemoteServiceProvider {
    #private;
    constructor(entries: readonly (ServiceProviderDefinition | {
        readonly id: string;
    })[]);
    get catalogue(): readonly ServiceCatalogueEntry[];
    provide<T>(service: Service<T>, implementation: NoInfer<RemoteServiceContract<T>>): void;
    /** Disconnect one singleton while preserving active subscriptions and remote facades. */
    withdraw<T>(service: Service<T>): void;
    /** Check a singleton replacement without changing the active provider. */
    validateReplacement<T>(service: Service<T>, implementation: NoInfer<RemoteServiceContract<T>>): void;
    /** Replace one singleton without making its stable remote facade unavailable. */
    replace<T>(service: Service<T>, implementation: NoInfer<RemoteServiceContract<T>>): void;
    use<T>(service: Service<T>): T;
    spawn<T>(service: Service<T>, key: string, implementation: NoInfer<RemoteServiceContract<T>>): () => void;
    invoke(call: ServiceCall, context: Context): Promise<JsonValue | undefined>;
    subscribe(serviceId: string, mode: ServiceMode, listener: (update: ServiceProviderUpdate, context: Context) => void): ServiceSubscription;
    dispose(): void;
}
export declare function createRemoteServiceEndpoint(provider: RemoteServiceProvider): RemoteServiceEndpoint;
export declare function validateRemoteServiceImplementation(serviceId: string, implementation: unknown): void;
export {};
//# sourceMappingURL=provider.d.ts.map