import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import { existsSync } from 'node:fs';

/**
 * By default Vite's dev server falls back to serving index.html for any
 * unknown route — even requests like `/assets/buildings/castle.png`. When
 * the file is missing, Phaser's image loader receives HTML with status 200
 * instead of a genuine 404, and may silently accept it as a "loaded" (but
 * broken) texture instead of firing FILE_LOAD_ERROR. The Village scene then
 * transitions anyway, rendering a map without tiles.
 *
 * This plugin returns a real 404 for any `/assets/...` path whose file is
 * missing from `public/`, so the loader's error path works deterministically.
 */
function strictAssets404() {
  return {
    name: 'strict-assets-404',
    configureServer(server: { middlewares: { use: (fn: (req: { url?: string }, res: { statusCode: number; end: () => void }, next: () => void) => void) => void } }) {
      server.middlewares.use((req, res, next) => {
        const url = req.url ?? '';
        const match = url.match(/^\/assets\/[^?#]+/);
        if (match !== null) {
          // URL path is percent-encoded (e.g. "Gold%20Mine"); the real
          // filesystem entry has a literal space. Decode before existsSync,
          // otherwise every asset whose path contains a space is 404'd
          // even though Vite's static handler would happily serve it.
          let decoded: string;
          try {
            decoded = decodeURIComponent(match[0]);
          } catch {
            decoded = match[0];
          }
          const filePath = `public${decoded}`;
          if (!existsSync(filePath)) {
            res.statusCode = 404;
            res.end();
            return;
          }
        }
        next();
      });
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '');
  return {
    plugins: [react(), strictAssets404()],
    server: {
      host: '127.0.0.1',
      port: Number(env.CLIENT_PORT) || 4445,
      strictPort: true,
      fs: { strict: true, allow: ['.'] },
    },
    preview: { host: '127.0.0.1', port: Number(env.CLIENT_PORT) || 4445, strictPort: true },
  };
});
