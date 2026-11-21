"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.userRepository = exports.UserRepository = void 0;
const database_1 = require("../db/database");
class UserRepository {
    async findByEmail(email) {
        // The repository is responsible for reading the users table.
        return (0, database_1.queryOne)("users", { email });
    }
}
exports.UserRepository = UserRepository;
exports.userRepository = new UserRepository();
