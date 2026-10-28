/**
 * Refuse to start when `node_modules` has no native bundler binary for *this* machine.
 *
 * Angular 22 serves through Vite, and Vite 8 bundles with rolldown — a Rust binary shipped as
 * one compiled npm package per platform (`@rolldown/binding-linux-x64-gnu`, `…-win32-x64-msvc`,
 * and thirteen more), all declared as **optional** dependencies.
 *
 * Two ways to end up with none of them, and both leave a `node_modules` that looks complete:
 *
 *   1. The install ran on too old a Node. Every one of those packages declares
 *      `engines: ^20.19.0 || >=22.12.0`, and npm silently skips an *optional* dependency whose
 *      engines do not match — that is what the `EBADENGINE` wall during a Node 18 install is
 *      saying. Upgrading Node afterwards does not backfill them: npm sees a satisfied tree.
 *   2. `node_modules` was copied (or a volume mounted) from a different OS — a Windows tree has
 *      only `binding-win32-x64-msvc`, which is useless on Linux.
 *
 * Either way `ng serve` dies with a MODULE_NOT_FOUND for a path *inside* node_modules:
 *
 *     Cannot find module '../rolldown-binding.linux-x64-gnu.node'
 *     at .../node_modules/vite/node_modules/rolldown/dist/shared/binding-TuFFIE_J.mjs
 *
 * — thirty lines of `node:internal/modules/cjs/loader` frames that never once mention npm, Node
 * versions or optional dependencies. Hence this check: the tree is the problem, and rebuilding it
 * is the whole fix.
 *
 * Required from `write-env.cjs`, which `start`, `build` and `watch` all run first.
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');

// rolldown arrives both top-level (Angular's own dep) and nested under vite, and either copy can
// be the one that loads. A binding in *any* of them proves the install fetched platform binaries.
const SEARCH = [
  path.join(ROOT, 'node_modules', '@rolldown'),
  path.join(ROOT, 'node_modules', 'vite', 'node_modules', '@rolldown'),
];

/** Every `binding-*` package present in the tree, by name (`linux-x64-gnu`, `win32-x64-msvc`, …). */
function installedBindings() {
  const found = new Set();
  for (const dir of SEARCH) {
    let entries;
    try {
      entries = fs.readdirSync(dir);
    } catch {
      continue; // no @rolldown here at all
    }
    for (const name of entries) {
      if (!name.startsWith('binding-')) continue;
      // A directory alone is not enough — the `.node` file is what gets required, and an
      // interrupted install can leave the folder with only a package.json in it.
      let files = [];
      try {
        files = fs.readdirSync(path.join(dir, name));
      } catch {
        continue;
      }
      if (files.some((f) => f.endsWith('.node'))) found.add(name.replace(/^binding-/, ''));
    }
  }
  return [...found];
}

/** What this machine needs, as it appears in the package name. */
function wantedBinding() {
  const arch = process.arch;
  if (process.platform === 'win32') return `win32-${arch}-msvc`;
  if (process.platform === 'darwin') return `darwin-${arch}`;
  if (process.platform === 'linux') {
    // glibc reports a runtime version here; musl (Alpine) does not.
    const glibc = process.report?.getReport?.()?.header?.glibcVersionRuntime;
    return `linux-${arch}-${glibc ? 'gnu' : 'musl'}`;
  }
  return `${process.platform}-${arch}`;
}

/**
 * Platform+arch match only — the gnu/musl half is left out on purpose. Getting it wrong would
 * block a working Alpine tree, and if it really is the wrong libc the loader says so clearly.
 */
function matches(binding, wanted) {
  const [plat, arch] = wanted.split('-');
  return binding.startsWith(`${plat}-${arch}`);
}

function checkNativeBindings() {
  // Nothing installed yet is a different problem, with its own clear message from npm/`ng`.
  if (!fs.existsSync(path.join(ROOT, 'node_modules', 'vite'))) return;

  const wanted = wantedBinding();
  const found = installedBindings();
  if (found.some((b) => matches(b, wanted))) return;

  const install = fs.existsSync(path.join(ROOT, 'package-lock.json')) ? 'npm ci' : 'npm install';

  const lines = [
    '',
    `[deps] node_modules has no native bundler binary for this machine (needs ${wanted}).`,
    found.length
      ? `[deps] It has ${found.join(', ')} instead — this tree was installed on another OS.`
      : '[deps] It has none at all, for any platform — the sign of an install run on too old a Node.',
    '',
    "[deps] Vite's bundler (rolldown) is a compiled binary, shipped as one *optional* npm package",
    '[deps] per platform. npm skips an optional dependency whose `engines` do not match the Node',
    '[deps] running the install, and every one of them needs Node ^20.19 || >=22.12 — so an',
    '[deps] install done on Node 18 leaves a tree that looks complete and dies in `ng serve` with',
    `[deps]   Cannot find module '../rolldown-binding.${wanted}.node'`,
    '',
    '[deps] Upgrading Node does not fix it on its own: npm sees a satisfied tree and adds nothing.',
    '[deps] The tree has to be rebuilt, once, on the new Node:',
    '',
    `           node --version                     # ${process.version} — must be >= v22.12`,
    `           rm -rf node_modules && ${install}`,
    '           npm start',
    '',
  ];
  console.error(lines.join('\n'));
  process.exit(1);
}

module.exports = { checkNativeBindings, installedBindings, wantedBinding, matches };
