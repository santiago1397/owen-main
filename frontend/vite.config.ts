import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: proxy API + webhooks to the local FastAPI. Prod: API lives on api.<domain>,
// set via VITE_API_BASE at build time (see Dockerfile / compose).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8888",
      "/webhooks": "http://localhost:8888",
    },
  },
});
