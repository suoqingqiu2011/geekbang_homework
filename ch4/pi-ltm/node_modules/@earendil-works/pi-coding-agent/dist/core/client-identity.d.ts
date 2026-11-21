import { type ClientIdentity } from "@earendil-works/pi-ai";
import type { SettingsManager } from "./settings-manager.ts";
/**
 * How pi's requests identify it to providers: a detailed `User-Agent` with pi's version, and attribution to pi as a
 * CLI agent while install telemetry is enabled.
 */
export declare function getClientIdentity(settingsManager: SettingsManager): ClientIdentity;
//# sourceMappingURL=client-identity.d.ts.map