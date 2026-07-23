import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: proxy API + webhooks to the local FastAPI. Prod: API lives on api.<domain>,
// set via VITE_API_BASE at build time (see Dockerfile / compose).
//
// DEV_API_TARGET lets the responsive screenshot harness (scripts/shoot.mjs) point dev at the
// deployed API so layouts get exercised against real data lengths. Proxying rather than
// setting VITE_API_BASE keeps the browser same-origin, so no CORS config is needed.
const apiTarget = process.env.DEV_API_TARGET || "http://localhost:8888";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: apiTarget, changeOrigin: true, secure: true },
      "/webhooks": { target: apiTarget, changeOrigin: true, secure: true },
    },
  },
});
