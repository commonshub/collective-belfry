// Three web apps from one tree: APP=people, market or photos picks the root (web/<app>); all import
// web/shared. Built into web/<app>/dist and served by server/index.js.
import { defineConfig } from "vite"

const app = process.env.APP
if (!["people", "market", "photos"].includes(app)) throw new Error("set APP=people, APP=market or APP=photos")

export default defineConfig({
  root: `${import.meta.dirname}/${app}`,
  envDir: import.meta.dirname,
  // Keycast clients belfry-people, belfry-market and belfry-photos (registered and trusted by bootstrap/Compose)
  define: {
    "import.meta.env.VITE_APP_NAME": JSON.stringify(app),
    "import.meta.env.VITE_OAUTH_CLIENT_ID": JSON.stringify(`belfry-${app}`),
  },
  build: { outDir: "dist", emptyOutDir: true, target: "es2022" },
})
