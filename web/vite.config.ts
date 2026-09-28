import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run build` writes straight into the Python package so the wheel ships
// the UI. `npm run dev` proxies /api to a running `msts-trader ui --port 8765`
// (start that with MSTS_UI_DEV_ORIGIN=http://localhost:5173 and open the dev
// server with the same ?t=TOKEN it printed).
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "../msts_trader/ui/static",
    emptyOutDir: true,
    chunkSizeWarningLimit: 800,
  },
  server: {
    proxy: { "/api": "http://127.0.0.1:8765" },
  },
});
