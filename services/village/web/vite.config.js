// Four web apps from one tree: APP=people, market, photos or programme picks the root (web/<app>); all import
// web/shared. Built into web/<app>/dist and served by server/index.js.
import { defineConfig } from "vite"

const app = process.env.APP
if (!["people", "market", "photos", "programme"].includes(app)) throw new Error("set APP=people, market, photos or programme")

export default defineConfig({
  root: `${import.meta.dirname}/${app}`,
  envDir: import.meta.dirname,
  // Keycast clients belfry-people, belfry-market, belfry-photos and belfry-programme (registered and trusted by bootstrap/Compose)
  define: {
    "import.meta.env.VITE_APP_NAME": JSON.stringify(app),
    "import.meta.env.VITE_OAUTH_CLIENT_ID": JSON.stringify(`belfry-${app}`),
  },
  build: { outDir: "dist", emptyOutDir: true, target: "es2022" },
})
