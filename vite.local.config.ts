import vinext from "vinext";
import { defineConfig } from "vite";
import { sites } from "./build/sites-vite-plugin";

// Local visual preview for hosts that cannot run the bundled workerd binary.
// Production builds continue to use vite.config.ts and Cloudflare Workers.
export default defineConfig({
  server: { watch: { useFsEvents: false, usePolling: true } },
  plugins: [vinext(), sites()],
});
