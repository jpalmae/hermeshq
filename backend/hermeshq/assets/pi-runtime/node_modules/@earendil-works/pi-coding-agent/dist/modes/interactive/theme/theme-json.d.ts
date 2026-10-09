/**
 * Theme JSON validation, kept out of `theme.ts` on purpose.
 *
 * Validating user-authored theme files needs typebox, which costs ~17 MB of module graph to import.
 * Palette lookup does not, so a presentation that only uses built-in themes should never pay for it.
 * `interactive-mode.ts` installs this validator; anything that does not simply skips validation, as
 * built-in themes already do.
 */
import { type Static, Type } from "typebox";
declare const ColorValueSchema: Type.TUnion<[Type.TString, Type.TInteger]>;
declare const ThemeJsonSchema: Type.TObject<{
    $schema: Type.TOptional<Type.TString>;
    name: Type.TString;
    appearance: Type.TOptional<Type.TUnion<[Type.TLiteral<"dark">, Type.TLiteral<"light">]>>;
    vars: Type.TOptional<Type.TRecord<"^.*$", Type.TUnion<[Type.TString, Type.TInteger]>>>;
    colors: Type.TObject<{
        accent: Type.TUnion<[Type.TString, Type.TInteger]>;
        border: Type.TUnion<[Type.TString, Type.TInteger]>;
        borderAccent: Type.TUnion<[Type.TString, Type.TInteger]>;
        borderMuted: Type.TUnion<[Type.TString, Type.TInteger]>;
        success: Type.TUnion<[Type.TString, Type.TInteger]>;
        error: Type.TUnion<[Type.TString, Type.TInteger]>;
        warning: Type.TUnion<[Type.TString, Type.TInteger]>;
        muted: Type.TUnion<[Type.TString, Type.TInteger]>;
        dim: Type.TUnion<[Type.TString, Type.TInteger]>;
        text: Type.TUnion<[Type.TString, Type.TInteger]>;
        thinkingText: Type.TUnion<[Type.TString, Type.TInteger]>;
        scrollbarTrack: Type.TOptional<Type.TUnion<[Type.TString, Type.TInteger]>>;
        scrollbarThumb: Type.TOptional<Type.TUnion<[Type.TString, Type.TInteger]>>;
        selectedBg: Type.TUnion<[Type.TString, Type.TInteger]>;
        searchMatchBg: Type.TOptional<Type.TUnion<[Type.TString, Type.TInteger]>>;
        searchMatchText: Type.TOptional<Type.TUnion<[Type.TString, Type.TInteger]>>;
        userMessageBg: Type.TUnion<[Type.TString, Type.TInteger]>;
        userMessageText: Type.TUnion<[Type.TString, Type.TInteger]>;
        customMessageBg: Type.TUnion<[Type.TString, Type.TInteger]>;
        customMessageText: Type.TUnion<[Type.TString, Type.TInteger]>;
        customMessageLabel: Type.TUnion<[Type.TString, Type.TInteger]>;
        toolPendingBg: Type.TUnion<[Type.TString, Type.TInteger]>;
        toolSuccessBg: Type.TUnion<[Type.TString, Type.TInteger]>;
        toolErrorBg: Type.TUnion<[Type.TString, Type.TInteger]>;
        toolTitle: Type.TUnion<[Type.TString, Type.TInteger]>;
        toolOutput: Type.TUnion<[Type.TString, Type.TInteger]>;
        mdHeading: Type.TUnion<[Type.TString, Type.TInteger]>;
        mdLink: Type.TUnion<[Type.TString, Type.TInteger]>;
        mdLinkUrl: Type.TUnion<[Type.TString, Type.TInteger]>;
        mdCode: Type.TUnion<[Type.TString, Type.TInteger]>;
        mdCodeBlock: Type.TUnion<[Type.TString, Type.TInteger]>;
        mdCodeBlockBorder: Type.TUnion<[Type.TString, Type.TInteger]>;
        mdQuote: Type.TUnion<[Type.TString, Type.TInteger]>;
        mdQuoteBorder: Type.TUnion<[Type.TString, Type.TInteger]>;
        mdHr: Type.TUnion<[Type.TString, Type.TInteger]>;
        mdListBullet: Type.TUnion<[Type.TString, Type.TInteger]>;
        toolDiffAdded: Type.TUnion<[Type.TString, Type.TInteger]>;
        toolDiffRemoved: Type.TUnion<[Type.TString, Type.TInteger]>;
        toolDiffContext: Type.TUnion<[Type.TString, Type.TInteger]>;
        syntaxComment: Type.TUnion<[Type.TString, Type.TInteger]>;
        syntaxKeyword: Type.TUnion<[Type.TString, Type.TInteger]>;
        syntaxFunction: Type.TUnion<[Type.TString, Type.TInteger]>;
        syntaxVariable: Type.TUnion<[Type.TString, Type.TInteger]>;
        syntaxString: Type.TUnion<[Type.TString, Type.TInteger]>;
        syntaxNumber: Type.TUnion<[Type.TString, Type.TInteger]>;
        syntaxType: Type.TUnion<[Type.TString, Type.TInteger]>;
        syntaxOperator: Type.TUnion<[Type.TString, Type.TInteger]>;
        syntaxPunctuation: Type.TUnion<[Type.TString, Type.TInteger]>;
        thinkingOff: Type.TUnion<[Type.TString, Type.TInteger]>;
        thinkingMinimal: Type.TUnion<[Type.TString, Type.TInteger]>;
        thinkingLow: Type.TUnion<[Type.TString, Type.TInteger]>;
        thinkingMedium: Type.TUnion<[Type.TString, Type.TInteger]>;
        thinkingHigh: Type.TUnion<[Type.TString, Type.TInteger]>;
        thinkingXhigh: Type.TUnion<[Type.TString, Type.TInteger]>;
        thinkingMax: Type.TOptional<Type.TUnion<[Type.TString, Type.TInteger]>>;
        bashMode: Type.TUnion<[Type.TString, Type.TInteger]>;
    }>;
    export: Type.TOptional<Type.TObject<{
        pageBg: Type.TOptional<Type.TUnion<[Type.TString, Type.TInteger]>>;
        cardBg: Type.TOptional<Type.TUnion<[Type.TString, Type.TInteger]>>;
        infoBg: Type.TOptional<Type.TUnion<[Type.TString, Type.TInteger]>>;
    }>>;
}>;
export type ThemeColorValue = Static<typeof ColorValueSchema>;
export type ValidatedThemeJson = Static<typeof ThemeJsonSchema>;
/** Validate one theme document, throwing a message that names the offending tokens. */
export declare function validateThemeJson(label: string, json: unknown): ValidatedThemeJson;
export {};
//# sourceMappingURL=theme-json.d.ts.map