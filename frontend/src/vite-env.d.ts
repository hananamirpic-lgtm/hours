/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Absolute base URL of the backend API, set at build time (e.g.
   * `https://hours-api.onrender.com/api`). Optional: when unset the client falls back to `/api`,
   * which local development and docker-compose serve through the Vite dev proxy.
   */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
