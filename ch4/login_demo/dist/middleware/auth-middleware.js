"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.authMiddleware = authMiddleware;
const token_1 = require("../utils/token");
function authMiddleware(request, response, next) {
    const authHeader = request.headers.authorization;
    if (!authHeader?.startsWith("Bearer ")) {
        response.status(401).json({ error: "Missing bearer token" });
        return;
    }
    const token = authHeader.replace("Bearer ", "");
    const payload = (0, token_1.parseAccessToken)(token);
    if (!payload) {
        response.status(401).json({ error: "Invalid token" });
        return;
    }
    response.locals.currentUser = payload;
    next();
}
