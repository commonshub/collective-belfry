// Belfry: replaces Coracle's pwa-assets.config.js. The source (brand/apps/chat-maskable-512.png,
// copied to public/images/logo.png) is already full bleed with the symbol inside the maskable safe
// zone, so no padding and no white background (the minimal preset's default).
import {defineConfig} from "@vite-pwa/assets-generator/config"

export default defineConfig({
  preset: {
    transparent: {sizes: [64, 192, 512], favicons: [[48, "favicon.ico"]], padding: 0},
    maskable: {sizes: [512], padding: 0},
    apple: {sizes: [180], padding: 0},
  },
  images: ["public/images/logo.png"],
})
