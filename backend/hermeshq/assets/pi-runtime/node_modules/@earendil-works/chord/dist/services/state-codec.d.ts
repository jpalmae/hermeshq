import type { ServiceProviderUpdate, ServiceSubscriptionSnapshot } from "../types.ts";
import type { WireServiceProviderUpdate, WireServiceSubscriptionSnapshot } from "./wire.ts";
/** Stateful operation encoders for every replicated state in one service subscription. */
export interface ServiceStateEncoder {
    encodeSnapshot(snapshot: ServiceSubscriptionSnapshot): WireServiceSubscriptionSnapshot;
    encodeUpdate(update: ServiceProviderUpdate): WireServiceProviderUpdate;
}
/** Stateful operation decoders for every replicated state in one service subscription. */
export interface ServiceStateDecoder {
    decodeSnapshot(snapshot: WireServiceSubscriptionSnapshot): ServiceSubscriptionSnapshot;
    decodeUpdate(update: WireServiceProviderUpdate): ServiceProviderUpdate;
}
export declare function createServiceStateEncoder(): ServiceStateEncoder;
export declare function createServiceStateDecoder(): ServiceStateDecoder;
//# sourceMappingURL=state-codec.d.ts.map