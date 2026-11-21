import { type ModelRuntime } from "../core/model-runtime.ts";
import type { AuthOutput } from "./auth-terminal.ts";
/** Dependencies of `runAuthLogout`, injected so tests can capture the output. */
export interface AuthLogoutOptions {
    modelRuntime: ModelRuntime;
    terminal: AuthOutput;
    signal: AbortSignal;
}
/**
 * Run `pi auth logout <provider>`: remove the credential `pi auth login` or /login stored, and return the exit code.
 * Environment variables and models.json config stay, as with /logout.
 */
export declare function runAuthLogout(args: string[], options: AuthLogoutOptions): Promise<number>;
//# sourceMappingURL=auth-logout.d.ts.map