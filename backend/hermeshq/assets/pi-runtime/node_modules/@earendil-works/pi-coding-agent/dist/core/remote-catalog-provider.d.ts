import { type ModelType, type Provider } from "@earendil-works/pi-ai";
export declare const REMOTE_CATALOG_REFRESH_INTERVAL_MS: number;
/**
 * Model types this client can consume. Sent as `?types=` so the catalog server
 * returns the full-type shard instead of the chat-only one served to clients
 * that predate model types. A server that ignores the parameter still returns
 * the chat-only shard, which this client handles unchanged.
 */
export declare const REMOTE_CATALOG_MODEL_TYPES: readonly ModelType[];
/** Add a persisted pi.dev catalog overlay to a static built-in provider. */
export declare function withRemoteCatalog(provider: Provider, catalogBaseUrl?: string, localGeneratedAt?: number): Provider;
//# sourceMappingURL=remote-catalog-provider.d.ts.map