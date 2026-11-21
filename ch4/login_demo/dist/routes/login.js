"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.loginRouter = void 0;
const express_1 = require("express");
const auth_service_1 = require("../services/auth-service");
exports.loginRouter = (0, express_1.Router)();
// Login entry point for the demo: POST /api/login
exports.loginRouter.post("/login", async (request, response) => {
    const { email, password } = request.body;
    if (!email || !password) {
        response.status(400).json({ error: "email and password are required" });
        return;
    }
    try {
        const result = await auth_service_1.authService.login(email, password);
        response.status(200).json(result);
    }
    catch (error) {
        response.status(401).json({
            error: error instanceof Error ? error.message : "Login failed"
        });
    }
});
