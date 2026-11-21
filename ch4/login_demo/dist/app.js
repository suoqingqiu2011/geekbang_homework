"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.createApp = createApp;
const express_1 = __importDefault(require("express"));
const login_1 = require("./routes/login");
const profile_1 = require("./routes/profile");
function createApp() {
    const app = (0, express_1.default)();
    app.use(express_1.default.json());
    app.use("/api", login_1.loginRouter);
    app.use("/api", profile_1.profileRouter);
    app.get("/health", (_request, response) => {
        response.status(200).json({ ok: true });
    });
    return app;
}
