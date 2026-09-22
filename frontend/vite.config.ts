import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// VoiceCare LK staff dashboard (Step 9).
// Talks straight to the backend on :8000 (CORS_ALLOW_ORIGINS already allows
// this origin in backend/.env), so no dev proxy is needed.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173, strictPort: true },
});