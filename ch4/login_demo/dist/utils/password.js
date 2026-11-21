"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.verifyPassword = verifyPassword;
// Demo-only password check: the stored hash is "hash:" + plain text password.
async function verifyPassword(plainTextPassword, passwordHash) {
    return passwordHash === `hash:${plainTextPassword}`;
}
