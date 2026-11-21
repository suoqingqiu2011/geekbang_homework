"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.profileRouter = void 0;
const express_1 = require("express");
const auth_middleware_1 = require("../middleware/auth-middleware");
exports.profileRouter = (0, express_1.Router)();
exports.profileRouter.get("/profile", auth_middleware_1.authMiddleware, (request, response) => {
    response.status(200).json({
        message: "Authenticated request",
        currentUser: response.locals.currentUser
    });
});
