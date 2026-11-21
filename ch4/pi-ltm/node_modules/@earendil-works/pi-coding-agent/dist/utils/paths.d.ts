export interface PathInputOptions {
    /** Trim leading/trailing whitespace before normalization. */
    trim?: boolean;
    /** Expand leading `~` to a home directory. Defaults to true. */
    expandTilde?: boolean;
    /** Home directory used for `~` expansion. Defaults to `os.homedir()`. */
    homeDir?: string;
    /** Strip a leading `@`, used for CLI @file paths. */
    stripAtPrefix?: boolean;
    /** Normalize unicode space variants to regular spaces. */
    normalizeUnicodeSpaces?: boolean;
}
/**
 * Resolve a path to its canonical (real) form, following symlinks.
 * Falls back to the raw path if resolution fails (e.g. the target does
 * not exist yet), so that callers never crash on missing filesystem
 * entries.
 */
export declare function canonicalizePath(path: string): string;
/**
 * Key for comparing filesystem paths: the resolved absolute path, case-folded
 * on Windows because Windows paths are case-insensitive (`c:\Users` and
 * `C:\Users` are the same directory). NTFS can enable per-directory case
 * sensitivity, but it is rarely used outside WSL interop, so it is ignored
 * here (https://learn.microsoft.com/en-us/windows/wsl/case-sensitivity).
 * Does not touch the filesystem; call `canonicalizePath` first when symlinks
 * must compare equal. Use only for comparison, never for display or
 * filesystem access.
 */
export declare function getPathComparisonKey(path: string): string;
/** True when both paths name the same location (see `getPathComparisonKey`). */
export declare function pathsEqual(left: string, right: string): boolean;
/** True when `child` is `parent` or inside it (see `getPathComparisonKey`). */
export declare function isPathWithin(parent: string, child: string): boolean;
/** Replace a leading home directory with `~` for display. Relative paths are returned unchanged. */
export declare function shortenHomePath(path: string, homeDir?: string): string;
export declare function getFileRevision(path: string): string | undefined;
/**
 * Returns true if the value is NOT a package source (npm:, git:, etc.),
 * a built-in extension (builtin:), or a remote URL protocol. Bare names,
 * relative paths, and file: URLs are considered local.
 */
export declare function isLocalPath(value: string): boolean;
/** Convert Git Bash, MSYS, Cygwin, and WSL drive paths to a form native Windows APIs accept. */
export declare function normalizeWindowsShellPath(filePath: string): string;
export declare function normalizePath(input: string, options?: PathInputOptions): string;
export declare function resolvePath(input: string, baseDir?: string, options?: PathInputOptions): string;
export declare function getCwdRelativePath(filePath: string, cwd: string): string | undefined;
export declare function formatPathRelativeToCwdOrAbsolute(filePath: string, cwd: string): string;
export declare function markPathIgnoredByCloudSync(path: string): void;
//# sourceMappingURL=paths.d.ts.map