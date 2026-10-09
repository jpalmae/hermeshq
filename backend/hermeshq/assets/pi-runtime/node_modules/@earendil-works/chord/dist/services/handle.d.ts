interface ResolvedValue {
    readonly value: unknown;
    readonly receiver: object;
}
/** Host-owned mutable target with consumer-owned guarded views. */
export declare class ServiceSlot {
    #private;
    constructor(serviceId: string, wrapObjects: boolean);
    view<T>(assertAccess: () => void): T;
    bind(implementation: object): void;
    unbind(): void;
    resolve(property: PropertyKey, assertAccess: () => void): ResolvedValue;
}
export {};
//# sourceMappingURL=handle.d.ts.map