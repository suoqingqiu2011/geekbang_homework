import { type ModelRuntime } from "../core/model-runtime.ts";
import type { LoginTerminal } from "./auth-terminal.ts";
/** Dependencies of `runAuthLogin`, injected so tests can script the terminal. */
export interface AuthLoginOptions {
    modelRuntime: ModelRuntime;
    terminal: LoginTerminal;
    agentDir: string;
    cwd: string;
    getDeviceId: () => string;
    /** Aborts the login, e.g. on Ctrl+C. */
    signal: AbortSignal;
}
/** Run `pi auth login <provider>` in the terminal and return the process exit code. */
export declare function runAuthLogin(args: string[], options: AuthLoginOptions): Promise<number>;
//# sourceMappingURL=auth-login.d.ts.map