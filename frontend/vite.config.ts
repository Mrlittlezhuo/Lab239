/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// 开发期把 API 前缀代理到 chatchat API 服务（默认 7861）。
// http-proxy 默认流式转发，不缓冲 SSE。
const API_TARGET = process.env.VITE_PROXY_TARGET ?? "http://127.0.0.1:7861";
const API_PREFIXES = [
  "/auth",
  "/admin",
  "/chat",
  "/conversations",
  "/knowledge_base",
  "/tools",
  "/v1",
  "/server",
  "/api/v1",
  "/memories",
  "/agent-runs",
  "/research",
  "/media",
  "/img",
];

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(import.meta.dirname, "src") },
  },
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      API_PREFIXES.map((prefix) => [
        prefix,
        { target: API_TARGET, changeOrigin: true },
      ]),
    ),
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
