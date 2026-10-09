import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("@xterm")) return "vendor-terminal";
          if (id.includes("@tanstack")) return "vendor-query";
          if (id.includes("react") || id.includes("scheduler")) return "vendor-react";
          if (id.includes("axios") || id.includes("zustand")) return "vendor-data";
          return undefined;
        },
      },
    },
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
  },
});
