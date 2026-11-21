"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.authService = exports.AuthService = void 0;
const user_repository_1 = require("../repositories/user-repository");
const password_1 = require("../utils/password");
const token_1 = require("../utils/token");
class AuthService {
    async login(email, password) {
        const user = await user_repository_1.userRepository.findByEmail(email);
        if (!user) {
            throw new Error("User not found");
        }
        const passwordMatched = await (0, password_1.verifyPassword)(password, user.passwordHash);
        if (!passwordMatched) {
            throw new Error("Invalid password");
        }
        const accessToken = (0, token_1.issueAccessToken)({
            userId: user.id,
            role: user.role
        });
        return {
            accessToken,
            user: {
                id: user.id,
                email: user.email,
                displayName: user.displayName,
                role: user.role
            }
        };
    }
}
exports.AuthService = AuthService;
exports.authService = new AuthService();
