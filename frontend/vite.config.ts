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
  build: {
    outDir: "dist",
    chunkSizeWarningLimit: 1200,
  },
});
