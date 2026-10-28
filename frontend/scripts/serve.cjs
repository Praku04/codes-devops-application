/**
 * Start `ng serve` on the port from the repo-root .env (FRONTEND_PORT).
 *
 * The port cannot just live in the npm script, because the backend's CORS allowlist is
 * derived from the same FRONTEND_PORT — hardcoding it in two places is how you get a
 * dev server the API refuses to talk to.
 *
 * Extra args are passed through, so `npm start -- --open` still works.
 */
const { spawn } = require('child_process');
const path = require('path');
const { readEnv } = require('./read-env.cjs');

const { frontendPort, configuredApiUrl, apiPort, publicHost, frontendHost, apiUrlByHost } =
  readEnv();
console.log(
  `[env] ng serve on ${frontendHost}:${frontendPort} -> ` +
    (configuredApiUrl ? `API ${configuredApiUrl}` : `API <page host>:${apiPort}`)
);
// Per-host overrides (API_URL_BY_HOST), so a proxied deployment can be checked from the log
// instead of the browser's network tab.
for (const [host, url] of Object.entries(apiUrlByHost)) {
  console.log(`[env] ${host} -> API ${url}`);
}
console.log(
  `[env] open http://localhost:${frontendPort}/` +
    (publicHost ? `  or  http://${publicHost}:${frontendPort}/` : '')
);
console.log('');

const ng = path.join(__dirname, '..', 'node_modules', '@angular', 'cli', 'bin', 'ng.js');
const args = [
  ng,
  'serve',
  '--port',
  String(frontendPort),
  '--host',
  String(frontendHost),
  // Vite (Angular's dev server since v17) rejects requests whose Host header it does not
  // recognise, which on EC2 surfaces as "Blocked request" instead of the app. `true`
  // accepts any Host, which is what makes one command work on localhost and on a public
  // IP without naming either. Safe enough for a dev server that should not be public
  // anyway; `npm run build` + nginx is the answer for real deployments.
  '--allowed-hosts',
  'true',
  ...process.argv.slice(2),
];

// stdio inherit keeps Angular's live rebuild output and its colours intact.
const child = spawn(process.execPath, args, { stdio: 'inherit' });
child.on('exit', (code, signal) => {
  // Mirror the child's fate so Ctrl-C and CI failures propagate correctly.
  if (signal) process.kill(process.pid, signal);
  else process.exit(code ?? 0);
});
