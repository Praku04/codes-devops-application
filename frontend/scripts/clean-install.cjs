const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');
const { checkNode } = require('./check-node.cjs');

// Before deleting anything: on too old a Node the reinstall would succeed and the app still
// would not run, so the wasted download is not the worst of it — throwing away a working
// node_modules to end up in the same place is. `npm run reinstall` is the one entry point here
// that does not go through `npm run env`, so it needs its own check.
checkNode();

const root = path.join(__dirname, '..');
const nm = path.join(root, 'node_modules');
const lock = path.join(root, 'package-lock.json');

console.log('Stop "npm start" first if reinstall fails with EPERM.\n');

if (fs.existsSync(nm)) {
  try {
    fs.rmSync(nm, { recursive: true, force: true });
    console.log('Removed node_modules');
  } catch (err) {
    console.warn('Could not remove node_modules (dev server may be running):', err.message);
    console.warn('Continuing with npm install — stop ng serve and rerun if install fails.');
  }
}
if (fs.existsSync(lock)) {
  fs.unlinkSync(lock);
  console.log('Removed package-lock.json');
}

execSync('npm install', { cwd: root, stdio: 'inherit' });
