import path from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  test: {
    // DOMPurify needs a DOM to sanitize against.
    environment: "jsdom",
  },
});
