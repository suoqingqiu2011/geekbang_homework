"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.queryOne = queryOne;
const usersTable = [
    {
        id: "u_1001",
        email: "demo@example.com",
        passwordHash: "hash:demo123",
        displayName: "Demo User",
        role: "member"
    },
    {
        id: "u_1002",
        email: "admin@example.com",
        passwordHash: "hash:admin123",
        displayName: "Admin User",
        role: "admin"
    }
];
function getTable(table) {
    if (table === "users") {
        return usersTable;
    }
    return [];
}
// Simulated database access so the repository can clearly show a users table lookup.
async function queryOne(table, where) {
    const rows = getTable(table);
    return rows.find((row) => Object.entries(where).every(([key, value]) => {
        const field = key;
        return row[field] === value;
    }));
}
