import { toCodemodeIdentifier } from "./identifier.ts";
export { toCodemodeIdentifier };
import type { CodemodeJsonSchema, CodemodeTool } from "./types.ts";
/** Largest rendered input type, in characters, before it becomes `unknown`. */
export declare const DEFAULT_INPUT_SCHEMA_MAX_CHARS = 16000;
/**
 * TypeScript types for MCP results, from the MCP `CallToolResult` schema, so `CallToolResult<T>`
 * declarations can refer to them.
 */
export declare const MCP_TYPESCRIPT_PREAMBLE = "type Role = \"user\" | \"assistant\";\ntype MetaObject = Record<string, unknown>;\ntype Annotations = {\n  audience?: Role[];\n  priority?: number;\n  lastModified?: string;\n};\ntype Icon = {\n  src: string;\n  mimeType?: string;\n  sizes?: string[];\n  theme?: \"light\" | \"dark\";\n};\ntype TextResourceContents = {\n  uri: string;\n  mimeType?: string;\n  _meta?: MetaObject;\n  text: string;\n};\ntype BlobResourceContents = {\n  uri: string;\n  mimeType?: string;\n  _meta?: MetaObject;\n  blob: string;\n};\ntype TextContent = {\n  type: \"text\";\n  text: string;\n  annotations?: Annotations;\n  _meta?: MetaObject;\n};\ntype ImageContent = {\n  type: \"image\";\n  data: string;\n  mimeType: string;\n  annotations?: Annotations;\n  _meta?: MetaObject;\n};\ntype AudioContent = {\n  type: \"audio\";\n  data: string;\n  mimeType: string;\n  annotations?: Annotations;\n  _meta?: MetaObject;\n};\ntype ResourceLink = {\n  icons?: Icon[];\n  name: string;\n  title?: string;\n  uri: string;\n  description?: string;\n  mimeType?: string;\n  annotations?: Annotations;\n  size?: number;\n  _meta?: MetaObject;\n  type: \"resource_link\";\n};\ntype EmbeddedResource = {\n  type: \"resource\";\n  resource: TextResourceContents | BlobResourceContents;\n  annotations?: Annotations;\n  _meta?: MetaObject;\n};\ntype ContentBlock =\n  | TextContent\n  | ImageContent\n  | AudioContent\n  | ResourceLink\n  | EmbeddedResource;\ntype CallToolResult<TStructured = { [key: string]: unknown }> = {\n  _meta?: MetaObject;\n  content: ContentBlock[];\n  isError?: boolean;\n  structuredContent?: TStructured;\n  [key: string]: unknown;\n};";
export interface RenderDeclarationsOptions {
    tools?: readonly CodemodeTool[];
    globals?: readonly CodemodeTool[];
}
/**
 * Render TypeScript declarations for the script-visible API. Tools become members of
 * `declare const tools`, globals become `declare function` statements, and `ns.member` globals
 * members of `declare const ns`. Descriptions become doc comments; schemas become types.
 */
export declare function renderDeclarations(options: RenderDeclarationsOptions): string;
/**
 * One tool as a member of the `tools` object: `name(args: T): Promise<R>;` with the
 * name as the identifier scripts use. Input types longer than `inputMaxChars` render as `unknown`.
 * Tools whose output schema is an MCP `CallToolResult` render as `Promise<CallToolResult<T>>`,
 * which needs {@link MCP_TYPESCRIPT_PREAMBLE}.
 */
export declare function renderToolSignature(tool: Pick<CodemodeTool, "name" | "inputSchema" | "outputSchema">, options?: {
    inputMaxChars?: number;
}): string;
/**
 * A tool's sample: the description followed by the tool's declaration. Used for tool
 * listings and `ALL_TOOLS` entries.
 */
export declare function renderToolSample(tool: Pick<CodemodeTool, "name" | "description" | "inputSchema" | "outputSchema">, options?: {
    inputMaxChars?: number;
}): string;
/**
 * The `structuredContent` schema of an MCP `CallToolResult` output schema (detected by a
 * `content` array of objects, boolean `isError`, and object `_meta`), `true` when it declares none, or
 * `undefined` when the schema is not a `CallToolResult`.
 */
export declare function mcpStructuredContentSchema(schema: CodemodeJsonSchema | undefined): CodemodeJsonSchema | undefined;
/**
 * The type a tool call resolves to: `CallToolResult<T>` for MCP output schemas (needs
 * {@link MCP_TYPESCRIPT_PREAMBLE}), the schema's type otherwise, and `unknown` without a schema.
 */
export declare function renderToolOutputType(schema: CodemodeJsonSchema | undefined): string;
/**
 * Convert a JSON Schema to a TypeScript type expression: objects on one line (`{ a: string; b?: number; }`) with properties sorted by name,
 * or one property per line with `//` comments when a property has a description; `Array<T>` for
 * arrays. Local references (`#/$defs/...`, `#/definitions/...`) resolve against `schema`;
 * recursive and remote references render as `unknown`. A result longer than `maxChars` renders as
 * `unknown`.
 */
export declare function schemaToType(schema: CodemodeJsonSchema, options?: {
    maxChars?: number;
}): string;
//# sourceMappingURL=declarations.d.ts.map