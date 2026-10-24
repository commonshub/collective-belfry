// The web app of the vote service (built into web/dist, served by server/index.js).
import { defineConfig } from "vite"

export default defineConfig({
  root: import.meta.dirname,
  envDir: import.meta.dirname,
  build: { outDir: "dist", emptyOutDir: true, target: "es2022" },
})
