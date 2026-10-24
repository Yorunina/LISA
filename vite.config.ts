import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  optimizeDeps: { include: ['@tiptap/react', '@tiptap/starter-kit'] },
  server: {
    proxy: Object.fromEntries(['/api', '/read'].map(path => [path, loadEnv(mode, '.', 'LISA_API_PROXY').LISA_API_PROXY || 'http://127.0.0.1:8000'])),
  },
}));
