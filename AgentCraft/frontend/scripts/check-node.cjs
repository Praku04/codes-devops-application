/**
 * Refuse to start on a Node version the Angular CLI will reject, and say how to fix it.
 *
 * Angular 22's CLI hard-requires Node >= 22.22.3, 24.15.0 or 26.0.0 and exits if it is given
 * anything else. Its own message is accurate but arrives *after* our `[env]` lines and after
 * npm's `EBADENGINE` wall, so on a box still on Node 18 it reads as though the app broke rather
 * than as though the runtime is too old — and it does not say *how* to get a newer Node on a
 * shared machine, which is the only part that is not obvious.
 *
 * Required from `write-env.cjs`, which every npm script here runs first (`start`, `build`,
 * `watch` all begin with `npm run env`), so one check covers all of them.
 */

// Angular only supports even (LTS) majors, each from a specific patch. Anything newer than the
// last entry is assumed fine — a new major appears long before this file is next touched.
const FLOORS = {
  22: [22, 22, 3],
  24: [24, 15, 0],
  26: [26, 0, 0],
};

function parse(version) {
  return String(version).replace(/^v/, '').split('.').map((n) => parseInt(n, 10) || 0);
}

function isSupported(version) {
  const [major, minor, patch] = parse(version);
  if (major > 26) return true;
  const floor = FLOORS[major];
  if (!floor) return false; // below 22, or an odd/unsupported major
  const [, fMinor, fPatch] = floor;
  if (minor !== fMinor) return minor > fMinor;
  return patch >= fPatch;
}

function checkNode() {
  if (isSupported(process.versions.node)) return;

  const wanted = Object.values(FLOORS).map((f) => `v${f.join('.')}`).join(', ');
  const lines = [
    '',
    `[node] Node ${process.version} is too old for this UI — the Angular CLI needs ${wanted} or newer.`,
    '[node] Nothing here will run until Node is upgraded. The dependencies are fine; the runtime is not.',
    '',
    '[node] nvm is the safe way on a shared box — it is per-user, so it cannot break another',
    '[node] project that still needs the old Node. Copy the whole block:',
    '',
    '           curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash',
    '           export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh"',
    // `--lts` rather than a major: it always lands above these floors, whereas `nvm install 22`
    // resolves to the newest 22.x, which is only good enough while that is >= 22.22.3.
    '           nvm install --lts && nvm use --lts && nvm alias default "lts/*"',
    '           node --version                     # must be one of the versions above',
    '           cd frontend && rm -rf node_modules && npm ci && npm start',
    '',
    '[node] Already in a tmux pane? `nvm use` only changes the shell that ran it. After',
    '[node] `nvm alias default`, run `exec bash` in this pane (or restart the session with',
    '[node] `./run-tmux.sh stop && ./run-tmux.sh`) so it picks the new Node up.',
    '',
    '[node] Or system-wide, if this box is only used for AgentCraft:',
    '',
    '           curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash -',
    '           sudo apt-get install -y nodejs',
    '',
    `[node] Check with: node --version   (currently ${process.version})`,
    '',
  ];
  console.error(lines.join('\n'));
  process.exit(1);
}

module.exports = { checkNode, isSupported };
