import { PI_ATTRIBUTION } from "@earendil-works/pi-ai";
import { VERSION } from "../config.js";
import { isInstallTelemetryEnabled } from "./telemetry.js";
/**
 * How pi's requests identify it to providers: a detailed `User-Agent` with pi's version, and attribution to pi as a
 * CLI agent while install telemetry is enabled.
 */
export function getClientIdentity(settingsManager) {
    return {
        userAgent: "detailed",
        version: VERSION,
        attribution: isInstallTelemetryEnabled(settingsManager) ? PI_ATTRIBUTION : null,
    };
}
//# sourceMappingURL=client-identity.js.map