import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies /api to the Python engine so that flipping USE_MOCK
// to false in src/api.ts requires no other code change.
// VITE_PORT / VITE_API_PORT let a second (dev) instance run beside the main one
// (e.g. 5174 -> 8001) without touching this file.
const port = Number(process.env.VITE_PORT ?? 5173);
const apiPort = Number(process.env.VITE_API_PORT ?? 8000);

export default defineConfig({
  plugins: [react()],
  server: {
    port,
    strictPort: true,
    proxy: {
      "/api": {
        target: `http://localhost:${apiPort}`,
        changeOrigin: true,
      },
    },
  },
});
