"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.Webhook = exports.WebhookVerificationError = void 0;
const base64 = require("@stablelib/base64");
const sha256 = require("fast-sha256");
const timing_safe_equal_1 = require("./timing_safe_equal");
const WEBHOOK_TOLERANCE_IN_SECONDS = 5 * 60;
class ExtendableError extends Error {
    constructor(message) {
        super(message);
        Object.setPrototypeOf(this, ExtendableError.prototype);
        this.name = "ExtendableError";
        this.stack = new Error(message).stack;
    }
}
class WebhookVerificationError extends ExtendableError {
    constructor(message) {
        super(message);
        Object.setPrototypeOf(this, WebhookVerificationError.prototype);
        this.name = "WebhookVerificationError";
    }
}
exports.WebhookVerificationError = WebhookVerificationError;
function payloadBytes(payload) {
    if (typeof payload === "string") {
        return new TextEncoder().encode(payload);
    }
    if (ArrayBuffer.isView(payload) && payload.BYTES_PER_ELEMENT === 1) {
        return new Uint8Array(payload.buffer, payload.byteOffset, payload.byteLength);
    }
    throw new Error("Expected payload to be of type string, Buffer or Uint8Array.");
}
class Webhook {
    constructor(secret, options) {
        if ((options === null || options === void 0 ? void 0 : options.format) === "raw") {
            if (secret instanceof Uint8Array) {
                this.key = secret;
            }
            else {
                this.key = Uint8Array.from(secret, (c) => c.charCodeAt(0));
            }
        }
        else {
            if (typeof secret !== "string") {
                throw new Error("Expected secret to be of type string");
            }
            if (secret.startsWith(Webhook.prefix)) {
                secret = secret.substring(Webhook.prefix.length);
            }
            this.key = base64.decode(secret);
        }
        if (this.key.length === 0) {
            throw new Error("Secret can't be empty.");
        }
    }
    verify(payload, headers, options) {
        var _a;
        const jsonParse = (_a = options === null || options === void 0 ? void 0 : options.jsonParse) !== null && _a !== void 0 ? _a : true;
        const normalizedHeaders = {};
        for (const key of Object.keys(headers)) {
            normalizedHeaders[key.toLowerCase()] = headers[key];
        }
        const msgId = normalizedHeaders["webhook-id"];
        const msgSignature = normalizedHeaders["webhook-signature"];
        const msgTimestamp = normalizedHeaders["webhook-timestamp"];
        if (!msgSignature || !msgId || !msgTimestamp) {
            throw new WebhookVerificationError("Missing required headers");
        }
        const timestamp = this.verifyTimestamp(msgTimestamp);
        const bytes = payloadBytes(payload);
        const encoder = new globalThis.TextEncoder();
        const expectedSignature = encoder.encode(this.sign(msgId, timestamp, bytes));
        const passedSignatures = msgSignature.split(" ");
        if (!passedSignatures.some((versionedSignature) => (0, timing_safe_equal_1.timingSafeEqual)(encoder.encode(versionedSignature), expectedSignature))) {
            throw new WebhookVerificationError("No matching signature found");
        }
        if (bytes.length === 0) {
            return undefined;
        }
        if (jsonParse) {
            return JSON.parse(typeof payload === "string" ? payload : new TextDecoder().decode(bytes));
        }
        else {
            return undefined;
        }
    }
    sign(msgId, timestamp, payload) {
        const bytes = payloadBytes(payload);
        const timestampNumber = Math.floor(timestamp.getTime() / 1000);
        const prefix = new TextEncoder().encode(`${msgId}.${timestampNumber}.`);
        const toSign = new Uint8Array(prefix.length + bytes.length);
        toSign.set(prefix);
        toSign.set(bytes, prefix.length);
        const expectedSignature = base64.encode(sha256.hmac(this.key, toSign));
        return `v1,${expectedSignature}`;
    }
    endpointVerificationResponse(challenge) {
        const encoder = new TextEncoder();
        const verificationKey = sha256.hmac(this.key, encoder.encode("wev-v1"));
        const signature = base64.encode(sha256.hmac(verificationKey, encoder.encode(challenge)));
        return `v1,${signature}`;
    }
    verifyEndpointVerificationResponse(challenge, header) {
        if (!header) {
            throw new WebhookVerificationError("Missing challenge response");
        }
        const encoder = new globalThis.TextEncoder();
        const expectedSignature = encoder.encode(this.endpointVerificationResponse(challenge));
        if (!(0, timing_safe_equal_1.timingSafeEqual)(encoder.encode(header), expectedSignature)) {
            throw new WebhookVerificationError("No matching signature found");
        }
    }
    verifyTimestamp(timestampHeader) {
        const now = Math.floor(Date.now() / 1000);
        const timestamp = parseInt(timestampHeader, 10);
        if (Number.isNaN(timestamp)) {
            throw new WebhookVerificationError("Invalid Signature Headers");
        }
        if (now - timestamp > WEBHOOK_TOLERANCE_IN_SECONDS) {
            throw new WebhookVerificationError("Message timestamp too old");
        }
        if (timestamp > now + WEBHOOK_TOLERANCE_IN_SECONDS) {
            throw new WebhookVerificationError("Message timestamp too new");
        }
        return new Date(timestamp * 1000);
    }
}
exports.Webhook = Webhook;
Webhook.prefix = "whsec_";
//# sourceMappingURL=index.js.map