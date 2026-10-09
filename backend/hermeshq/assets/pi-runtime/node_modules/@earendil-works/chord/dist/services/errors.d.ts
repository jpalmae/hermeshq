export declare const REMOTE_SERVICE_ERROR_CODES: readonly ["service_not_allowed", "service_not_found", "service_mode_mismatch", "service_member_not_found", "service_member_mismatch", "service_instance_not_found", "service_stale_instance", "service_invalid_value"];
export type RemoteServiceErrorCode = (typeof REMOTE_SERVICE_ERROR_CODES)[number];
export declare function isRemoteServiceErrorCode(value: unknown): value is RemoteServiceErrorCode;
export declare class RemoteServiceError extends Error {
    readonly code: RemoteServiceErrorCode;
    constructor(code: RemoteServiceErrorCode, message: string);
}
//# sourceMappingURL=errors.d.ts.map