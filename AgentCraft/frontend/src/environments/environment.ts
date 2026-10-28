/**
 * GENERATED FILE — do not edit, and do not commit.
 *
 * Written by frontend/scripts/write-env.cjs from the repo-root .env on every
 * `npm start` and `npm run build`. Change API_PORT / PUBLIC_HOST in .env, not here.
 */
export const environment: {
  apiUrl: string;
  apiPort: number;
  publicHost: string;
  apiUrlByHost: Record<string, string>;
} = {
  /**
   * Pinned API URL from PUBLIC_HOST / AGENTCRAFT_API_URL, or '' when neither is set.
   *
   * Empty is the normal case and is not a missing value: api-base.ts then resolves the API
   * host from window.location, so one build works on localhost and on a server.
   */
  apiUrl: '',
  /** Port the API listens on — combined with the browser's hostname when apiUrl is ''. */
  apiPort: 8555,
  /** PUBLIC_HOST as configured, '' when unset. */
  publicHost: '',
  /**
   * API_URL_BY_HOST — per-address overrides, keyed by the host in the browser's address bar
   * (lower-cased, with the port only when the URL shows one). Usually empty.
   *
   * Consulted before the window.location rules, so a UI reached through a proxied domain can
   * point at a backend on a *different* domain while localhost and <ip>:PORT keep resolving
   * the way they always did. Unlike apiUrl this is conditional, so it cannot misdirect the
   * addresses it does not name.
   */
  apiUrlByHost: {"agentcraft-poc.bpsgentech.com":"https://agent-craft-be.bpsgentech.com"},
};
