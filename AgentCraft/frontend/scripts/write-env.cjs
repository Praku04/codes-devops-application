/**
 * Generate src/environments/environment.ts from the repo-root .env.
 *
 * Runs before every `npm start` / `npm run build`, so the compiled bundle always points
 * at the port the backend is actually on. The generated file is gitignored — .env is the
 * source of truth, and committing it would let a stale port ship.
 */
const fs = require('fs');
const path = require('path');
const { readEnv } = require('./read-env.cjs');
const { checkNode } = require('./check-node.cjs');
const { checkNativeBindings } = require('./check-native.cjs');

// First, before any other output: on too old a Node nothing below can succeed, and the reason
// has to be the first thing on screen rather than buried under `[env]` lines and npm warnings.
checkNode();

// Then the tree: a node_modules installed on an older Node has no bundler binary for this
// platform, and `ng serve` reports that as a MODULE_NOT_FOUND deep inside vite.
checkNativeBindings();

const { apiUrl, configuredApiUrl, apiPort, publicHost, apiUrlByHost } = readEnv();
const dir = path.join(__dirname, '..', 'src', 'environments');
const target = path.join(dir, 'environment.ts');

const contents = `/**
 * GENERATED FILE — do not edit, and do not commit.
 *
 * Written by frontend/scripts/write-env.cjs from the repo-root .env on every
 * \`npm start\` and \`npm run build\`. Change API_PORT / PUBLIC_HOST in .env, not here.
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
  apiUrl: '${configuredApiUrl}',
  /** Port the API listens on — combined with the browser's hostname when apiUrl is ''. */
  apiPort: ${Number(apiPort)},
  /** PUBLIC_HOST as configured, '' when unset. */
  publicHost: '${publicHost}',
  /**
   * API_URL_BY_HOST — per-address overrides, keyed by the host in the browser's address bar
   * (lower-cased, with the port only when the URL shows one). Usually empty.
   *
   * Consulted before the window.location rules, so a UI reached through a proxied domain can
   * point at a backend on a *different* domain while localhost and <ip>:PORT keep resolving
   * the way they always did. Unlike apiUrl this is conditional, so it cannot misdirect the
   * addresses it does not name.
   */
  apiUrlByHost: ${JSON.stringify(apiUrlByHost)},
};
`;

fs.mkdirSync(dir, { recursive: true });
// Only write when the content changed: `ng serve` watches this file, and rewriting it
// byte-identical on every start would trigger a pointless rebuild.
// Say which of the two modes is in effect: a pinned URL, or resolved per request from the
// browser's own address. The difference matters when debugging a deployment.
const how = configuredApiUrl
  ? `API ${configuredApiUrl}`
  : `API <page host>:${apiPort} (resolved in the browser; local default ${apiUrl})`;
// Name the per-host overrides too. They apply to nobody but the address they list, so during a
// deployment the useful question is not "is a map configured" but "is *this* host in it".
for (const [host, url] of Object.entries(apiUrlByHost)) {
  console.log(`[env] ${host} -> API ${url}`);
}
if (!fs.existsSync(target) || fs.readFileSync(target, 'utf8') !== contents) {
  fs.writeFileSync(target, contents, 'utf8');
  console.log(`[env] environment.ts -> ${how}`);
} else {
  console.log(`[env] environment.ts already current -> ${how}`);
}
