import { readFile, realpath, stat } from "node:fs/promises";
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { bundleFacets } from "./bundle.js";
/** Build a plugin package using package.json metadata and application-provided facet conventions. */
export async function bundleFacetPackage(options) {
    const metadata = await readFacetPackageMetadata(options.packagePath);
    const entries = await resolveFacetEntries(metadata, options.defaultFacets ?? {});
    const external = [...metadata.peerDependencies, ...metadata.external].flatMap((specifier) => [
        specifier,
        `${specifier}/*`,
    ]);
    const result = await bundleFacets({
        plugin: { id: metadata.name, version: metadata.version },
        entries,
        outdir: options.outdir,
        workingDirectory: metadata.packageDirectory,
        external,
        sourceMap: metadata.sourceMap,
    });
    return Object.freeze({
        ...result,
        packageDirectory: metadata.packageDirectory,
        packageJsonPath: metadata.packageJsonPath,
    });
}
async function readFacetPackageMetadata(packagePath) {
    if (packagePath.length === 0)
        throw new TypeError("Facet package path must not be empty");
    const candidate = resolve(packagePath);
    let packageDirectory;
    let packageJsonPath;
    let candidateStats;
    try {
        candidateStats = await stat(candidate);
    }
    catch (error) {
        throw new Error(`Could not access facet package ${candidate}`, { cause: error });
    }
    if (candidateStats.isDirectory()) {
        packageDirectory = await realpath(candidate);
        packageJsonPath = join(packageDirectory, "package.json");
    }
    else if (candidateStats.isFile() && basename(candidate) === "package.json") {
        packageJsonPath = await realpath(candidate);
        packageDirectory = dirname(packageJsonPath);
    }
    else {
        throw new Error(`Facet package path must name a directory or package.json: ${candidate}`);
    }
    let parsed;
    try {
        parsed = JSON.parse(await readFile(packageJsonPath, "utf8"));
    }
    catch (error) {
        throw new Error(`Could not read facet package metadata ${packageJsonPath}`, { cause: error });
    }
    if (!isRecord(parsed))
        throw new Error(`Facet package metadata must be an object: ${packageJsonPath}`);
    if (typeof parsed.name !== "string" || parsed.name.length === 0) {
        throw new Error(`Facet package must have a non-empty name: ${packageJsonPath}`);
    }
    if (typeof parsed.version !== "string" || parsed.version.length === 0) {
        throw new Error(`Facet package must have a non-empty version: ${packageJsonPath}`);
    }
    const peerDependencies = parsePeerDependencies(parsed.peerDependencies, packageJsonPath);
    const chord = parseChordConfiguration(parsed.chord, packageJsonPath);
    return {
        packageDirectory,
        packageJsonPath,
        name: parsed.name,
        version: parsed.version,
        peerDependencies,
        configuredFacets: chord.facets,
        external: chord.external,
        sourceMap: chord.sourceMap,
    };
}
function parsePeerDependencies(value, packageJsonPath) {
    if (value === undefined)
        return [];
    if (!isRecord(value) ||
        Object.entries(value).some(([name, version]) => name.length === 0 || typeof version !== "string")) {
        throw new Error(`Facet package has invalid peerDependencies: ${packageJsonPath}`);
    }
    return Object.freeze(Object.keys(value).sort());
}
function parseChordConfiguration(value, packageJsonPath) {
    if (value === undefined)
        return { facets: Object.freeze({}), external: Object.freeze([]), sourceMap: true };
    if (!isRecord(value))
        throw new Error(`Facet package chord configuration must be an object: ${packageJsonPath}`);
    if (Object.keys(value).some((key) => key !== "facets" && key !== "external" && key !== "sourceMap")) {
        throw new Error(`Facet package chord configuration has an unknown field: ${packageJsonPath}`);
    }
    const facets = {};
    if (value.facets !== undefined) {
        if (!isRecord(value.facets)) {
            throw new Error(`Facet package chord.facets must be an object: ${packageJsonPath}`);
        }
        for (const [name, source] of Object.entries(value.facets)) {
            if (name.length === 0 || (typeof source !== "string" && source !== false) || source === "") {
                throw new Error(`Facet package has an invalid chord.facets entry: ${packageJsonPath}`);
            }
            facets[name] = source;
        }
    }
    let external = [];
    if (value.external !== undefined) {
        if (!Array.isArray(value.external) ||
            value.external.some((specifier) => typeof specifier !== "string" || specifier.length === 0)) {
            throw new Error(`Facet package chord.external must contain non-empty strings: ${packageJsonPath}`);
        }
        external = Object.freeze([...new Set(value.external)].sort());
    }
    if (value.sourceMap !== undefined && typeof value.sourceMap !== "boolean") {
        throw new Error(`Facet package chord.sourceMap must be a boolean: ${packageJsonPath}`);
    }
    return {
        facets: Object.freeze(facets),
        external,
        sourceMap: value.sourceMap ?? true,
    };
}
async function resolveFacetEntries(metadata, defaultFacets) {
    const entries = {};
    for (const [name, source] of Object.entries(defaultFacets)) {
        validateFacetMapping(name, source, "default");
        const path = resolvePackageEntry(metadata.packageDirectory, source, name);
        try {
            const entryStats = await stat(path);
            if (!entryStats.isFile())
                throw new Error(`Default facet entry ${name} is not a file: ${path}`);
            const canonicalPath = await realpath(path);
            validateCanonicalPackageEntry(metadata.packageDirectory, canonicalPath, name);
            entries[name] = canonicalPath;
        }
        catch (error) {
            if (isMissingPath(error))
                continue;
            throw error;
        }
    }
    for (const [name, source] of Object.entries(metadata.configuredFacets)) {
        if (source === false) {
            delete entries[name];
            continue;
        }
        validateFacetMapping(name, source, "configured");
        const path = resolvePackageEntry(metadata.packageDirectory, source, name);
        let entryStats;
        try {
            entryStats = await stat(path);
        }
        catch (error) {
            throw new Error(`Could not access configured facet entry ${name}: ${path}`, { cause: error });
        }
        if (!entryStats.isFile())
            throw new Error(`Configured facet entry ${name} is not a file: ${path}`);
        const canonicalPath = await realpath(path);
        validateCanonicalPackageEntry(metadata.packageDirectory, canonicalPath, name);
        entries[name] = canonicalPath;
    }
    if (Object.keys(entries).length === 0) {
        throw new Error(`Facet package ${metadata.name} has no configured or conventional facet entries`);
    }
    return Object.freeze(entries);
}
function validateFacetMapping(name, source, kind) {
    if (name.length === 0)
        throw new Error(`Facet package ${kind} entry name must not be empty`);
    if (source.length === 0)
        throw new Error(`Facet package ${kind} entry ${name} must have a source path`);
}
function resolvePackageEntry(packageDirectory, source, name) {
    if (isAbsolute(source))
        throw new Error(`Facet package entry ${name} must be relative to the package directory`);
    const path = resolve(packageDirectory, source);
    const relativePath = relative(packageDirectory, path);
    if (relativePath.length === 0 ||
        relativePath === ".." ||
        relativePath.startsWith(`..${sep}`) ||
        isAbsolute(relativePath)) {
        throw new Error(`Facet package entry ${name} escapes the package directory`);
    }
    return path;
}
function validateCanonicalPackageEntry(packageDirectory, path, name) {
    const relativePath = relative(packageDirectory, path);
    if (relativePath === ".." || relativePath.startsWith(`..${sep}`) || isAbsolute(relativePath)) {
        throw new Error(`Facet package entry ${name} resolves outside the package directory`);
    }
}
function isMissingPath(error) {
    return isRecord(error) && error.code === "ENOENT";
}
function isRecord(value) {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}
//# sourceMappingURL=package.js.map