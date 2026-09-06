import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * Two things here are load-bearing for the latency requirement.
 *
 * The proxy: the console talks to `/api` on its own origin in every environment, so
 * there is no base-URL constant to get wrong, no preflight on every request, and the
 * HLS relay's segment URLs — which the backend derives from its own mounted route —
 * resolve without rewriting. The CORS allowance in the API stays for other clients.
 *
 * The manual chunks: hls.js and leaflet are ~150 kB together and only two routes need
 * them. Left alone, Rollup hoists shared imports into the entry chunk, so the operator
 * pays for the map and the video decoder before the dashboard's first paint. Splitting
 * them by name keeps the initial download to the shell plus one route.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        // SSE and HLS both stream. Buffering either one turns a live feed into a
        // slideshow, and the default proxy does buffer.
        ws: false,
        configure: (proxy) => {
          proxy.on("proxyReq", (proxyReq) => proxyReq.setHeader("accept-encoding", "identity"));
        },
      },
    },
  },
  build: {
    target: "es2022",
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          hls: ["hls.js"],
          map: ["leaflet"],
          query: ["@tanstack/react-query", "@tanstack/react-virtual"],
        },
      },
    },
  },
});
