import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

const ui = fileURLToPath(new URL('.', import.meta.url));

export default defineConfig({
  root: ui,
  define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  esbuild: { jsx: 'automatic' },
  build: {
    outDir: '../frontend',
    emptyOutDir: true,
    minify: true,
    lib: { entry: 'entry.tsx', formats: ['es'], fileName: () => 'index.js' },
  },
});
