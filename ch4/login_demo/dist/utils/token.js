"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.issueAccessToken = issueAccessToken;
exports.parseAccessToken = parseAccessToken;
function issueAccessToken(payload) {
    const encoded = Buffer.from(JSON.stringify(payload), "utf8").toString("base64url");
    return `demo-token.${encoded}`;
}
function parseAccessToken(token) {
    if (!token.startsWith("demo-token.")) {
        return null;
    }
    const encoded = token.replace("demo-token.", "");
    try {
        const json = Buffer.from(encoded, "base64url").toString("utf8");
        return JSON.parse(json);
    }
    catch {
        return null;
    }
}
