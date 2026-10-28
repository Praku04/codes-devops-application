/**
 * Offline checks for the Files-tab markdown preview — no browser, no test runner.
 *
 * Run it as:
 *
 *     node scripts/verify-markdown-preview.mjs
 *
 * How it avoids testing a copy of the code: it lifts the real method bodies out of
 * `wizard.component.ts` between two markers, drops them into a class with the four things
 * they touch stubbed (`sanitizer`, `architectureViews`, `architectureUrls`, `normalizePath`),
 * compiles that with the project's own `tsc`, and runs it. So a change to `formatMarkdown`
 * that breaks a table is caught here, and a rename of what it is extracted from fails loudly
 * rather than passing against stale code.
 *
 * What it proves:
 *
 * 1. A GFM pipe table becomes a real `<table>` — the actual bug the user reported, where
 *    `| § | Section |` reached the pane as text.
 * 2. `####` and deeper are headings, not literal hashes (SDD.md numbers §3.2.1 at four).
 * 3. `[text](url)` links render, and a `javascript:`/`data:`/protocol-relative URL does not
 *    survive — this HTML goes through `bypassSecurityTrustHtml`, so this is the only gate.
 * 4. `![alt](docs/architecture/logical-view.png)` becomes an `<img>` once the blob is in
 *    hand, and a named placeholder before that — carrying the title that says a click opens it
 *    in the diagram viewer, and still wrapped in the `<p>` the full-width CSS rule targets.
 * 5. Escaped pipes inside a cell stay inside that cell instead of shifting the columns.
 * 6. The real SDD.md renders with no literal table pipes or hash headings left over.
 *
 * Exits non-zero on the first failure, and prints one line per check either way.
 */

import { execFileSync } from 'node:child_process';
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');
const component = join(root, 'src', 'app', 'wizard', 'wizard.component.ts');
const tmp = join(root, '.md-preview-check');

/** The placeholder delimiter `formatMarkdown` uses internally — none may reach the HTML. */
const HOLD = String.fromCharCode(1);

const failures = [];
function check(label, condition, detail = '') {
  if (condition) {
    console.log(`  ok    ${label}`);
    return;
  }
  failures.push(label);
  console.log(`  FAIL  ${label}${detail ? ` — ${detail}` : ''}`);
}

/** The source text between two markers, or a hard failure naming what moved. */
function slice(src, startMarker, endMarker) {
  const a = src.indexOf(startMarker);
  const b = src.indexOf(endMarker, a + 1);
  if (a === -1 || b === -1) {
    throw new Error(
      `Cannot find ${a === -1 ? startMarker : endMarker} in wizard.component.ts — ` +
        'this script extracts the real methods, so update the markers here too.'
    );
  }
  return src.slice(a, b);
}

// CRLF normalised: the markers below are written with \n, and the component is CRLF on disk.
const src = readFileSync(component, 'utf8').replace(/\r\n/g, '\n');
const consts = slice(src, 'const MD_HOLD = String.fromCharCode(1);', '\ninterface BriefSection');
const methods = slice(src, '  /**\n   * A URL a generated document may link to', '\n  toggleBriefSection(');

const harness = `type SafeHtml = string;

${consts}

class Preview {
  sanitizer = { bypassSecurityTrustHtml: (html: string): SafeHtml => html };
  architectureUrls: Record<string, string | undefined> = {};
  readonly architectureViews: ReadonlyArray<{ kind: string; label: string; path: string }> = [
    { kind: 'logical', label: 'Logical View', path: 'docs/architecture/logical-view.png' },
    { kind: 'development', label: 'Development View', path: 'docs/architecture/development-view.png' },
    { kind: 'deployment', label: 'Deployment View', path: 'docs/architecture/deployment-view.png' },
  ];
  fetches = 0;
  private ensureArchitectureImages(): void {
    this.fetches++;
  }
  private normalizePath(path: string): string {
    return path.replace(/\\\\/g, '/').replace(/^\\/+/, '');
  }

${methods}
}

export { Preview };
`;

rmSync(tmp, { recursive: true, force: true });
mkdirSync(tmp, { recursive: true });
writeFileSync(join(tmp, 'preview.ts'), harness, 'utf8');
// Marks the compiled output as ESM so importing it does not warn about a typeless package.
writeFileSync(join(tmp, 'package.json'), '{ "type": "module" }\n', 'utf8');
// The compiler is invoked as a script rather than through `npx`: Node refuses to spawn a
// `.cmd` without a shell, and going through a shell to reach a file that is already local is
// a needless quoting problem.
execFileSync(
  process.execPath,
  [
    join(root, 'node_modules', 'typescript', 'bin', 'tsc'),
    join(tmp, 'preview.ts'),
    '--target',
    'es2022',
    '--module',
    'es2022',
    // The app's tsconfig would otherwise be picked up and refuse a file list on the command
    // line; the harness needs nothing from it.
    '--ignoreConfig',
  ],
  { cwd: root, stdio: 'inherit' }
);
const { Preview } = await import(`file://${join(tmp, 'preview.js').replace(/\\/g, '/')}`);
const p = new Preview();
const md = (text) => String(p.formatMarkdown(text));

console.log('1. Pipe tables become tables');
const table = md(
  ['| § | Section |', '| --- | --- |', '| 1 | Project Summary |', '| 2 | Scope |'].join('\n')
);
check('a <table> is emitted', table.includes('<table class="md-table">'));
check('the header row is a <th>', table.includes('<th>Section</th>'));
check('both body rows are <tr>', (table.match(/<tr>/g) || []).length === 3, table);
check('no literal pipe survives', !table.includes('|'), table);
check('the separator row is not a body row', !table.includes('---'));
const aligned = md(['| A | B | C |', '| :-- | :-: | --: |', '| 1 | 2 | 3 |'].join('\n'));
check('centre alignment is applied', aligned.includes('style="text-align:center"'));
check('right alignment is applied', aligned.includes('style="text-align:right"'));
const escaped = md(['| Cell | Note |', '| --- | --- |', '| a \\| b | one cell |'].join('\n'));
check('an escaped pipe stays in its cell', escaped.includes('<td>a | b</td>'), escaped);
check('the escaped row still has two cells', (escaped.match(/<td/g) || []).length === 2, escaped);
const notATable = md('The value is | maybe | not a table');
check('pipes without a separator stay a paragraph', notATable.startsWith('<p>'), notATable);

console.log('2. Headings deeper than ###');
const heads = md('# One\n## Two\n### Three\n#### 3.2.1 Logical View\n##### Five\n###### Six');
check('#### renders as a heading', heads.includes('3.2.1 Logical View</h6>'), heads);
check('no literal hashes are left', !heads.includes('#'), heads);

console.log('2b. Heading anchors — what the table of contents links to');
check(
  'the heading carries GitHub\'s own id',
  heads.includes('<h6 id="321-logical-view">'),
  heads
);
// The backend hard-codes these anchors in the TOC, so the slug rule has to agree exactly —
// including the double hyphen where a dropped `&` leaves two spaces behind.
for (const [text, id] of [
  ['3.6 AI Guardrails & Data Security', '36-ai-guardrails--data-security'],
  ['3.4 Setup and Configuration/Migration Requirements', '34-setup-and-configurationmigration-requirements'],
  ['3.4.1 Hardware, Software, and Access Requirements', '341-hardware-software-and-access-requirements'],
  ['2.3 Non-Functional Requirements', '23-non-functional-requirements'],
]) {
  check(`"${text}" → #${id}`, md(`### ${text}`).includes(`id="${id}"`), md(`### ${text}`));
}

console.log('3. Links, and the URLs that are refused');
check('an http link renders', md('see [docs](https://example.com/a)').includes('href="https://example.com/a"'));
check('an anchor link renders', md('[top](#section-1)').includes('href="#section-1"'));
check('a relative path renders', md('[SDD](SDD.md)').includes('href="SDD.md"'));
for (const bad of [
  'javascript:alert(1)',
  'JaVaScRiPt:alert(1)',
  'data:text/html;base64,PHNjcmlwdD4=',
  'vbscript:msgbox',
  '//evil.example.com/x',
]) {
  const out = md(`click [here](${bad})`);
  check(`${bad} is refused`, !out.includes('href') && out.includes('here'), out);
}
check('a refused link keeps its text', md('[label](javascript:x)').includes('label'));
const inCode = md('the path `[a](https://evil.example.com)` is literal');
check('a link inside backticks stays literal', !inCode.includes('<a href'), inCode);
check('the code span survives', inCode.includes('<code>'), inCode);
check('no placeholder leaks', !inCode.includes(HOLD), inCode);
const numbers = md('phase `x` covers 0 to 1 and 2 items');
check('a bare number is not mistaken for a placeholder', numbers.includes('0 to 1 and 2'), numbers);

console.log('4. Architecture figures');
const figure = '![Logical View — how it is layered](docs/architecture/logical-view.png)';
const pending = md(figure);
check('a missing blob renders a named placeholder', pending.includes('md-image-pending'), pending);
check('the placeholder names the view', pending.includes('Logical View'), pending);
check('a fetch was started', p.fetches > 0);
p.architectureUrls = { logical: 'blob:http://localhost/abc' };
const shown = md(figure);
check('the image renders once the blob is in hand', shown.includes('<img class="md-image"'), shown);
check('it points at the blob', shown.includes('src="blob:http://localhost/abc"'), shown);
check('the alt text is the caption', shown.includes('alt="Logical View — how it is layered"'), shown);
// The figure is fitted to the pane width and opens in the diagram viewer on click
// (`onPreviewClick` → `openFigureFromPreview`), so it has to say so on hover.
check('the figure advertises what a click does', shown.includes('title="Click to open'), shown);
// Why `.md-preview p:has(> .md-image)` exists: a figure on its own line is wrapped in a
// paragraph, and the prose rule caps a paragraph at 112ch. If this ever stops being a `<p>`,
// that CSS exemption is dead weight rather than the thing making the figure full width.
check('a lone figure is wrapped in a paragraph', shown.startsWith('<p><img'), shown);
const foreign = md('![x](https://example.com/x.png)');
check(
  'an image this client cannot fetch is a placeholder, not a broken icon',
  foreign.includes('md-image-pending') && !foreign.includes('<img'),
  foreign
);

/*
 * 4b. The two marks the PDF/DOCX export renders and this pane used to print literally.
 *
 * `backend/app/services/export/documents.py` parses the same markdown for both downloads, so a
 * rule it draws and the preview does not is a document that reads differently on paper than on
 * screen — which is the whole thing the export was asked to avoid.
 */
console.log('4b. Thematic breaks and single-underscore emphasis');
for (const mark of ['---', '----', '***', '___']) {
  const out = md(`Above\n\n${mark}\n\nBelow`);
  check(`\`${mark}\` becomes a rule`, out.includes('<hr class="md-rule" />'), out);
  check(`\`${mark}\` leaves no literal text`, !out.includes(`<p>${mark}`), out);
}
const starList = md('* * *');
check('`* * *` is a rule, not a one-item list', !starList.includes('<li>'), starList);
const bulletStill = md('- one\n- two');
check('a real bullet list still renders', (bulletStill.match(/<li>/g) || []).length === 2, bulletStill);
const em = md('_Generated for Claude Code by AgentCraft._');
check('`_text_` is emphasis', em.includes('<em>Generated for Claude Code by AgentCraft.</em>'), em);
// The reason the rule is guarded: these documents are full of identifiers, and none is emphasis.
for (const literal of ['batch_service.py', 'snake_case_name', 'a __dunder__ name is bold not em']) {
  const out = md(literal);
  check(`\`${literal}\` keeps its underscores`, !out.includes('<em>'), out);
}
const emInCode = md('the field `created_at_utc` is a timestamp');
check('underscores inside a code span are untouched', !emInCode.includes('<em>'), emInCode);

console.log('5. The real SDD.md');
let sdd = '';
try {
  sdd = execFileSync(
    join(root, '..', 'backend', '.venv', 'Scripts', 'python.exe'),
    [
      '-c',
      [
        'import sys; sys.path.insert(0, ".")',
        'from app.core.state_machine import Platform',
        'from app.models.schemas import ProjectBrief, ProjectPlan, SourceFileSpec',
        'from app.services.deduction.solution_design import fallback_solution_design',
        'b = ProjectBrief(problem_statement="Trainers cannot see batch progress.", tech_stack="FastAPI, Angular")',
        'pl = ProjectPlan(project_name="TrainTrack", summary="Batch progress tracker.", source_tree=[SourceFileSpec(path=p, purpose=p) for p in ["main.py", "backend/api/routes/x.py", "backend/services/y.py"]])',
        'sys.stdout.write(fallback_solution_design(b, pl, Platform.CLAUDE_CODE))',
      ].join('\n'),
    ],
    { cwd: join(root, '..', 'backend'), encoding: 'utf8', env: { ...process.env, PYTHONIOENCODING: 'utf-8' } }
  );
} catch (err) {
  console.log(`  skip  could not render SDD.md (${err.message.split('\n')[0]})`);
}
if (sdd) {
  p.architectureUrls = { logical: 'blob:a', development: 'blob:b', deployment: 'blob:c' };
  const html = md(sdd);
  const tables = (html.match(/<table class="md-table">/g) || []).length;
  check('every table rendered', tables >= 8, `${tables} tables`);
  check('all three figures rendered', (html.match(/<img class="md-image"/g) || []).length === 3);
  check('no row of literal pipes is left', !/<p>\|/.test(html));
  check('no literal `| --- |` separator is left', !html.includes('| --- |'));
  check('no literal heading hashes are left', !/<p>#{1,6} /.test(html));
  check('no literal `---` separator is left', !/<p>-{3,}/.test(html));
  check('the section separators became rules', (html.match(/<hr class="md-rule" \/>/g) || []).length > 0);
  check('the TOC links resolved', html.includes('href="#321-logical-view"'), '');
  // The reported bug: clicking a TOC row did nothing, because no heading carried the id the
  // row pointed at. Every anchor in the real document must find its heading.
  const wanted = [...html.matchAll(/href="#([^"]+)"/g)].map((m) => m[1]);
  const ids = new Set([...html.matchAll(/ id="([^"]+)"/g)].map((m) => m[1]));
  const dangling = [...new Set(wanted)].filter((a) => !ids.has(a));
  check(`all ${wanted.length} TOC anchors point at a real heading`, dangling.length === 0, dangling.join(', '));
  check('no placeholder character leaked', !html.includes(HOLD));
  console.log(`  info  ${sdd.length} chars of markdown → ${html.length} chars of HTML`);
}

/*
 * 6. The stylesheet can actually reach what section 1–5 just built.
 *
 * This is the check that was missing while three "rendering" bugs were really one CSS bug. The
 * preview's DOM comes from `[innerHTML]`, so it carries no `_ngcontent-*` attribute, and
 * Angular's emulated encapsulation appends that attribute to *every* compound selector in a
 * chain. A rule written `.md-preview .md-table th` therefore matches nothing — the table is
 * built correctly and drawn unstyled. `::ng-deep` is what stops the scoping.
 *
 * So: every rule in the component's stylesheet that reaches *inside* a preview must say
 * `::ng-deep`. Asserted against the source, not the build, so it fails before a bundle exists.
 */
console.log('6. Preview rules pierce view encapsulation');
const css = readFileSync(join(root, 'src', 'app', 'wizard', 'wizard.component.css'), 'utf8');
// Selectors only: everything before a `{`, with comments and declaration blocks removed.
const selectors = css
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .split('}')
  .map((block) => block.split('{')[0])
  .flatMap((list) => list.split(','))
  .map((s) => s.trim())
  .filter(Boolean);
const unreachable = selectors.filter(
  (s) => /^\.md-preview\s+\S/.test(s) && !s.includes('::ng-deep')
);
check(
  'no `.md-preview <descendant>` rule is written without ::ng-deep',
  unreachable.length === 0,
  unreachable.join(' | ')
);
// And the classes `formatMarkdown` emits are all actually styled — a rule that is reachable but
// absent is the same blank table to the reader.
for (const cls of ['md-table', 'md-code', 'md-image', 'md-image-pending', 'md-frontmatter', 'md-rule']) {
  check(
    `.${cls} has a rule that can reach it`,
    new RegExp(`\\.md-preview\\s+::ng-deep\\s+[^,{]*\\.${cls}\\b`).test(css)
  );
}

rmSync(tmp, { recursive: true, force: true });

if (failures.length) {
  console.log(`\n${failures.length} check(s) failed:`);
  for (const label of failures) console.log(`  - ${label}`);
  process.exit(1);
}
console.log('\nAll checks passed.');
