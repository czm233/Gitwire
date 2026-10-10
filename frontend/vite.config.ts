import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    // 本机端口登记表 10240-10249 号段：后端 10240，前端 dev 10241
    port: 10241,
    host: true,
    proxy: {
      "/api": "http://127.0.0.1:10240",
    },
  },
  preview: {
    // e2e 隔离栈用 preview 伺服构建版；默认代理到本机后端，e2e_stack.py 会覆盖目标端口
    proxy: {
      "/api": process.env.E2E_API_TARGET || "http://127.0.0.1:10240",
    },
  },
  build: {
    outDir: "dist",
    chunkSizeWarningLimit: 1200,
  },
});
