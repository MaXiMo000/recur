import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The proxy keeps the browser on one origin in development, so cookies behave
// exactly as they will in production.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } },
  },
});
