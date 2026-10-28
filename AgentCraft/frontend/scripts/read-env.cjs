/**
 * Read the repo-root .env so the Angular app and the dev server use the same ports
 * as the backend.
 *
 * The browser cannot read .env — it is a server-side file — so the values have to be
 * baked in before the app is compiled. `write-env.cjs` turns them into a TypeScript
 * file at build time; `serve.cjs` uses them to pick the dev-server port.
 *
 * Deliberately dependency-free: this runs before Angular starts, and pulling `dotenv`
 * into the frontend just to parse five lines is not worth the install.
 */
const fs = require('fs');
const path = require('path');

const ROOT_ENV = path.join(__dirname, '..', '..', '.env');
const EXAMPLE_ENV = path.join(__dirname, '..', '..', '.env.example');

/** Defaults, kept in step with backend/app/core/config.py. */
const DEFAULTS = { API_PORT: '8555', FRONTEND_PORT: '4225' };

function parse(file) {
  if (!fs.existsSync(file)) return {};
  const out = {};
  for (const raw of fs.readFileSync(file, 'utf8').split('\n')) {
    const line = raw.trim();
    // Skip blanks and comments — including the commented-out optional keys.
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq < 1) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    // Tolerate quoted values; .env files are written both ways.
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    if (value) out[key] = value;
  }
  return out;
}

/**
 * Effective config, in precedence order: real environment, then .env, then .env.example,
 * then the defaults above.
 *
 * The process environment wins so CI or a one-off `API_PORT=9001 npm start` works
 * without editing a file. .env.example is consulted so a fresh clone with no .env yet
 * still builds against the documented ports instead of failing.
 */
function readEnv() {
  const file = { ...parse(EXAMPLE_ENV), ...parse(ROOT_ENV) };
  const pick = (key) => process.env[key] || file[key] || DEFAULTS[key];

  const apiPort = pick('API_PORT');
  const frontendPort = pick('FRONTEND_PORT');
  // Host the *browser* should use to reach the API. On a laptop that is loopback, but on a
  // remote box (EC2) loopback is wrong in a way that is easy to miss: the bundle runs in
  // the visitor's browser, so 127.0.0.1 points at their machine, not the server. Set
  // PUBLIC_HOST to the EC2 public IP or DNS name and every URL below follows.
  const publicHost = pick('PUBLIC_HOST') || '';
  // Only pinned when explicitly configured. Left empty — the normal case — the app resolves
  // the API host from `window.location` at run time (see src/app/api-base.ts), so one
  // build works on localhost and on a server without knowing either address up front.
  //
  // AGENTCRAFT_API_URL wins over PUBLIC_HOST: it is a full URL, for an API behind a proxy
  // or on a scheme/port this pair cannot express.
  // FRONTEND_URL / BACKEND_URL — the pair a deployment hands over ("the UI is served here, the
  // API is served there"). Kept as their own keys rather than folded into the ones above because
  // that is what an infrastructure team writes down, and a rename is a config file nobody edits.
  const frontendUrl = (pick('FRONTEND_URL') || '').trim();
  const backendUrl = (pick('BACKEND_URL') || '').trim().replace(/\/+$/, '');
  // AGENTCRAFT_API_URL wins over PUBLIC_HOST: it is a full URL, for an API behind a proxy
  // or on a scheme/port this pair cannot express.
  //
  // BACKEND_URL only pins the API globally when there is no FRONTEND_URL to scope it against.
  // With both set the pairing is conditional instead (see apiUrlByHost below) — that is the
  // difference between "the API is at X" and "the UI at Y talks to the API at X", and it is what
  // lets the deployed domains and a local `npm start` coexist in one configuration.
  const configuredApiUrl = (
    pick('AGENTCRAFT_API_URL') ||
    (backendUrl && !frontendUrl ? backendUrl : '') ||
    (publicHost ? `http://${publicHost}:${apiPort}` : '')
  ).replace(/\/+$/, '');
  // What to print in logs, where "resolved at run time" is less useful than a concrete URL.
  const apiUrl = configuredApiUrl || `http://${publicHost || '127.0.0.1'}:${apiPort}`;
  // Dev-server bind address. Defaults to 0.0.0.0 when PUBLIC_HOST is set, because a server
  // listening only on localhost is unreachable from outside the box — the usual reason
  // `npm start` on EC2 "works" in the log and refuses connections in the browser.
  // Bind 0.0.0.0 by default. A dev server on localhost only is unreachable from outside the
  // box — the usual reason `npm start` on EC2 logs a healthy start and refuses connections.
  // Binding all interfaces still serves localhost, so this costs a local run nothing.
  const frontendHost = pick('FRONTEND_HOST') || '0.0.0.0';
  const apiUrlByHost = parseHostMap(pick('API_URL_BY_HOST'));
  // FRONTEND_URL + BACKEND_URL is the same statement as one API_URL_BY_HOST entry, so it becomes
  // one. An explicit API_URL_BY_HOST entry for the same host wins — it is the more specific key.
  const frontendUrlHost = hostOf(frontendUrl);
  if (frontendUrlHost && backendUrl && !apiUrlByHost[frontendUrlHost]) {
    apiUrlByHost[frontendUrlHost] = backendUrl;
  }

  return {
    apiPort,
    frontendPort,
    apiUrl,
    configuredApiUrl,
    publicHost,
    frontendHost,
    apiUrlByHost,
    frontendUrl,
    backendUrl,
  };
}

/**
 * Host (with port, if the URL carries a non-default one) of a configured URL, lower-cased.
 *
 * Tolerates a bare `host` or `host:port` with no scheme, because half of all hand-written config
 * omits it — `new URL('example.com')` throws, so guessing https first is friendlier than failing.
 */
function hostOf(url) {
  const raw = String(url || '').trim();
  if (!raw) return '';
  for (const candidate of [raw, `https://${raw}`]) {
    try {
      return new URL(candidate).host.toLowerCase();
    } catch {
      /* try the next spelling */
    }
  }
  return '';
}

/**
 * `API_URL_BY_HOST` — "which API does *this* address of the UI talk to".
 *
 * Format: comma-separated `page-host=api-url` pairs, where the page host is what appears in the
 * browser's address bar (with its port, if the URL shows one):
 *
 *   API_URL_BY_HOST=agentcraft-poc.example.com=https://agent-craft-be.example.com
 *
 * Why this exists rather than just AGENTCRAFT_API_URL: that key *pins* the API for every way of
 * reaching the UI at once, so setting it for a deployed domain also redirects `localhost:4225`
 * and `<ec2-ip>:4225` — the two addresses the tmux stack is normally used through — at the public
 * backend. A map is conditional, so a deployment behind a proxy and the direct-port access used
 * during development can coexist in one build with one config.
 *
 * Safe to keep in `.env.example`, unlike a pin: an entry only ever applies to a browser that is
 * already on that exact host.
 */
function parseHostMap(raw) {
  const out = {};
  for (const pair of String(raw || '').split(',')) {
    const eq = pair.indexOf('=');
    if (eq < 1) continue;
    // Hosts are case-insensitive; the API URL is not, so only the key is lowered.
    const host = pair.slice(0, eq).trim().toLowerCase();
    const url = pair.slice(eq + 1).trim().replace(/\/+$/, '');
    if (host && url) out[host] = url;
  }
  return out;
}

module.exports = { readEnv };
