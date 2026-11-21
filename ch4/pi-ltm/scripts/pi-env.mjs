#!/usr/bin/env node
/**
 * 启动 pi 前加载项目根目录的 .env。
 * 已存在的系统环境变量优先级更高，不会被 .env 覆盖。
 * 用法：node scripts/pi-env.mjs [pi 参数...]，或 npm start / npm run pi -- <参数>
 */
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const envFile = resolve(projectRoot, ".env");

if (existsSync(envFile)) {
	try {
		process.loadEnvFile(envFile);
	} catch (error) {
		console.error(`[pi-env] 加载 ${envFile} 失败: ${error.message}`);
		process.exit(1);
	}
}

const args = process.argv.slice(2);
const localPi = resolve(projectRoot, "node_modules", ".bin", process.platform === "win32" ? "pi.cmd" : "pi");
const piCommand = existsSync(localPi) ? localPi : "pi";
const child =
	process.platform === "win32"
		? spawn(process.env.ComSpec ?? "cmd.exe", ["/d", "/s", "/c", piCommand, ...args], {
				cwd: projectRoot,
				env: process.env,
				stdio: "inherit",
			})
		: spawn(piCommand, args, {
				cwd: projectRoot,
				env: process.env,
				stdio: "inherit",
			});

child.on("error", (error) => {
	console.error(`[pi-env] 启动 pi 失败: ${error.message}`);
	process.exit(1);
});

child.on("exit", (code) => {
	process.exit(code ?? 1);
});
