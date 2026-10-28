import { environment } from '../environments/environment';

/**
 * Base URL of the AgentCraft API, resolved in the browser.
 *
 * The API host cannot be baked in at build time and still work in more than one place: the
 * bundle runs in the *visitor's* browser, so a compiled-in `127.0.0.1` points at their
 * machine rather than the server. Deploying the same build to EC2 used to mean every API
 * call died against the visitor's own loopback.
 *
 * So take the host from `window.location` — whatever the user actually typed to reach the
 * UI — and swap in the API port. One build then works on localhost, on a LAN address, and
 * on a public IP, with nothing to reconfigure between them.
 *
 * `environment.apiUrl` (from AGENTCRAFT_API_URL / PUBLIC_HOST in .env) overrides this when
 * set, for the cases location cannot describe: an API on another host, behind a proxy, or
 * on a different scheme. `environment.apiUrlByHost` (API_URL_BY_HOST) does the same for one
 * address at a time, which is what a deployed domain needs without disturbing the others.
 */
function resolveApiBase(): string {
  // SSR / tests have no window. Nothing below can be decided without an address, so take the
  // pinned URL if there is one and loopback otherwise.
  if (typeof window === 'undefined' || !window.location?.hostname) {
    return environment.apiUrl
      ? environment.apiUrl.replace(/\/+$/, '')
      : `http://127.0.0.1:${environment.apiPort}`;
  }

  const { protocol, hostname, port, host } = window.location;

  // A per-address override (API_URL_BY_HOST, or the FRONTEND_URL/BACKEND_URL pair) is checked
  // *first* — ahead of the global pin — because it is the more specific statement: "the UI at
  // this address talks to that API", against `environment.apiUrl`'s "every address talks to that
  // API". A deployment where the UI and the API sit on two different proxied domains needs the
  // former, and it must not stop working because someone also set PUBLIC_HOST.
  //
  // The other half of the reason: a pin redirects `localhost:4225` and `<ec2-ip>:4225` — the
  // addresses the tmux stack is used through — at the deployed backend as a side effect. A map
  // entry cannot, since it only ever matches the host it names.
  //
  // `host` includes the port when the URL shows one, so `ui.example.com` and `ui.example.com:4225`
  // can be mapped separately; falling back to `hostname` means one entry covers every port.
  const mapped =
    environment.apiUrlByHost?.[host.toLowerCase()] ??
    environment.apiUrlByHost?.[hostname.toLowerCase()];
  if (mapped) {
    return mapped.replace(/\/+$/, '');
  }

  // Then a global pin (AGENTCRAFT_API_URL, or PUBLIC_HOST) — see .env.example.
  if (environment.apiUrl) {
    return environment.apiUrl.replace(/\/+$/, '');
  }

  // Served through a reverse proxy on the standard ports (no :4225 in the URL), the API is
  // almost certainly behind the same proxy — a guessed :8555 would be closed. Use a
  // same-origin relative base and let the proxy route /api.
  //
  // When it is *not* behind the same proxy — a separate API domain — that assumption is wrong
  // and every call 404s against the UI's own origin. API_URL_BY_HOST above is the fix for that
  // deployment shape; there is nothing in the page's own URL that could reveal it.
  if (!port || port === '80' || port === '443') {
    return '';
  }

  // Keep the page's scheme: an https UI calling an http API is blocked as mixed content.
  return `${protocol}//${hostname}:${environment.apiPort}`;
}

/** Resolved once at startup — the location cannot change without a reload. */
export const API_BASE = resolveApiBase();

/**
 * The same URL for error messages.
 *
 * `API_BASE` is deliberately `''` in the same-origin/proxy case, which reads as a blank in
 * "is the API running on ?" — so name the page's own origin there instead.
 */
export const API_BASE_LABEL =
  API_BASE || (typeof window !== 'undefined' ? window.location.origin : 'this host');
