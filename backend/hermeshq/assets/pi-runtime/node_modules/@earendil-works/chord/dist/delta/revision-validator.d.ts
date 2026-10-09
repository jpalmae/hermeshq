import type { JsonValue } from "../types.ts";
/** Validates immutable replica revisions while skipping containers validated in earlier revisions. */
export declare class JsonRevisionValidator {
    #private;
    validate<T extends JsonValue>(value: T): T;
}
//# sourceMappingURL=revision-validator.d.ts.map