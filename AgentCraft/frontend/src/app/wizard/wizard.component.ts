import { CommonModule } from '@angular/common';
import { ChangeDetectorRef, Component, OnDestroy, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { ActivatedRoute, Router } from '@angular/router';
import { Observable, Subscription, finalize, interval, switchMap, timeout } from 'rxjs';
import { ApiService } from '../services/api.service';
import { AuthService } from '../services/auth.service';
import {
  ARCHITECTURE_RENDER_SCALE,
  ARCHITECTURE_VIEWS,
  DIAGRAM_VIEWS,
  DiagramViewerComponent,
} from '../shared/diagram-viewer/diagram-viewer.component';
import {
  ArchitectureKind,
  DiagramKind,
  DiagramRevision,
  DiagramScope,
  DiagramSetView,
  DocumentFormat,
  InterviewQuestion,
  PlatformInfo,
  ProcessStep,
  ProjectPlan,
  ProjectState,
  ProjectStatus,
  UploadedDocument,
  isArchitectureKind,
  isDiagramKind,
} from '../models';

type UiStep =
  | 'start'
  | 'docs'
  | 'interview'
  | 'blueprint'
  | 'platform'
  | 'generate'
  | 'review'
  | 'export';
type ReviewTab = 'agents' | 'skills' | 'rules' | 'mcp' | 'diagrams' | 'files';

/** Sentinel stored as the answer when a user skips an optional question. */
const SKIP_ANSWER = '—';

/**
 * The two generated documents, named once.
 *
 * Mirrors `WORK_BREAKDOWN_FILENAME` in backend/app/services/deduction/work_breakdown.py
 * and `SDD_FILENAME` in backend/app/services/deduction/solution_design.py. Neither is a
 * `source_tree` entry — the exporters emit them beside README.md — so they are matched
 * by name in several places here and a typo in any one of them silently stops the
 * incremental reload for that document.
 */
const WBS_FILENAME = 'WORKBREAKDOWN.md';
const SDD_FILENAME = 'SDD.md';

/**
 * Placeholder delimiter used while `formatMarkdown` holds code spans aside.
 *
 * A control character, because the marker has to be something the document itself cannot
 * contain: the text is HTML-escaped before any of this runs, so no author can write one.
 */
const MD_HOLD = String.fromCharCode(1);
const MD_HOLD_RE = new RegExp(`${MD_HOLD}(\\d+)${MD_HOLD}`, 'g');

interface BriefSection {
  title: string;
  body: string;
  /** Lazily rendered markdown — built when a section opens. */
  html?: SafeHtml;
}

export interface FileTreeNode {
  name: string;
  path?: string;
  children?: FileTreeNode[];
  expanded: boolean;
  /**
   * Full folder path ("backend/app/api") — the identity the open/closed state is
   * keyed on, so a rebuilt tree restores what the user had open. Undefined on files.
   */
  dir?: string;
}

/**
 * One visible line of the file tree.
 *
 * The tree used to render by re-entering a recursive `<ng-template>` once per folder
 * depth, so opening a folder instantiated a whole nested stack of embedded views and
 * every change-detection pass walked all of them. Flattening to the rows that are
 * actually on screen makes the template a single `@for`: expanding a folder splices a
 * few rows in and Angular's track-by leaves every other row's DOM untouched.
 */
export interface FileTreeRow {
  node: FileTreeNode;
  /** Stable identity for @for track — a path is unique across the whole tree. */
  key: string;
  depth: number;
  isFolder: boolean;
}

@Component({
  selector: 'ac-wizard',
  standalone: true,
  imports: [CommonModule, FormsModule, DiagramViewerComponent],
  templateUrl: './wizard.component.html',
  styleUrl: './wizard.component.css',
})
export class WizardComponent implements OnInit, OnDestroy {
  project: ProjectStatus | null = null;
  platforms: PlatformInfo[] = [];
  step: UiStep = 'start';
  projectName = 'My Agent Workspace';
  statement = '';
  files: File[] = [];
  dragOver = false;
  currentAnswer = '';
  /** Project title captured on the interview goal step (mirrors the docs flow). */
  interviewTitle = '';
  /** When set, interview shows this question index (for Back within interview). */
  interviewIndex: number | null = null;
  selectedPlatform: string | null = null;
  busy = false;
  error = '';
  previewFiles: string[] = [];
  /**
   * The workspace paths that are images, not text — the blueprint PNGs under `docs/diagrams/`.
   *
   * They are in `previewFiles` (the tree lists them and the zip holds them) but never in
   * `fileContents`, so the file pane has to know which rows open a viewer instead of an editor.
   */
  previewImages: string[] = [];
  fileContents: Record<string, string> = {};
  selectedFile: string | null = null;

  /**
   * The textarea's model. Assigning it also refreshes the preview snapshot, so callers
   * cannot leave the two panes disagreeing.
   */
  get selectedFileBody(): string {
    return this._selectedFileBody;
  }

  set selectedFileBody(value: string) {
    this._selectedFileBody = value;
    // Typing must not re-parse the file: `filePreviewBody` only moves while the preview
    // is the visible pane, and `toggleFileEditMode` re-syncs it on the way back.
    if (!this.fileEditMode) this.filePreviewBody = value;
  }

  private _selectedFileBody = '';

  /**
   * Body the markdown preview renders — a snapshot, not the live textarea model.
   *
   * The preview div stays in the DOM across an Edit toggle (see `toggleFileEditMode`), so
   * binding it straight to the model would re-parse the whole file on every keystroke —
   * ~463 KB of HTML for the biggest WORKBREAKDOWN.md in this repo.
   */
  filePreviewBody = '';

  /**
   * Which SDD.md download is in flight, so both buttons disable together — the server lays the
   * document out on the request, and firing the second one on top of the first would queue a
   * second render for no gain. Null when nothing is downloading.
   */
  sddDownloading: DocumentFormat | null = null;
  sddDownloadMessage = '';
  draftPlan: ProjectPlan | null = null;
  reviewTab: ReviewTab = 'agents';
  saveMessage = '';
  /**
   * True once a persisted plan has been seen — "has anything been saved yet?", not a
   * snapshot to diff against.
   *
   * It used to hold `JSON.stringify(draftPlan)`, but every reader only tested it for
   * truthiness (`planDirty` does the actual change tracking). Serialising the whole plan
   * — every agent prompt, skill body and scaffold file — was pure work, and the 4-second
   * WORKBREAKDOWN poll redid it on each tick, which is what made clicks during
   * generation feel sticky.
   */
  private planSaved = false;

  get hasSavedPlan(): boolean {
    return this.planSaved;
  }
  apiOk = false;
  expandedIndex: number | null = null;
  editingSummary = false;
  expandingBrief = false;
  /** Goal saved with a plain statement — server is tailoring follow-up questions. */
  tailoringQuestions = false;
  briefExpanded = false;
  editingExpandedBrief = false;
  briefSections: BriefSection[] = [];
  /** Live parallel expand progress chips */
  expandParallelStatus: { key: string; title: string; state: 'pending' | 'done' | 'fail' }[] = [];
  expandDoneCount = 0;
  /** Which expanded-brief accordion rows are open (multi-select). */
  openBriefSections = new Set<number>([0]);
  /** Per-card section edit (interview expanded brief). */
  editingBriefSectionIndex: number | null = null;
  editingBriefSectionBody = '';
  fileTree: FileTreeNode[] = [];
  fileEditMode = false;
  contentEditMode = false;
  fileRefreshMessage = '';
  filesPreviewBusy = false;
  wbsGenerating = false;
  wbsMessage = '';
  fileCopyMessage = '';
  private wbsGenerationAttempted = false;
  private sddGenerationAttempted = false;
  /**
   * Which document the single poll is currently watching.
   *
   * WORKBREAKDOWN.md and SDD.md are generated one after the other, never together —
   * the API refuses to start one while the other is in flight because both write the
   * same plan row, and the loser's `file_overrides` would drop the winner's document.
   * So one `wbsPollSub` / `wbsGenerating` / `wbsMessage` triple serves both, and this
   * says which one the status line and the incremental reload are talking about.
   */
  private docLabel = WBS_FILENAME;
  private wbsPollSub?: Subscription;
  docsPhase: '' | 'checking' = '';
  fileRejectMessage = '';
  readonly maxBodyLines = 49;
  // Mirrors ALLOWED_EXTENSIONS in backend/app/services/parser/documents.py. The two lists have
  // to agree: an extension the picker accepts and the parser rejects is a file the user watches
  // upload and then sees fail, with the reason arriving from the server.
  readonly allowedUploadExts = [
    '.pdf', '.docx', '.md', '.markdown', '.txt',
    '.csv', '.tsv', '.xlsx', '.xlsm', '.xls',
    '.png', '.jpg', '.jpeg',
  ];
  readonly examplePlaceholder =
    'Describe the product or problem in a few sentences. This must match the documents you upload.';

  private pollSub?: Subscription;
  private healthSub?: Subscription;

  // ── Process blueprint (SIPOC / flow / swimlane) ────────────────────────────
  blueprint: DiagramSetView | null = null;
  /** True while the first GET is in flight — separate from `busy` so it can't lock the step. */
  blueprintLoading = false;
  /** True while a draw or a follow-up runs on the server. */
  blueprintDrawing = false;
  blueprintMessage = '';
  blueprintError = '';
  blueprintFollowup = '';
  /**
   * Whether the next follow-up rewrites the process (all three views) or re-draws only the view
   * being looked at.
   *
   * Stored as the *mode* rather than the resolved scope so the "only this view" option follows
   * the tab strip: pick it on the SIPOC, switch to the swimlane, and the button still says what
   * it will actually do. The resolved value is `followupScope`.
   */
  followupScopeMode: 'all' | 'view' = 'all';
  /**
   * The scope of the follow-up currently running, or null when nothing is. Read by the spinner —
   * a redraw of one view and a rewrite of the whole process take very different amounts of time
   * and have very different consequences, so the wait should not describe them identically.
   */
  runningScope: DiagramScope | null = null;
  /** Scope of the last submitted follow-up, so the redrawn set lands on the view that changed. */
  private lastFollowupScope: DiagramScope = 'all';
  /**
   * SIPOC lands first, and a redraw returns here.
   *
   * It is the first view in the method — suppliers and customers before steps — and it is the
   * one that fits a pane whole, so it is what orients someone who has just watched three
   * diagrams appear. The flow and the swimlane are one click away.
   */
  blueprintTab: DiagramKind = 'sipoc';
  /**
   * The three views, for the empty state's "here is what will be drawn" tiles. The same list the
   * viewer labels its tab strip from, so the promise and the result use one wording.
   */
  readonly diagramViews = DIAGRAM_VIEWS;
  /**
   * Object URLs for the three PNGs.
   *
   * The images cannot be bound to a bare `<img src>`: auth is a Bearer header applied by the
   * HTTP interceptor, so a browser-issued image request would arrive unauthenticated and 401.
   * They are fetched as blobs and revoked in `releaseDiagramUrls()` — an un-revoked object URL
   * pins its blob for the lifetime of the document.
   */
  diagramUrls: Partial<Record<DiagramKind, string>> = {};

  /**
   * SDD.md §3.2's three architectural views, keyed by the workspace path the document links to.
   *
   * Mirrors `ARCH_VIEWS` / `ARCH_WORKSPACE_PATHS` in
   * `backend/app/services/diagrams/architecture.py`. The markdown preview resolves
   * `![…](docs/architecture/logical-view.png)` through this list, so a rename on either side
   * shows up as an unresolved figure rather than a silently broken image.
   */
  readonly architectureViews: ReadonlyArray<{ kind: ArchitectureKind; label: string; path: string }> =
    [
      { kind: 'logical', label: 'Logical View', path: 'docs/architecture/logical-view.png' },
      { kind: 'development', label: 'Development View', path: 'docs/architecture/development-view.png' },
      { kind: 'deployment', label: 'Deployment View', path: 'docs/architecture/deployment-view.png' },
    ];
  /**
   * The same three views in the shape `ac-diagram-viewer` takes them.
   *
   * Shared with the admin console rather than declared here: both show these figures, so the
   * labels and the sentences describing them come from one list.
   */
  readonly architectureViewerViews = ARCHITECTURE_VIEWS;
  /** Supersample factor the architecture PNGs are drawn at — see the shared constant. */
  readonly architectureRenderScale = ARCHITECTURE_RENDER_SCALE;
  /** Object URLs for the architecture PNGs — same Bearer-header reason as `diagramUrls`. */
  architectureUrls: Partial<Record<ArchitectureKind, string>> = {};
  private architectureFetched = false;
  /**
   * Which view the Diagrams tab's architecture strip is on.
   *
   * Separate from the Files pane, where the selected row is the choice — the two viewers are
   * shown in different places and switching one has no business moving the other.
   */
  architectureTab: ArchitectureKind = 'logical';
  /** A file asked for before the Files pane had loaded — see `openWorkspaceFile`. */
  private pendingFileSelection: string | null = null;

  /**
   * The version being looked at from the change history, or `null` for the current one.
   *
   * Old PNGs are not kept — only the views needed to redraw them — so clicking a history entry
   * asks the server to re-render that version with today's renderer. The three URLs live here
   * rather than in `diagramUrls` so leaving history is a revoke, not a refetch of the current
   * set, and so the tabs keep working while a past version is on screen.
   */
  historyVersion: number | null = null;
  historyUrls: Partial<Record<DiagramKind, string>> = {};
  historyLoading = false;
  historyError = '';
  /** Free-text filter over the full step list. Matches name, id, actor, systems and detail. */
  stepFilter = '';
  private blueprintPollSub?: Subscription;
  /**
   * True once the user has explicitly moved past the blueprint step this visit.
   *
   * Deliberately not persisted: the blueprint is an artifact of CONTEXT_READY, not a state of
   * its own (that would have changed the transition table every existing project and the CLI
   * depend on), so reopening a project that has not chosen an IDE yet lands back here — where
   * the frozen blueprint is on screen and Continue is one click away.
   */
  private blueprintPassed = false;

  readonly steps: { id: UiStep; label: string }[] = [
    { id: 'start', label: 'Start' },
    { id: 'docs', label: 'Context' },
    { id: 'blueprint', label: 'Blueprint' },
    { id: 'platform', label: 'IDE' },
    { id: 'generate', label: 'Generate' },
    { id: 'review', label: 'Review' },
    { id: 'export', label: 'Export' },
  ];

  constructor(
    private api: ApiService,
    public auth: AuthService,
    private sanitizer: DomSanitizer,
    private route: ActivatedRoute,
    private router: Router,
    private cdr: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    this.pingApi();
    this.healthSub = interval(12000).subscribe(() => this.pingApi());
    this.api.platforms().subscribe({
      next: (p) => {
        this.platforms = p;
        this.apiOk = true;
        this.cdr.detectChanges();
      },
      error: () => undefined,
    });

    const id = this.route.snapshot.paramMap.get('id');
    if (id) {
      this.api.getProject(id).subscribe({
        next: (p) => {
          this.project = p;
          this.projectName = p.name;
          this.wbsGenerationAttempted = false;
          this.sddGenerationAttempted = false;
          this.wbsMessage = '';
          this.apiOk = true;
          this.syncStepFromState();
          if (p.state === 'READY_FOR_REVIEW' || p.state === 'EXPORTED') {
            this.hydrateDraft();
            this.loadFilePreview();
            if (this.docGenerating(p.progress)) {
              this.wbsGenerating = true;
              this.wbsMessage = p.progress || '';
              this.docLabel = p.progress?.includes('WORKBREAKDOWN')
                ? WBS_FILENAME
                : SDD_FILENAME;
              this.startWbsPoll();
            }
          }
          this.cdr.detectChanges();
        },
        error: (err) => {
          this.error = err?.error?.message || 'Could not open session';
          void this.router.navigateByUrl('/sessions');
        },
      });
    }
  }

  ngOnDestroy(): void {
    this.pollSub?.unsubscribe();
    this.wbsPollSub?.unsubscribe();
    this.blueprintPollSub?.unsubscribe();
    this.healthSub?.unsubscribe();
    this.releaseDiagramUrls();
    this.stopDocsTimer();
  }

  private pingApi(): void {
    this.api
      .health()
      .pipe(timeout(8000))
      .subscribe({
        next: () => {
          this.apiOk = true;
          // Clear a stale offline banner once the API answers. Matched by prefix rather
          // than the exact old string, which named a hardcoded port — the port now comes
          // from .env, so an equality check would silently stop matching when it changes.
          if (this.error.startsWith('API offline')) {
            this.error = '';
          }
          this.cdr.detectChanges();
        },
        error: () => {
          this.apiOk = false;
          this.cdr.detectChanges();
        },
      });
  }

  get interviewQuestions() {
    return this.project?.interview || [];
  }

  /** Answered or explicitly skipped — either way the user is done with it. */
  isQuestionSettled(q: InterviewQuestion | null | undefined): boolean {
    return !!q && !!(q.answer || '').trim();
  }

  /** Answer that only carries the skip sentinel (— / -). */
  isQuestionSkipped(q: InterviewQuestion | null | undefined): boolean {
    const text = (q?.answer || '').trim();
    return !!text && text.replace(/[—–\-\s]/g, '') === '';
  }

  get activeInterviewIndex(): number {
    const qs = this.interviewQuestions;
    if (!qs.length) return -1;
    const goalIdx = qs.findIndex((q) => q.id === 'goal');
    if (goalIdx >= 0 && !this.isQuestionSettled(qs[goalIdx])) {
      return goalIdx;
    }
    if (this.interviewIndex != null && this.interviewIndex >= 0 && this.interviewIndex < qs.length) {
      return this.interviewIndex;
    }
    const pending = qs.findIndex((q) => q.id !== 'goal' && !this.isQuestionSettled(q));
    if (pending >= 0) return pending;
    // Everything is settled (e.g. after Back from IDE selection) — show the goal
    // so answers stay visible and editable instead of dead-ending the step.
    return goalIdx >= 0 ? goalIdx : 0;
  }

  /** Nothing left to ask — offer an explicit Continue instead of auto-advancing. */
  get interviewAllSettled(): boolean {
    const qs = this.interviewQuestions;
    return qs.length > 0 && qs.every((q) => this.isQuestionSettled(q));
  }

  get activeInterviewQuestion() {
    const i = this.activeInterviewIndex;
    return i >= 0 ? this.interviewQuestions[i] : null;
  }

  get canInterviewPrev(): boolean {
    return this.activeInterviewIndex > 0;
  }

  get canWizardBack(): boolean {
    const s = this.project?.state;
    // After agents/skills/rules are generated, lock the flow — no rollback.
    if (!s || s === 'CREATED' || s === 'GENERATING' || s === 'READY_FOR_REVIEW' || s === 'EXPORTED') {
      return false;
    }
    return true;
  }

  get interviewProgress(): number {
    const qs = this.project?.interview || [];
    if (!qs.length) return 0;
    const goal = qs.find((q) => q.id === 'goal');
    if (!this.isQuestionSettled(goal)) return 0;
    const followups = qs.filter((q) => q.id !== 'goal');
    if (!followups.length) return 100;
    const done = followups.filter((q) => this.isQuestionSettled(q)).length;
    return Math.round(40 + (done / followups.length) * 60);
  }

  get goalAnswered(): boolean {
    return this.isQuestionSettled(this.interviewQuestions.find((q) => q.id === 'goal'));
  }

  get canSkipCurrentQuestion(): boolean {
    const q = this.activeInterviewQuestion;
    return !!q && q.id !== 'goal';
  }

  /** Follow-ups only (goal excluded) — used for the "Question n of m" counter. */
  get followupQuestions(): InterviewQuestion[] {
    return this.interviewQuestions.filter((q) => q.id !== 'goal');
  }

  get followupPosition(): number {
    const q = this.activeInterviewQuestion;
    if (!q || q.id === 'goal') return 0;
    return this.followupQuestions.findIndex((f) => f.id === q.id) + 1;
  }

  get remainingFollowups(): number {
    return this.followupQuestions.filter((q) => !this.isQuestionSettled(q)).length;
  }

  get interviewHint(): string {
    if (!this.goalAnswered) {
      return 'Problem statement is required. Expand it into a detailed brief to skip the rest, or save it plainly to get a few project-specific questions.';
    }
    const total = this.followupQuestions.length;
    if (!total) {
      return 'Problem statement saved — continuing to the process blueprint.';
    }
    const left = this.remainingFollowups;
    if (!left) {
      return 'All questions handled — continuing to the process blueprint.';
    }
    return `Tailored to your project — ${left} of ${total} left, and every one is skippable.`;
  }

  /** Label + spinner state for the primary interview button. */
  get interviewSubmitLabel(): string {
    if (this.busy) {
      return this.activeInterviewQuestion?.id === 'goal' ? 'Preparing questions…' : 'Saving…';
    }
    return this.activeInterviewQuestion?.id === 'goal' ? 'Save & continue' : 'Save & next';
  }

  private markdownCache = new Map<string, SafeHtml>();
  /** Source text each cached render was built from — the cache's invalidation check. */
  private markdownStamps = new Map<string, string>();

  /**
   * Cached markdown render — avoids re-parsing on every click/change detection.
   *
   * Keyed per slot on the whole body, not a length-plus-prefix fingerprint: an edit that
   * left both unchanged (fixing a typo, swapping a word for one the same length) hashed
   * to the same key, so flipping back to Preview redisplayed the pre-edit HTML. Only the
   * current body is kept per key, so the map holds one entry per visible slot instead of
   * growing with every distinct edit.
   */
  renderMarkdown(key: string, src: string | null | undefined): SafeHtml {
    const body = src || '';
    if (this.markdownStamps.get(key) === body) {
      const hit = this.markdownCache.get(key);
      if (hit) return hit;
    }
    const html = this.formatMarkdown(body);
    this.markdownCache.set(key, html);
    this.markdownStamps.set(key, body);
    return html;
  }

  private clearMarkdownCache(): void {
    this.markdownCache.clear();
    this.markdownStamps.clear();
  }

  /**
   * True for paths that are IDE artifacts rather than application scaffold.
   *
   * Mirrors `_is_ide_or_readme` in the backend renderer. Both sides must agree or the
   * Files badge and the README's "Source files" disagree by a few entries.
   */
  private isIdeOrReadmePath(path: string): boolean {
    const p = this.normalizePath(path);
    // .gitignore and CLAUDE.md are rendered from the plan by the exporter, not scaffolded,
    // so they belong to the workspace count and never to the source count.
    const generated = [
      'README.md',
      'README.agentcraft.md',
      'AGENTS.md',
      '.mcp.json',
      '.gitignore',
      'CLAUDE.md',
    ];
    if (generated.includes(p)) return true;
    return ['.cursor/', '.claude/', '.windsurf/', '.agents/', '.github/instructions/', '.github/agents/', '.github/skills/'].some((pre) => p.startsWith(pre)) || p === '.github/copilot-instructions.md';
  }

  /**
   * Application scaffold count — the **Source files** figure, and the Files tab badge.
   *
   * Excludes IDE artifacts the same way the exporter does, so this equals the README's
   * "Source files" exactly. Deliberately smaller than `workspaceFileCount`.
   */
  get sourceFileCount(): number {
    return (this.draftPlan?.source_tree || []).filter(
      (f) => (f?.path || '').trim() && !this.isIdeOrReadmePath(f.path)
    ).length;
  }

  /**
   * Every path in the workspace — scaffold **plus** the IDE folder, README,
   * WORKBREAKDOWN.md and SDD.md. This is what the tree lists and what the zip holds, so it is
   * always larger than `sourceFileCount`. Straight from the server's preview.
   */
  get workspaceFileCount(): number {
    return this.previewFiles.length;
  }

  private normalizePath(path: string): string {
    return path.replace(/\\/g, '/').replace(/^\/+/, '');
  }

  /** Files tab badge — the scaffold count, matching the README's "Source files". */
  get filesTabCount(): number {
    return this.sourceFileCount;
  }

  private visiblePreviewFiles(): string[] {
    // The tree browses the whole workspace (IDE folder + scaffold); the badge stays
    // source-only so it matches the README. Both numbers are labelled in the UI.
    return this.previewFiles;
  }

  /**
   * Rebuild the tree from the current workspace paths.
   *
   * Folders the user opened stay open — `buildFileTree` reads `openDirs`. Before that,
   * every reload (including the 4-second WORKBREAKDOWN poll) handed back an all-closed
   * tree, so a folder opened a moment earlier collapsed on its own.
   */
  private rebuildFileTree(): void {
    this.fileTree = this.buildFileTree(this.visiblePreviewFiles());
    this.invalidateTreeRows();
  }

  lineCount(text: string | undefined | null): number {
    if (!text) return 0;
    return text.split(/\r?\n/).length;
  }

  lineChipClass(text: string | undefined | null): string {
    const n = this.lineCount(text);
    if (n > this.maxBodyLines) return 'warn';
    if (n <= 24) return 'ok';
    return '';
  }

  setRuleGlobs(rule: { globs: string[] }, raw: string): void {
    rule.globs = raw
      .split(',')
      .map((g) => g.trim())
      .filter(Boolean);
    this.markPlanDirty();
  }

  /** Expand the current interview answer into a detailed brief (interview path only). */
  expandInterviewAnswer(): void {
    const seed = this.currentAnswer.trim();
    if (!seed) {
      this.error = 'Type an answer first, then expand it into a detailed brief.';
      this.cdr.detectChanges();
      return;
    }
    this.cdr.detectChanges();
    void this.runExpandBriefParallel(seed, (expanded) => {
      this.currentAnswer = expanded;
    });
  }

  private async runExpandBriefParallel(
    seed: string,
    apply: (expanded: string) => void,
  ): Promise<void> {
    this.expandingBrief = true;
    this.error = '';
    this.saveMessage = '';
    this.briefExpanded = false;
    this.editingBriefSectionIndex = null;
    this.editingBriefSectionBody = '';
    this.expandDoneCount = 0;
    this.expandParallelStatus = [];
    try {
      const res = await this.api.expandBriefParallel(seed, (ev) => {
        if (ev.event === 'start' && ev.sections?.length) {
          this.expandParallelStatus = ev.sections.map((key) => ({
            key,
            title: key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
            state: 'pending' as const,
          }));
        }
        if (ev.event === 'section' && ev.section) {
          const row = this.expandParallelStatus.find((s) => s.key === ev.section);
          if (row) {
            row.state = ev.ok === false ? 'fail' : 'done';
            this.expandDoneCount = this.expandParallelStatus.filter((s) => s.state !== 'pending').length;
          }
        }
        this.cdr.detectChanges();
      });
      const expanded = (res.expanded || '').trim();
      const sections = this.parseBriefSections(expanded);
      const structured = sections.length >= 2 || /^##\s+/m.test(expanded);
      if (!expanded || !structured) {
        this.error =
          'Expand did not return Product / Goals / Stack sections. Check Bedrock credentials and try again.';
        return;
      }
      if (expanded === seed.trim() && !/^##\s+/m.test(seed)) {
        this.error = 'Expand returned the same text. Retry Expand to detailed brief.';
        return;
      }
      apply(expanded);
      this.briefSections = sections.map((s) => ({ ...s, html: undefined }));
      this.briefExpanded = true;
      this.editingExpandedBrief = false;
      this.openBriefSections = new Set([0]);
      this.ensureBriefSectionHtml(0);
      this.saveMessage = `Detailed brief ready · ${sections.length} sections`;
    } catch (err: unknown) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: string }).message)
          : 'Could not expand brief';
      this.error = msg;
    } finally {
      this.expandingBrief = false;
      this.cdr.detectChanges();
    }
  }

  get canExpandInterviewAnswer(): boolean {
    return this.activeInterviewQuestion?.id === 'goal';
  }

  /** Restore expanded-brief UI when reopening a saved structured goal. */
  private hydrateExpandedBriefFromAnswer(): void {
    const text = (this.currentAnswer || '').trim();
    if (!text || this.activeInterviewQuestion?.id !== 'goal') {
      if (!this.goalAnswered) {
        this.briefExpanded = false;
        this.briefSections = [];
      }
      return;
    }
    const sections = this.parseBriefSections(text);
    const structured = sections.length >= 2 || /^##\s+/m.test(text);
    if (!structured) {
      this.briefExpanded = false;
      this.briefSections = [];
      return;
    }
    this.briefSections = sections.map((s) => ({ ...s, html: undefined }));
    this.briefExpanded = true;
    this.openBriefSections = new Set([0]);
    this.ensureBriefSectionHtml(0);
  }

  private ensureBriefSectionHtml(index: number): void {
    const sec = this.briefSections[index];
    if (!sec || sec.html) return;
    this.briefSections = this.briefSections.map((s, i) =>
      i === index ? { ...s, html: this.formatMarkdown(s.body) } : s,
    );
  }

  private ensureAllBriefSectionHtml(): void {
    this.briefSections = this.briefSections.map((s) => ({
      ...s,
      html: s.html ?? this.formatMarkdown(s.body),
    }));
  }

  parseBriefSections(md: string): BriefSection[] {
    const text = (md || '').trim();
    if (!text) return [];
    // Prefer ## sections; also accept single-# headings as a fallback
    let parts = text.split(/^##\s+/m).filter((p) => p.trim());
    if (parts.length <= 1 && !text.startsWith('##')) {
      parts = text.split(/^#\s+/m).filter((p) => p.trim());
      if (parts.length <= 1 && !/^#\s+/m.test(text)) {
        return [{ title: 'Brief', body: text }];
      }
    }
    return parts.map((chunk) => {
      const lines = chunk.split(/\r?\n/);
      const title = (lines.shift() || 'Section').trim();
      const body = lines.join('\n').trim();
      return { title, body };
    });
  }

  /**
   * A click inside a rendered document — makes its own table of contents work.
   *
   * `SDD.md` opens with 26 links to its own headings. Left to the browser, each one would
   * append `#321-logical-view` to the wizard's route (a router navigation, not a scroll) and
   * move nothing, because the heading is inside a pane that scrolls rather than the page.
   * So the fragment is resolved here, against that pane, and only that pane is scrolled.
   *
   * It also handles a click on one of §3.2's figures: those are fitted to the pane width, and
   * the click opens the same picture in the diagram viewer at whatever size the reader wants.
   */
  onPreviewClick(ev: MouseEvent): void {
    const el = ev.target as HTMLElement | null;
    const figure = el?.closest?.('img.md-image') as HTMLImageElement | null;
    if (figure) {
      if (this.openFigureFromPreview(figure)) ev.preventDefault();
      return;
    }
    const anchor = el?.closest?.('a[href]') as HTMLAnchorElement | null;
    const href = anchor?.getAttribute('href') ?? '';
    if (!anchor || !href.startsWith('#') || href.length < 2) {
      return;
    }
    ev.preventDefault();
    const pane = anchor.closest('.md-preview') as HTMLElement | null;
    if (!pane) {
      return;
    }
    // Matched by scanning rather than with `querySelector('#' + id)`: these ids start with a
    // digit (`321-logical-view`), which is not a valid CSS selector without escaping.
    const id = decodeURIComponent(href.slice(1));
    const target = Array.from(pane.querySelectorAll<HTMLElement>('[id]')).find((n) => n.id === id);
    if (!target) {
      return;
    }
    // Not `scrollIntoView`: that also scrolls every ancestor, so jumping to §7 dragged the
    // whole wizard page down with it. Moving this one scroller keeps the tree and the toolbar
    // where they are. The 8px is so the heading is not flush against the pane's top edge.
    pane.scrollTop += target.getBoundingClientRect().top - pane.getBoundingClientRect().top - 8;
  }

  /**
   * A URL a generated document may link to, or null to drop the link and keep the text.
   *
   * The result of `formatMarkdown` goes through `bypassSecurityTrustHtml`, so Angular does no
   * checking of its own and this is the only gate: an allowlist of `http(s)`, `mailto`, an
   * in-page anchor, or a repo-relative path. Anything with another scheme — `javascript:`,
   * `data:`, `vbscript:` — and anything protocol-relative (`//host`) is refused, as is any URL
   * carrying whitespace or a quote, which is what would let it escape the attribute.
   *
   * The text arriving here has already been HTML-escaped, so `&` reads as `&amp;`; it is
   * unescaped for the test and re-escaped for the attribute.
   */
  private safeHref(raw: string): string | null {
    const url = (raw || '').trim().replace(/&amp;/g, '&');
    if (!url || url.startsWith('//') || /["'\s<>`\\]/.test(url)) return null;
    const ok =
      /^https?:\/\/[^/]/i.test(url) ||
      /^mailto:[^@\s]+@[^@\s]+$/i.test(url) ||
      /^#[\w-]+$/.test(url) ||
      // Repo-relative: `docs/architecture/logical-view.png`, `./x`, `../x`, `/x`. A colon is
      // excluded by the character class, so no scheme can slip through as a "path".
      /^\.{0,2}\/?[\w.+-]+(\/[\w.+-]+)*(#[\w-]+)?$/.test(url);
    return ok ? url.replace(/&/g, '&amp;') : null;
  }

  /**
   * Workspace image path → an object URL this client can actually display.
   *
   * Only SDD §3.2's three figures resolve: they are the only images a generated document
   * embeds, and their bytes have to come through an authenticated request (see
   * `ensureArchitectureImages`). Everything else returns null and renders as a named
   * placeholder rather than a broken-image icon.
   */
  private resolveImageSrc(src: string): string | null {
    const path = this.normalizePath((src || '').split('#')[0].trim());
    const view = this.architectureViews.find((v) => v.path === path);
    if (!view) return null;
    if (!this.architectureUrls[view.kind]) {
      // First figure to render pulls all three. Async, and the arrival clears the markdown
      // cache, so this render shows the placeholder and the next one shows the picture.
      this.ensureArchitectureImages();
      return null;
    }
    return this.architectureUrls[view.kind] || null;
  }

  /** `![alt](src)` → an `<img>` when the bytes are in hand, else the figure's name. */
  private imageTag(alt: string, src: string): string {
    const resolved = this.resolveImageSrc(src);
    const label = (alt || src).trim();
    const attr = label.replace(/"/g, '&quot;');
    if (resolved) {
      // Fitted to the pane width by CSS, and clickable: `onPreviewClick` opens the same bytes
      // in the diagram viewer, where fit / 100% / pan / full screen live. The title says so —
      // an image that does something on click and does not admit it is a trap.
      return (
        `<img class="md-image" src="${resolved}" alt="${attr}" loading="lazy"` +
        ' title="Click to open this figure with zoom, fit, 100% and full screen">'
      );
    }
    return `<span class="md-image-pending" title="${attr}">${label} <em>(${src})</em></span>`;
  }

  /**
   * A heading's anchor id, by GitHub's rule — because that is the rule the document was
   * written against: the backend's table of contents hard-codes `#321-logical-view` for
   * `#### 3.2.1 Logical View`, so the id has to come out the same way here.
   *
   * Lowercase, entities back to characters, markdown markers and every other
   * non-word character dropped, spaces to hyphens — *without* collapsing runs, which is why
   * `3.6 AI Guardrails & Data Security` is `36-ai-guardrails--data-security` and not one
   * hyphen there.
   */
  private headingSlug(escaped: string): string {
    return escaped
      .replace(/&amp;/g, '&')
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>')
      .replace(/&quot;/g, '"')
      .replace(/&#39;/g, "'")
      .trim()
      .toLowerCase()
      // One filter, not a separate pass for `*` / backticks: markdown markers are
      // non-word characters, so the same rule that drops `&` and `/` drops them too.
      .replace(/[^\w\- ]/g, '')
      .replace(/ /g, '-');
  }

  /**
   * One markdown table row → its cells, honouring `\|` as a literal pipe.
   *
   * Hand-rolled rather than `split(/(?<!\\)\|/)`: the escape matters because the backend
   * escapes pipes inside cells (`_md_table` in `solution_design.py`), and a split that
   * ignored it would shift every later column in that row by one.
   */
  private tableCells(line: string): string[] {
    const row = line.trim();
    const cells: string[] = [];
    let cur = '';
    for (let i = 0; i < row.length; i++) {
      if (row[i] === '\\' && row[i + 1] === '|') {
        cur += '|';
        i++;
        continue;
      }
      if (row[i] === '|') {
        cells.push(cur);
        cur = '';
        continue;
      }
      cur += row[i];
    }
    cells.push(cur);
    // Leading and trailing border pipes produce one empty cell at each end.
    if (row.startsWith('|')) cells.shift();
    if (row.endsWith('|') && cells.length) cells.pop();
    return cells.map((c) => c.trim());
  }

  /** Escape + light markdown → SafeHtml for brief sections. */
  formatMarkdown(src: string | null | undefined): SafeHtml {
    let raw = (src || '').replace(/\r\n/g, '\n');
    let fmHtml = '';
    if (raw.startsWith('---')) {
      const end = raw.indexOf('\n---', 3);
      if (end !== -1) {
        const fm = raw.slice(3, end).trim();
        raw = raw.slice(end + 4).replace(/^\n+/, '');
        const fmEsc = fm
          .replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;');
        fmHtml = `<pre class="md-frontmatter">${fmEsc}</pre>`;
      }
    }
    const escaped = raw
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');

    const lines = escaped.split('\n');
    const htmlParts: string[] = [];
    let inList = false;
    let inOrdered = false;
    let inCode = false;
    let codeBuf: string[] = [];

    const flushList = () => {
      if (inList) {
        htmlParts.push('</ul>');
        inList = false;
      }
      if (inOrdered) {
        htmlParts.push('</ol>');
        inOrdered = false;
      }
    };

    const flushCode = () => {
      if (!inCode) return;
      htmlParts.push(`<pre class="md-code"><code>${codeBuf.join('\n')}</code></pre>`);
      codeBuf = [];
      inCode = false;
    };

    const inline = (text: string): string => {
      // Code spans are lifted out first: a path or a URL inside backticks has to stay
      // literal, and the link pass below would otherwise rewrite it into markup.
      const spans: string[] = [];
      const held = text.replace(/`([^`]+)`/g, (_m, code: string) => {
        spans.push(`<code>${code}</code>`);
        // A control character delimits the placeholder rather than a bare number in
        // spaces: prose is full of " 3 ", and a guessable marker would turn a page count
        // into somebody else's code span. Escaped text cannot forge one.
        return `${MD_HOLD}${spans.length - 1}${MD_HOLD}`;
      });
      return held
        .replace(/!\[([^\]]*)\]\(([^()\s]+)\)/g, (_m, alt: string, url: string) =>
          this.imageTag(alt, url)
        )
        .replace(/\[([^\]]+)\]\(([^()\s]+)\)/g, (_m, label: string, url: string) => {
          const href = this.safeHref(url);
          // A refused URL keeps its label — dropping the text as well would silently delete
          // a sentence, which is worse than losing the link.
          return href
            ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`
            : label;
        })
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/__(.+?)__/g, '<strong>$1</strong>')
        .replace(/\*([^*\n]+)\*/g, '<em>$1</em>')
        // `_italic_`, but only where the underscores stand alone. `snake_case_name` and
        // `batch_service.py` are everywhere in these documents and none of them is emphasis.
        .replace(/(?<![A-Za-z0-9_])_([^_\n]+)_(?![A-Za-z0-9_])/g, '<em>$1</em>')
        .replace(MD_HOLD_RE, (_m, i: string) => spans[Number(i)] ?? '');
    };

    /** A `| --- | :--: |` separator, which is what makes the line above it a header row. */
    const isDelimiterRow = (line: string | undefined): boolean =>
      !!line && /^\s*\|?[\s:-]*-[\s:|-]*\|?\s*$/.test(line) && line.includes('-');

    for (let li = 0; li < lines.length; li++) {
      const line = lines[li];
      if (/^\s*```/.test(line)) {
        if (inCode) {
          flushCode();
        } else {
          flushList();
          inCode = true;
          codeBuf = [];
        }
        continue;
      }
      if (inCode) {
        codeBuf.push(line);
        continue;
      }

      // GFM pipe table — a row followed by a `| --- |` separator.
      //
      // Without this every table in SDD.md and WORKBREAKDOWN.md reached the pane as literal
      // `| § | Section |` lines, which is what the reader sees as a broken document. The
      // separator is required, so a lone line that happens to contain pipes stays a paragraph.
      if (/^\s*\|/.test(line) && isDelimiterRow(lines[li + 1])) {
        flushList();
        const headers = this.tableCells(line);
        const aligns = this.tableCells(lines[li + 1]).map((c) =>
          /^:-+:$/.test(c) ? 'center' : /^-+:$/.test(c) ? 'right' : /^:-+$/.test(c) ? 'left' : ''
        );
        const align = (i: number) => (aligns[i] ? ` style="text-align:${aligns[i]}"` : '');
        const rows: string[] = [];
        li += 2;
        while (li < lines.length && /^\s*\|/.test(lines[li])) {
          const cells = this.tableCells(lines[li]);
          // Header count wins, as GFM does: a ragged row keeps the columns lined up instead
          // of pushing one cell of one row out past the header.
          rows.push(
            `<tr>${headers
              .map((_h, i) => `<td${align(i)}>${inline(cells[i] ?? '')}</td>`)
              .join('')}</tr>`
          );
          li++;
        }
        li--; // the loop's own increment steps over the line that ended the table
        const head = headers.map((h, i) => `<th${align(i)}>${inline(h)}</th>`).join('');
        htmlParts.push(
          `<div class="md-table-wrap"><table class="md-table">` +
            `<thead><tr>${head}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`
        );
        continue;
      }

      // A thematic break, tested before the bullet below or `* * *` reads as a one-item list.
      // SDD.md separates its sections with `---`, and without this branch each one reached the
      // pane as three literal dashes on a line of their own.
      if (/^ {0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$/.test(line)) {
        flushList();
        htmlParts.push('<hr class="md-rule" />');
        continue;
      }

      const bullet = line.match(/^\s*[-*]\s+(.*)$/);
      if (bullet) {
        if (inOrdered) {
          htmlParts.push('</ol>');
          inOrdered = false;
        }
        if (!inList) {
          htmlParts.push('<ul>');
          inList = true;
        }
        htmlParts.push(`<li>${inline(bullet[1])}</li>`);
        continue;
      }
      const numbered = line.match(/^\s*\d+\.\s+(.*)$/);
      if (numbered) {
        if (inList) {
          htmlParts.push('</ul>');
          inList = false;
        }
        if (!inOrdered) {
          htmlParts.push('<ol>');
          inOrdered = true;
        }
        htmlParts.push(`<li>${inline(numbered[1])}</li>`);
        continue;
      }
      flushList();
      if (!line.trim()) {
        continue; // skip blank lines — avoid huge vertical gaps
      }
      // `#{1,6}`, not `#{1,3}`: SDD.md numbers its architectural views at `#### 3.2.1`, and
      // the old bound left those four hashes on screen as text.
      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        const level = Math.min(heading[1].length + 2, 6);
        // The id is what makes `[Logical View](#321-logical-view)` land somewhere: a generated
        // document's table of contents links to its own headings, and without ids every one of
        // those 26 links was a no-op. `onPreviewClick` does the scrolling.
        const slug = this.headingSlug(heading[2]);
        htmlParts.push(`<h${level}${slug ? ` id="${slug}"` : ''}>${inline(heading[2])}</h${level}>`);
        continue;
      }
      // Blockquote
      const quote = line.match(/^\s*&gt;\s?(.*)$/);
      if (quote) {
        htmlParts.push(`<blockquote>${inline(quote[1])}</blockquote>`);
        continue;
      }
      htmlParts.push(`<p>${inline(line)}</p>`);
    }
    flushList();
    flushCode();
    return this.sanitizer.bypassSecurityTrustHtml(fmHtml + htmlParts.join(''));
  }

  toggleBriefSection(i: number, ev?: Event): void {
    ev?.stopPropagation();
    const next = new Set(this.openBriefSections);
    const opening = !next.has(i);
    if (next.has(i)) next.delete(i);
    else next.add(i);
    this.openBriefSections = next;
    this.cdr.detectChanges();
    if (opening) {
      requestAnimationFrame(() => {
        this.ensureBriefSectionHtml(i);
        this.cdr.detectChanges();
      });
    }
  }

  onBriefCardClick(i: number): void {
    if (this.isEditingBriefSection(i)) return;
    if (!this.isBriefSectionOpen(i)) {
      this.toggleBriefSection(i);
    }
  }

  isBriefSectionOpen(i: number): boolean {
    return this.openBriefSections.has(i);
  }

  expandAllBriefSections(ev?: Event): void {
    ev?.stopPropagation();
    this.openBriefSections = new Set(this.briefSections.map((_, i) => i));
    this.cdr.detectChanges();
    const buildNext = (idx: number) => {
      if (idx >= this.briefSections.length) return;
      this.ensureBriefSectionHtml(idx);
      this.cdr.detectChanges();
      if (idx + 1 < this.briefSections.length) {
        requestAnimationFrame(() => buildNext(idx + 1));
      }
    };
    requestAnimationFrame(() => buildNext(0));
  }

  collapseAllBriefSections(ev?: Event): void {
    ev?.stopPropagation();
    this.openBriefSections = new Set();
    this.cdr.detectChanges();
  }

  briefPreview(body: string): string {
    const text = (body || '').replace(/\s+/g, ' ').trim();
    if (!text) return 'Empty section — click Show to open.';
    return text.length > 160 ? `${text.slice(0, 160)}…` : text;
  }

  isEditingBriefSection(i: number): boolean {
    return this.editingBriefSectionIndex === i;
  }

  startEditBriefSection(i: number, ev?: Event): void {
    ev?.stopPropagation();
    const sec = this.briefSections[i];
    if (!sec) return;
    this.editingBriefSectionIndex = i;
    this.editingBriefSectionBody = sec.body || '';
    const next = new Set(this.openBriefSections);
    next.add(i);
    this.openBriefSections = next;
    this.cdr.detectChanges();
  }

  saveBriefSection(i: number, ev?: Event): void {
    ev?.stopPropagation();
    if (this.editingBriefSectionIndex !== i || !this.briefSections[i]) return;
    this.briefSections[i] = {
      ...this.briefSections[i],
      body: (this.editingBriefSectionBody || '').trim(),
      html: undefined,
    };
    this.syncAnswerFromBriefSections();
    this.editingBriefSectionIndex = null;
    this.editingBriefSectionBody = '';
    this.saveMessage = `Saved “${this.briefSections[i].title}” — markdown preview updated`;
    if (this.isBriefSectionOpen(i)) {
      requestAnimationFrame(() => {
        this.ensureBriefSectionHtml(i);
        this.cdr.detectChanges();
      });
    }
    this.cdr.detectChanges();
  }

  cancelEditBriefSection(ev?: Event): void {
    ev?.stopPropagation();
    this.editingBriefSectionIndex = null;
    this.editingBriefSectionBody = '';
    this.cdr.detectChanges();
  }

  /** Rebuild full interview answer markdown from section cards. */
  private syncAnswerFromBriefSections(): void {
    this.currentAnswer = this.briefSections
      .map((s) => `## ${s.title}\n\n${(s.body || '').trim()}`.trim())
      .join('\n\n');
  }

  startEditExpandedBrief(ev?: Event): void {
    ev?.stopPropagation();
    this.editingBriefSectionIndex = null;
    this.editingBriefSectionBody = '';
    this.editingExpandedBrief = true;
    this.cdr.detectChanges();
  }

  applyInterviewExpandedEdit(): void {
    this.briefSections = this.parseBriefSections(this.currentAnswer).map((s) => ({
      ...s,
      html: undefined,
    }));
    this.editingExpandedBrief = false;
    this.briefExpanded = true;
    this.openBriefSections = new Set([0]);
    this.ensureBriefSectionHtml(0);
    this.cdr.detectChanges();
  }

  collapseExpandedBrief(ev?: Event): void {
    ev?.stopPropagation();
    this.briefExpanded = false;
    this.editingExpandedBrief = false;
    this.editingBriefSectionIndex = null;
    this.editingBriefSectionBody = '';
    this.openBriefSections = new Set();
    this.cdr.detectChanges();
  }

  isStepDone(id: UiStep): boolean {
    if (!this.project) return false;
    const state = this.project.state;
    const map: Record<UiStep, ProjectState[]> = {
      start: ['CREATED', 'AWAITING_DOCS', 'AWAITING_INTERVIEW', 'CONTEXT_READY', 'PLATFORM_SELECTED', 'GENERATING', 'READY_FOR_REVIEW', 'EXPORTED'],
      docs: ['CONTEXT_READY', 'PLATFORM_SELECTED', 'GENERATING', 'READY_FOR_REVIEW', 'EXPORTED'],
      interview: ['CONTEXT_READY', 'PLATFORM_SELECTED', 'GENERATING', 'READY_FOR_REVIEW', 'EXPORTED'],
      // Choosing an IDE is only reachable past the blueprint step, so the same states mark
      // both as done. A project that skipped the blueprint still passed through it.
      blueprint: ['PLATFORM_SELECTED', 'GENERATING', 'READY_FOR_REVIEW', 'EXPORTED'],
      platform: ['PLATFORM_SELECTED', 'GENERATING', 'READY_FOR_REVIEW', 'EXPORTED'],
      generate: ['READY_FOR_REVIEW', 'EXPORTED'],
      review: ['READY_FOR_REVIEW', 'EXPORTED'],
      export: ['EXPORTED'],
    };
    if (id === 'docs' || id === 'interview') {
      return map.docs.includes(state);
    }
    return (map[id] || []).includes(state) && state !== 'CREATED';
  }

  syncStepFromState(): void {
    if (!this.project) {
      this.step = 'start';
      this.savedDocuments = [];
      return;
    }
    this.refreshSavedDocuments();
    switch (this.project.state) {
      case 'CREATED':
        this.step = 'start';
        break;
      case 'AWAITING_DOCS':
        this.step = 'docs';
        // Restore a previously saved statement so Back into this step keeps it.
        if (!this.statement.trim() && this.project.brief?.problem_statement) {
          this.statement = this.project.brief.problem_statement;
        }
        break;
      case 'AWAITING_INTERVIEW':
        this.step = 'interview';
        this.interviewIndex = null;
        this.currentAnswer = this.activeInterviewQuestion?.answer || '';
        if (!this.interviewTitle && this.project.name !== 'Untitled Project') {
          this.interviewTitle = this.project.name;
        }
        this.hydrateExpandedBriefFromAnswer();
        break;
      case 'CONTEXT_READY':
        // The blueprint comes first: the diagrams are what the agents get generated from, so
        // they are reviewed and frozen before an IDE is chosen. Skipping is allowed — a
        // project without diagrams generates exactly as it did before this step existed.
        this.step = this.blueprintPassed ? 'platform' : 'blueprint';
        if (this.step === 'blueprint') this.loadBlueprint();
        break;
      case 'PLATFORM_SELECTED':
      case 'GENERATING':
        // Having an IDE means the blueprint step is behind us — record that so a reload
        // followed by Back returns to the IDE choice rather than skipping over it.
        this.blueprintPassed = true;
        this.step = 'generate';
        break;
      case 'READY_FOR_REVIEW':
        this.step = 'review';
        this.hydrateDraft();
        this.loadFilePreview();
        break;
      case 'EXPORTED':
        this.step = 'export';
        this.hydrateDraft();
        this.loadFilePreview();
        break;
    }
  }

  private hydrateDraft(): void {
    if (this.project?.plan) {
      this.draftPlan = structuredClone(this.project.plan);
      this.captureSavedPlan();
    }
  }

  private captureSavedPlan(): void {
    this.planSaved = !!this.draftPlan;
    this.planDirty = false;
  }

  /**
   * Dirty flag, not a recomputation.
   *
   * The template reads this four times per render, and comparing meant
   * JSON.stringify-ing the whole plan (agents, skills, rules, prompts, work
   * breakdown) each time — so typing in a prompt textarea got slower the bigger
   * the plan. Every editor already calls markPlanDirty(), so a flag is enough;
   * captureSavedPlan() clears it whenever the draft is refreshed from the server.
   */
  private planDirty = false;

  /** True when the draft has unsaved edits. */
  get isPlanDirty(): boolean {
    if (!this.draftPlan) return false;
    if (!this.planSaved) return true;
    return this.planDirty;
  }

  get savePlanLabel(): string {
    if (this.busy) return 'Saving…';
    if (!this.isPlanDirty && this.planSaved) return 'Saved';
    return 'Save plan';
  }

  /** Call from editors so change detection updates the Save button promptly. */
  markPlanDirty(): void {
    this.saveMessage = '';
    this.planDirty = true;
  }

  private loadFilePreview(): void {
    if (!this.project) return;
    this.filesPreviewBusy = true;
    this.api.previewFiles(this.project.id).subscribe({
      next: (r) => {
        this.filesPreviewBusy = false;
        this.previewFiles = r.files || [];
        this.previewImages = r.images || [];
        this.fileContents = r.contents || {};
        this.rebuildFileTree();
        const visible = this.visiblePreviewFiles();
        // Somebody clicked a filename before the tree existed — that file, not the default.
        const wanted = this.pendingFileSelection;
        this.pendingFileSelection = null;
        if (wanted && visible.includes(wanted)) {
          this.revealInTree(wanted);
          this.selectExportFile(wanted);
          this.maybeGenerateWorkspaceDocuments();
          this.cdr.detectChanges();
          return;
        }
        if (!this.selectedFile && visible.length) {
          this.selectExportFile(this.defaultExportFile(visible));
        } else if (this.fileEditMode) {
          // An editor is open — keep the user's unsaved text. Re-rendered server content
          // would overwrite it mid-typing, and Save persists this body anyway.
        } else if (this.selectedFile && this.fileContents[this.selectedFile] != null) {
          this.selectedFileBody = this.fileContents[this.selectedFile];
        } else if (this.selectedFile && !visible.includes(this.normalizePath(this.selectedFile))) {
          if (visible.length) {
            this.selectExportFile(this.defaultExportFile(visible));
          } else {
            this.selectedFile = null;
            this.selectedFileBody = '';
          }
        }
        this.maybeGenerateWorkspaceDocuments();
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.filesPreviewBusy = false;
        this.previewFiles = [];
        this.previewImages = [];
        this.fileContents = {};
        this.fileTree = [];
        this.invalidateTreeRows();
        this.error =
          err?.error?.message ||
          'Could not load file tree — check the API is running and refresh Bedrock credentials if export fails.';
        this.cdr.detectChanges();
      },
    });
  }

  /** True while the API is writing either generated document. */
  private docGenerating(progress?: string | null): boolean {
    const p = progress || '';
    return p.includes('WORKBREAKDOWN') || p.includes('SDD');
  }

  /**
   * Backfill whichever generated document this plan is still missing.
   *
   * Strictly one at a time, work breakdown first: the API refuses to start an SDD run
   * while a work breakdown is in flight (both write the same plan row), so the second
   * document is kicked off from the poll's terminal branch — which reloads the preview,
   * which lands back here — rather than fired alongside the first.
   */
  private maybeGenerateWorkspaceDocuments(): void {
    if (!this.project || this.wbsGenerating) return;
    if (this.step !== 'review' && this.step !== 'export') return;
    if (this.docGenerating(this.project.progress)) {
      this.wbsGenerating = true;
      this.wbsMessage = this.project.progress || '';
      this.docLabel = this.project.progress?.includes('WORKBREAKDOWN')
        ? WBS_FILENAME
        : SDD_FILENAME;
      this.startWbsPoll();
      return;
    }
    const plan = this.draftPlan || this.project.plan;
    if (!plan) return;
    if (!plan.work_breakdown_complete && !this.wbsGenerationAttempted) {
      this.generateDetailedWorkBreakdown();
      return;
    }
    if (!plan.solution_design_complete && !this.sddGenerationAttempted) {
      this.generateSolutionDesign();
    }
  }

  private generateDetailedWorkBreakdown(): void {
    if (!this.project || this.wbsGenerating) return;
    this.wbsGenerationAttempted = true;
    this.startDocGeneration(WBS_FILENAME, this.api.generateWorkBreakdown(this.project.id));
  }

  private generateSolutionDesign(): void {
    if (!this.project || this.wbsGenerating) return;
    this.sddGenerationAttempted = true;
    this.startDocGeneration(SDD_FILENAME, this.api.generateSolutionDesign(this.project.id));
  }

  /** Shared start + poll handoff for both documents (they never run concurrently). */
  private startDocGeneration(label: string, call: Observable<ProjectStatus>): void {
    this.docLabel = label;
    this.wbsGenerating = true;
    this.wbsMessage = `${label}: queued…`;
    call.subscribe({
      next: (p) => {
        this.project = p;
        if (this.docGenerating(p.progress)) {
          this.wbsMessage = p.progress || this.wbsMessage;
        }
        this.startWbsPoll();
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.wbsGenerating = false;
        if (label === WBS_FILENAME) this.wbsGenerationAttempted = false;
        else this.sddGenerationAttempted = false;
        this.wbsMessage = '';
        this.error =
          err?.error?.message ||
          `Could not start ${label} generation — check API and Bedrock credentials.`;
        this.cdr.detectChanges();
      },
    });
  }

  private startWbsPoll(): void {
    if (!this.project) return;
    this.wbsPollSub?.unsubscribe();
    this.wbsPollSub = interval(4000)
      .pipe(switchMap(() => this.api.getProject(this.project!.id)))
      .subscribe({
        next: (p) => {
          this.project = p;
          // This poll runs every 4s while a document generates. Replacing draftPlan
          // wholesale threw away whatever the user had typed into an open card editor or
          // an unsaved textarea. When something is in flight, take only the two generated
          // documents this poll exists to collect and leave the rest of the draft alone —
          // skipping outright would instead let the next Save push a stale
          // work_breakdown / solution_design over the freshly generated one.
          const editing = this.contentEditMode || this.fileEditMode || this.editingSummary;
          if (p.plan && (editing || this.isPlanDirty)) {
            if (this.draftPlan) {
              this.draftPlan.work_breakdown = p.plan.work_breakdown;
              this.draftPlan.work_breakdown_llm = p.plan.work_breakdown_llm;
              this.draftPlan.work_breakdown_complete = p.plan.work_breakdown_complete;
              this.draftPlan.solution_design = p.plan.solution_design;
              this.draftPlan.solution_design_llm = p.plan.solution_design_llm;
              this.draftPlan.solution_design_complete = p.plan.solution_design_complete;
            }
          } else if (p.plan) {
            this.draftPlan = structuredClone(p.plan);
            // Draft now equals server truth — snapshot it so Save shows "Saved".
            this.captureSavedPlan();
          }
          if (this.docGenerating(p.progress)) {
            this.wbsGenerating = true;
            this.wbsMessage = p.progress || this.wbsMessage;
            // The backend may hand off from one document to the other mid-poll; follow it
            // so the incremental reload opens the file that is actually being written.
            if (p.progress?.includes('WORKBREAKDOWN')) this.docLabel = WBS_FILENAME;
            else if (p.progress?.includes('SDD')) this.docLabel = SDD_FILENAME;
            this.refreshGeneratedDocPreview();
            this.cdr.detectChanges();
            return;
          }
          this.wbsGenerating = false;
          this.wbsPollSub?.unsubscribe();
          const isSdd = this.docLabel === SDD_FILENAME;
          const complete = isSdd
            ? p.plan?.solution_design_complete
            : p.plan?.work_breakdown_complete;
          const usedLlm = isSdd ? p.plan?.solution_design_llm : p.plan?.work_breakdown_llm;
          this.wbsMessage = complete
            ? `Detailed ${this.docLabel} ready — open it in Files.`
            : usedLlm
              ? `${this.docLabel} updated.`
              : 'Using fallback plan (Bedrock unavailable).';
          // Reloads the tree, and lands in maybeGenerateWorkspaceDocuments — which is
          // where the second document gets started once the first has finished.
          this.loadFilePreview();
          this.cdr.detectChanges();
        },
        error: () => {
          this.wbsGenerating = false;
          this.wbsPollSub?.unsubscribe();
          this.cdr.detectChanges();
        },
      });
  }

  /** Reload only the document being written (keeps scroll/file selection). */
  private refreshGeneratedDocPreview(): void {
    if (!this.project) return;
    this.api.previewFiles(this.project.id).subscribe({
      next: (r) => {
        this.previewFiles = r.files || [];
        this.previewImages = r.images || [];
        this.fileContents = r.contents || {};
        this.rebuildFileTree();
        const doc = this.docLabel;
        // Never yank the pane out from under an open editor: selectExportFile clears
        // fileEditMode and overwrites selectedFileBody, so this poll used to discard
        // whatever the user had typed — every 4 seconds, while a document generates.
        if (this.fileContents[doc] != null && !this.fileEditMode) {
          if (!this.selectedFile || this.selectedFile === doc) {
            this.selectExportFile(doc);
          }
        }
        this.cdr.detectChanges();
      },
    });
  }

  /**
   * Folders the user has opened, by full path.
   *
   * Held outside the tree because the tree is rebuilt from scratch every time the
   * workspace files reload — including the 4-second poll while WORKBREAKDOWN.md
   * generates. Without this, an expanded folder snapped shut moments after opening,
   * which looked exactly like the expand having failed or being slow.
   */
  private openDirs = new Set<string>();

  /**
   * The rows currently on screen, rebuilt only when the tree or its open folders change.
   *
   * A plain getter would re-flatten on every change-detection pass — and there are
   * several per second while WORKBREAKDOWN.md generates. The cache is invalidated by
   * `invalidateTreeRows()`, which every expand/collapse/rebuild calls.
   */
  private treeRowsCache: FileTreeRow[] | null = null;

  get fileTreeRows(): FileTreeRow[] {
    if (this.treeRowsCache) return this.treeRowsCache;
    const rows: FileTreeRow[] = [];
    const walk = (nodes: FileTreeNode[], depth: number) => {
      for (const node of nodes) {
        const isFolder = !!node.children;
        rows.push({
          node,
          key: isFolder ? `d:${node.dir || node.name}` : `f:${node.path || node.name}`,
          depth,
          isFolder,
        });
        // Closed folders contribute no rows at all — that is what keeps a 139-file
        // workspace to a couple of dozen DOM nodes instead of hundreds.
        if (isFolder && node.expanded) walk(node.children!, depth + 1);
      }
    };
    walk(this.fileTree, 0);
    this.treeRowsCache = rows;
    return rows;
  }

  private invalidateTreeRows(): void {
    this.treeRowsCache = null;
  }

  private buildFileTree(paths: string[]): FileTreeNode[] {
    const root: FileTreeNode[] = [];
    // Path → node, so building is one pass instead of scanning each folder's
    // children for every segment of every file (quadratic on a 139-file workspace).
    const dirs = new Map<string, FileTreeNode>();
    const seenFiles = new Set<string>();

    const ensureDir = (dirSegs: string[]): FileTreeNode[] => {
      let list = root;
      let prefix = '';
      for (const name of dirSegs) {
        prefix = prefix ? `${prefix}/${name}` : name;
        let node = dirs.get(prefix);
        if (!node) {
          node = { name, dir: prefix, children: [], expanded: this.openDirs.has(prefix) };
          dirs.set(prefix, node);
          list.push(node);
        }
        list = node.children!;
      }
      return list;
    };

    for (const raw of [...paths].sort()) {
      const norm = raw.replace(/\\/g, '/');
      const segs = norm.split('/').filter(Boolean);
      if (!segs.length || seenFiles.has(norm)) continue;
      seenFiles.add(norm);
      ensureDir(segs.slice(0, -1)).push({
        name: segs[segs.length - 1],
        path: norm,
        expanded: false,
      });
    }

    // Forget folders that no longer exist, so "Collapse all" is never offered with
    // nothing actually open.
    for (const dir of [...this.openDirs]) {
      if (!dirs.has(dir)) this.openDirs.delete(dir);
    }
    return root;
  }

  /**
   * Open or close one folder.
   *
   * The explicit detectChanges is what makes the chevron flip on the same frame as
   * the click: change detection is coalesced app-wide ([app.config.ts]), so mutating
   * `expanded` alone defers the repaint to the next coalesced pass. That pass is only a
   * frame away, not seconds — an earlier comment here claimed otherwise — but for a
   * direct toggle there is no reason to wait for it. Same fix as the attachment × button.
   *
   * The open state is also recorded in `openDirs` so the next tree rebuild keeps this
   * folder open instead of collapsing it under the user.
   */
  toggleFolder(node: FileTreeNode, ev?: Event): void {
    ev?.stopPropagation();
    if (!node.children) return;
    node.expanded = !node.expanded;
    if (node.dir) {
      if (node.expanded) this.openDirs.add(node.dir);
      else this.openDirs.delete(node.dir);
    }
    this.invalidateTreeRows();
    this.cdr.detectChanges();
  }

  /** Open every folder at once — cheaper than clicking down a deep path. */
  expandAllFolders(): void {
    const walk = (nodes: FileTreeNode[]) => {
      for (const n of nodes) {
        if (!n.children) continue;
        n.expanded = true;
        if (n.dir) this.openDirs.add(n.dir);
        walk(n.children);
      }
    };
    walk(this.fileTree);
    this.invalidateTreeRows();
    this.cdr.detectChanges();
  }

  /** Collapse back to top-level folders. */
  collapseAllFolders(): void {
    const walk = (nodes: FileTreeNode[]) => {
      for (const n of nodes) {
        if (!n.children) continue;
        n.expanded = false;
        walk(n.children);
      }
    };
    walk(this.fileTree);
    this.openDirs.clear();
    this.invalidateTreeRows();
    this.cdr.detectChanges();
  }

  /** True when at least one folder is open — drives which bulk action is offered. */
  get hasOpenFolders(): boolean {
    return this.openDirs.size > 0;
  }

  /**
   * Claude Code selected — the only platform that gets `CLAUDE.md`.
   *
   * Cursor reads `.cursor/rules` and Windsurf reads `AGENTS.md`, so neither would ever
   * load a `CLAUDE.md`; shipping one would just be an unread file in the repo.
   */
  get isClaudePlatform(): boolean {
    return this.project?.platform === 'claude_code';
  }

  private run<T>(obs: import('rxjs').Observable<T>, onOk: (v: T) => void): void {
    this.busy = true;
    this.error = '';
    this.saveMessage = '';
    this.cdr.detectChanges();
    obs.pipe(finalize(() => {
      this.busy = false;
      this.tailoringQuestions = false;
      this.cdr.detectChanges();
    })).subscribe({
      next: (v) => {
        this.apiOk = true;
        onOk(v);
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.error = err?.error?.message || err?.message || 'Request failed';
        this.cdr.detectChanges();
      },
    });
  }

  begin(): void {
    this.run(this.api.createProject(this.projectName || 'Untitled Project'), (p) => {
      this.project = p;
      this.syncStepFromState();
      this.router.navigate(['/wizard', p.id], { replaceUrl: true });
    });
  }

  choosePath(path: 'docs' | 'interview'): void {
    if (!this.project) return;
    this.briefExpanded = false;
    this.editingExpandedBrief = false;
    this.editingBriefSectionIndex = null;
    this.editingBriefSectionBody = '';
    this.briefSections = [];
    this.expandingBrief = false;
    this.run(this.api.setPath(this.project.id, path), (p) => {
      this.project = p;
      this.interviewIndex = null;
      this.currentAnswer = '';
      this.syncStepFromState();
    });
  }

  rollbackWizard(): void {
    if (!this.project || !this.canWizardBack || this.busy) return;
    // Back from the IDE choice lands on the blueprint, not on the context step — the
    // blueprint is the step in between. Back from *generate* still lands on the IDE choice,
    // which is why this is conditional rather than an unconditional reset.
    if (this.step === 'platform') this.blueprintPassed = false;
    this.run(this.api.rollback(this.project.id), (p) => {
      this.project = p;
      this.interviewIndex = null;
      // Do NOT blank currentAnswer — syncStepFromState refills it from the
      // persisted answer, so an expanded brief survives Back.
      this.selectedPlatform = p.platform || null;
      this.syncStepFromState();
    });
  }

  /**
   * Move the interview cursor locally — no request, so Back/Next are instant.
   *
   * Unsaved edits to the current question are stashed onto the question itself
   * first, so stepping away and back never loses typing (including an expanded
   * brief that has not been saved yet).
   */
  private moveInterviewCursor(index: number): void {
    const q = this.interviewQuestions[index];
    if (!q) return;
    this.stashCurrentAnswer();
    this.error = '';
    this.saveMessage = '';
    this.editingExpandedBrief = false;
    this.editingBriefSectionIndex = null;
    this.editingBriefSectionBody = '';
    this.briefExpanded = false;
    this.briefSections = [];
    this.interviewIndex = index;
    // Prefer an unsaved draft; a skipped question reopens empty so it can be
    // answered for real.
    const draft = this.pendingDrafts[q.id];
    this.currentAnswer = draft ?? (this.isQuestionSkipped(q) ? '' : q.answer || '');
    this.hydrateExpandedBriefFromAnswer();
    this.cdr.detectChanges();
  }

  /**
   * Keep unsaved text in a draft map so navigation is lossless. Drafts are
   * flushed to the server with the next Save, never silently dropped.
   */
  private stashCurrentAnswer(): void {
    const current = this.activeInterviewQuestion;
    const text = (this.currentAnswer || '').trim();
    if (!current) return;
    if (!text || text === (current.answer || '').trim()) {
      delete this.pendingDrafts[current.id];
      return;
    }
    this.pendingDrafts[current.id] = text;
  }

  /** Unsaved answers by question id — flushed on the next save. */
  pendingDrafts: Record<string, string> = {};

  get pendingDraftCount(): number {
    return Object.keys(this.pendingDrafts).length;
  }

  interviewBack(): void {
    if (this.busy || this.expandingBrief) return;
    if (this.canInterviewPrev) {
      this.moveInterviewCursor(this.activeInterviewIndex - 1);
      return;
    }
    // At the first question there is nothing left to step back to in the
    // interview itself — leave the step entirely.
    this.rollbackWizard();
  }

  /**
   * Next is only offered once the current question is settled: it re-reads a
   * later question without resubmitting, so revisiting answers is free.
   */
  get canInterviewNext(): boolean {
    const i = this.activeInterviewIndex;
    if (i < 0 || i >= this.interviewQuestions.length - 1) return false;
    return this.isQuestionSettled(this.interviewQuestions[i]);
  }

  interviewNext(): void {
    if (this.busy || this.expandingBrief || !this.canInterviewNext) return;
    this.moveInterviewCursor(this.activeInterviewIndex + 1);
  }

  onFileInput(ev: Event): void {
    const input = ev.target as HTMLInputElement;
    if (input.files) {
      const picked = Array.from(input.files);
      // Reset before the async attach so re-picking the same file still fires change.
      input.value = '';
      void this.addUploadFiles(picked);
    }
  }

  onDrop(ev: DragEvent): void {
    ev.preventDefault();
    this.dragOver = false;
    if (ev.dataTransfer?.files) {
      void this.addUploadFiles(Array.from(ev.dataTransfer.files));
    }
  }

  /**
   * Files being attached right now, and how many.
   *
   * Picking documents off a slow disk, a network share, or OneDrive is not instant:
   * the browser hands over lazy File handles and the first read is where the wait
   * happens. Without this the dropzone looked frozen and people clicked Browse
   * again, so the spinner covers the whole attach — including the readability probe.
   */
  attaching = 0;

  get isAttaching(): boolean {
    return this.attaching > 0;
  }

  private async addUploadFiles(incoming: File[]): Promise<void> {
    const allowed: File[] = [];
    const rejected: string[] = [];
    for (const f of incoming) {
      if (this.isAllowedUpload(f)) {
        allowed.push(f);
      } else {
        rejected.push(f.name);
      }
    }

    const accepted: File[] = [];
    const unreadable: string[] = [];
    if (allowed.length) {
      this.attaching = allowed.length;
      this.cdr.detectChanges();
      try {
        for (const f of allowed) {
          if (await this.isReadable(f)) {
            accepted.push(f);
          } else {
            unreadable.push(f.name);
          }
        }
      } finally {
        this.attaching = 0;
      }
    }

    const problems: string[] = [];
    if (rejected.length) {
      problems.push(
        `Skipped unsupported file(s): ${rejected.join(', ')}. ` +
          'Allowed: png, jpeg, pdf, docx, md, txt, csv, tsv, xlsx, xls.',
      );
    }
    if (unreadable.length) {
      problems.push(
        `Could not read: ${unreadable.join(', ')}. ` +
          'Empty, moved, or still syncing from cloud storage — try again.',
      );
    }
    this.fileRejectMessage = problems.join(' ');

    if (accepted.length) {
      this.files = [...this.files, ...accepted];
    }
    this.cdr.detectChanges();
  }

  /**
   * Can the browser actually read these bytes?
   *
   * Reading the first byte forces the handle open, which is what fails for a file
   * that has been moved since the picker listed it or that a cloud client has not
   * hydrated yet. Cheap for large files — only one byte is fetched.
   */
  private async isReadable(file: File): Promise<boolean> {
    if (!file.size) return false;
    try {
      await file.slice(0, 1).arrayBuffer();
      return true;
    } catch {
      return false;
    }
  }

  isAllowedUpload(file: File): boolean {
    const name = (file.name || '').toLowerCase();
    return this.allowedUploadExts.some((ext) => name.endsWith(ext));
  }

  fileExtLabel(file: File): string {
    const name = (file.name || '').toLowerCase();
    const hit = this.allowedUploadExts.find((ext) => name.endsWith(ext));
    return (hit || '').replace('.', '').toUpperCase() || 'FILE';
  }

  /**
   * Drop one attached file.
   *
   * Identified by object reference, not index, so the template can track by file: with
   * `track $index` removing the first chip renumbered every later one and Angular
   * re-created all of their DOM instead of detaching a single node.
   *
   * The explicit detectChanges is what makes the × feel instant — change detection is
   * coalesced app-wide, so without it the chip lingered until the next coalesced pass a
   * frame later. Small, but visible on a direct click.
   */
  removeFile(file: File): void {
    this.files = this.files.filter((f) => f !== file);
    this.fileRejectMessage = '';
    this.cdr.detectChanges();
  }

  /** Clear every attached file in one go — faster than removing chips one at a time. */
  clearFiles(): void {
    if (!this.files.length) return;
    this.files = [];
    this.fileRejectMessage = '';
    this.cdr.detectChanges();
  }

  /**
   * Documents saved with this project. Recomputed only when the project object
   * changes — never in a getter: this list used to be derived by splitting the
   * whole document_text on every change-detection pass, which made typing and
   * clicking on the docs step scale with document size.
   */
  savedDocuments: UploadedDocument[] = [];

  private refreshSavedDocuments(): void {
    this.savedDocuments = this.project?.brief?.documents ?? [];
  }

  /** "12.4 KB" / "812 B" — sizes are 0 for pre-upgrade uploads, so those are hidden. */
  formatBytes(bytes: number): string {
    if (!bytes || bytes <= 0) return '';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  extLabelFor(filename: string): string {
    const name = (filename || '').toLowerCase();
    const hit = this.allowedUploadExts.find((ext) => name.endsWith(ext));
    return (hit || '').replace('.', '').toUpperCase() || 'FILE';
  }

  get docsReady(): boolean {
    return this.statement.trim().length >= 12 && this.files.length > 0;
  }

  get docsMissingHint(): string {
    const needStatement = this.statement.trim().length < 12;
    const needFiles = this.files.length === 0;
    if (needStatement && needFiles) {
      return 'Problem statement and at least one document are required';
    }
    if (needStatement) return 'Add a clear problem statement (required)';
    if (needFiles) return 'Upload at least one document (required)';
    return '';
  }

  /** Seconds spent in the current upload — keeps the overlay honest. */
  docsElapsed = 0;
  private docsTimer: ReturnType<typeof setInterval> | null = null;

  private stopDocsTimer(): void {
    if (this.docsTimer) {
      clearInterval(this.docsTimer);
      this.docsTimer = null;
    }
  }

  get docsUploadHint(): string {
    if (this.docsElapsed < 3) return 'Extracting text from your documents';
    if (this.docsElapsed < 10) return 'Checking semantic similarity with your statement';
    return 'Still checking — the model is taking longer than usual';
  }

  submitDocs(): void {
    if (!this.project || !this.docsReady) return;
    this.docsPhase = 'checking';
    this.error = '';
    this.busy = true;
    this.docsElapsed = 0;
    this.stopDocsTimer();
    this.docsTimer = setInterval(() => {
      this.docsElapsed += 1;
      this.cdr.detectChanges();
    }, 1000);
    this.cdr.detectChanges();
    this.api.submitDocuments(this.project.id, this.statement, this.files).subscribe({
      next: (p) => {
        this.stopDocsTimer();
        this.busy = false;
        this.docsPhase = '';
        this.project = p;
        this.syncStepFromState();
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.stopDocsTimer();
        this.busy = false;
        this.docsPhase = '';
        this.error = err?.error?.message || err?.message || 'Document submit failed';
        this.cdr.detectChanges();
      },
    });
  }

  submitAnswer(): void {
    const q = this.activeInterviewQuestion;
    if (!this.project || !q) return;
    if (q.id === 'goal' && !this.currentAnswer.trim()) {
      this.error = 'Problem statement (goal) is required.';
      this.cdr.detectChanges();
      return;
    }
    const id = q.id;
    const text = this.currentAnswer.trim();
    // Flush drafts stashed while navigating so nothing typed is ever dropped.
    const answers: Record<string, string> = { ...this.pendingDrafts, [id]: text };
    // Saving a plain goal triggers server-side question tailoring — show that wait.
    this.tailoringQuestions = id === 'goal' && !/^##\s+/m.test(text);
    const title = id === 'goal' ? this.interviewTitle : undefined;
    this.run(this.api.answerInterview(this.project.id, answers, title), (p) => {
      this.tailoringQuestions = false;
      this.pendingDrafts = {};
      this.applyInterviewResult(p, id, text);
    });
  }

  /** Skip the current follow-up — stores the skip sentinel so the flow advances. */
  skipOptionalQuestion(): void {
    const q = this.activeInterviewQuestion;
    if (!this.project || !q || q.id === 'goal') return;
    const answers: Record<string, string> = { ...this.pendingDrafts, [q.id]: SKIP_ANSWER };
    delete this.pendingDrafts[q.id];
    this.run(this.api.answerInterview(this.project.id, answers), (p) => {
      this.pendingDrafts = {};
      this.applyInterviewResult(p, q.id, SKIP_ANSWER);
    });
  }

  /** Skip every remaining follow-up in one request and jump to the process blueprint. */
  skipRemainingQuestions(): void {
    if (!this.project || !this.goalAnswered) return;
    const pending = this.followupQuestions.filter((q) => !this.isQuestionSettled(q));
    if (!pending.length) return;
    // Drafts win over the skip sentinel — typed text is never thrown away.
    const answers: Record<string, string> = {};
    for (const q of pending) {
      answers[q.id] = this.pendingDrafts[q.id] || SKIP_ANSWER;
    }
    this.run(this.api.answerInterview(this.project.id, answers), (p) => {
      this.pendingDrafts = {};
      this.applyInterviewResult(p, 'skip_all', SKIP_ANSWER);
    });
  }

  /** Interview is fully answered — continue without resubmitting anything. */
  continueFromInterview(): void {
    if (!this.project || this.busy || !this.interviewAllSettled) return;
    const q = this.interviewQuestions.find((x) => x.id === 'goal');
    if (!q) return;
    this.stashCurrentAnswer();
    const answers: Record<string, string> = {
      ...this.pendingDrafts,
      goal: (this.pendingDrafts['goal'] || q.answer || '').trim(),
    };
    this.run(
      this.api.answerInterview(this.project.id, answers, this.interviewTitle),
      (p) => {
        this.pendingDrafts = {};
        this.applyInterviewResult(p, 'goal', answers['goal']);
      },
    );
  }

  private applyInterviewResult(p: ProjectStatus, id: string, text: string): void {
    this.project = p;
    this.interviewIndex = null;
    this.clearMarkdownCache();
    if (p.state === 'CONTEXT_READY') {
      // Keep currentAnswer/brief in memory: Back from IDE selection re-renders
      // this step, and syncStepFromState refills from the persisted answer.
      this.editingExpandedBrief = false;
      if (id === 'goal' && /^##\s+/m.test(text)) {
        this.saveMessage = 'Brief saved — repeat questions skipped; review the process blueprint next.';
      } else if (id === 'skip_all') {
        this.saveMessage = 'Remaining questions skipped — review the process blueprint next.';
      } else {
        this.saveMessage = 'Interview complete — review the process blueprint next.';
      }
    } else {
      this.currentAnswer = this.activeInterviewQuestion?.answer || '';
      this.hydrateExpandedBriefFromAnswer();
      if (id === 'goal') {
        const total = this.followupQuestions.length;
        this.saveMessage = total
          ? `Saved — ${total} project-specific question${total === 1 ? '' : 's'} ready. Skip any you don't need.`
          : 'Saved.';
      }
    }
    this.syncStepFromState();
  }

  // ── Process blueprint ──────────────────────────────────────────────────────────
  //
  // Three views of one extracted process model, so a follow-up edits the model once and all
  // three redraw in agreement. Draw and follow-up run in the background on the server; this
  // component polls the project and watches `progress` (prefixed "Blueprint:") the same way
  // the WORKBREAKDOWN poll does.

  get hasBlueprint(): boolean {
    return !!this.blueprint;
  }

  get blueprintFrozen(): boolean {
    return !!this.blueprint?.frozen;
  }

  /** True when generation would be refused server-side — diagrams exist but aren't approved. */
  get blueprintBlocksGenerate(): boolean {
    return !!this.project?.has_diagrams && !this.project?.diagrams_frozen;
  }

  /**
   * The set of PNGs the viewer is showing — the current version's, or a history version's.
   *
   * Handed over whole rather than one URL at a time so switching view inside the viewer needs
   * nothing from here. Its identity changes only when a set is replaced, which is also exactly
   * when the viewer should drop back to its default zoom, so the two stay in step by themselves.
   */
  get activeDiagramUrls(): Partial<Record<DiagramKind, string>> {
    return this.historyVersion !== null ? this.historyUrls : this.diagramUrls;
  }

  /**
   * The line after the process title under the image.
   *
   * The counts describe the current model, so they are only shown alongside the current
   * version's image — printing them under a v1 render would label that picture with numbers
   * that belong to v2.
   */
  get diagramCaption(): string {
    if (this.historyVersion !== null) return `as it was at v${this.historyVersion}`;
    const c = this.blueprintCounts;
    return `${c.steps} steps · ${c.decisions} decisions · ${c.actors} actors · ${c.handoffs} handoffs`;
  }

  /** Step counts for the caption under the image, so the numbers come from the model. */
  get blueprintCounts(): { steps: number; actors: number; decisions: number; handoffs: number } {
    const model = this.blueprint?.model;
    const steps = model?.steps || [];
    return {
      steps: steps.length,
      actors: model?.actors?.length || 0,
      decisions: steps.filter((s) => s.kind === 'decision').length,
      handoffs: model?.edges?.length || 0,
    };
  }

  setBlueprintTab(kind: DiagramKind): void {
    this.blueprintTab = kind;
    // Zoom is the viewer's own business: it resets itself when the view changes, because the
    // three have very different dimensions and a carried-over zoom would drop the reader into
    // the middle of a picture they have not seen yet.
    this.cdr.detectChanges();
  }


  /** Fetch the stored blueprint, if any. A null body is normal — it means "not drawn yet". */
  loadBlueprint(): void {
    if (!this.project || this.blueprintLoading) return;
    this.blueprintLoading = true;
    this.blueprintError = '';
    this.api.getDiagrams(this.project.id).subscribe({
      next: (set) => {
        this.blueprintLoading = false;
        this.blueprint = set;
        if (set) this.loadDiagramImages();
        // A draw that was running when the page reloaded keeps its spinner and its poll.
        if (this.project?.progress?.startsWith('Blueprint:')) {
          this.blueprintDrawing = true;
          this.blueprintMessage = this.project.progress;
          this.startBlueprintPoll(set?.version ?? 0);
        }
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.blueprintLoading = false;
        this.blueprintError = err?.error?.message || 'Could not load the process blueprint';
        this.cdr.detectChanges();
      },
    });
  }

  /** Draw all three views from the brief (and any uploaded documents). */
  generateBlueprint(): void {
    if (!this.project || this.blueprintDrawing) return;
    const from = this.blueprint?.version ?? 0;
    this.lastFollowupScope = 'all';
    // Null, not 'all': a draw from scratch is not a scoped follow-up, and the spinner says so.
    this.runningScope = null;
    this.blueprintDrawing = true;
    this.blueprintError = '';
    this.blueprintMessage = 'Reading the brief…';
    this.cdr.detectChanges();
    this.api.generateDiagrams(this.project.id).subscribe({
      next: (p) => {
        this.project = p;
        this.startBlueprintPoll(from);
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.blueprintDrawing = false;
        this.blueprintError =
          err?.error?.message || 'Could not start the blueprint — check API and Bedrock credentials.';
        this.cdr.detectChanges();
      },
    });
  }

  // ── Follow-up scope ────────────────────────────────────────────────────────
  //
  // Two things a follow-up can mean, and they are not the same request. "The reviewer also
  // checks credit history" changes the process, so all three views have to move together or
  // they start contradicting each other. "Group the checks into one SIPOC row" changes how one
  // picture is drawn and has no business touching the process — or the other two views, or the
  // agents generated from the model. Asking which one it is costs a click and is the difference
  // between a redraw the user expected and three diagrams they now have to re-check.

  /** The scope that will actually be sent — the tab's own kind when "only this view" is on. */
  get followupScope(): DiagramScope {
    return this.followupScopeMode === 'all' ? 'all' : this.blueprintTab;
  }

  /** "SIPOC" / "Process flow" / "Swimlane" for the current tab. */
  get activeViewLabel(): string {
    return this.diagramViews.find((v) => v.id === this.blueprintTab)?.label || 'this view';
  }

  setFollowupScope(mode: 'all' | 'view'): void {
    this.followupScopeMode = mode;
    this.cdr.detectChanges();
  }

  /** Label on the submit button, so the button says what it is about to do. */
  get followupApplyLabel(): string {
    return this.followupScopeMode === 'all'
      ? 'Apply to all three views'
      : `Apply to ${this.activeViewLabel} only`;
  }

  /** The consequence of the chosen scope, under the choice. */
  get followupScopeNote(): string {
    return this.followupScopeMode === 'all'
      ? 'The process itself is revised, then SIPOC, process flow and swimlane are all redrawn ' +
          'from it — the three stay in agreement, and the agents generated later follow the ' +
          'revised process.'
      : `Only the ${this.activeViewLabel} is re-drawn. The process, the other two views and ` +
          'anything generated from them are untouched — use this for how the picture reads ' +
          '(grouping, wording, level of detail), not for what the process does.';
  }

  /** Heading over the spinner while a blueprint job runs. */
  get blueprintSpinnerTitle(): string {
    if (this.runningScope === null) return 'Drawing your process';
    if (this.runningScope === 'all') return 'Revising the process and redrawing all three views';
    const label = this.diagramViews.find((v) => v.id === this.runningScope)?.label || 'the view';
    return `Re-drawing the ${label} only`;
  }

  /** The badge on the spinner. Reads `runningScope`, not the tab — the tab is not on screen. */
  get runningScopeTag(): string {
    if (this.runningScope === null || this.runningScope === 'all') return 'All three views';
    const label = this.diagramViews.find((v) => v.id === this.runningScope)?.label || 'This view';
    return `${label} only`;
  }

  get blueprintSpinnerHint(): string {
    if (this.runningScope === null) {
      return (
        'One pass reads the process, then the three views are drawn in parallel and reconciled ' +
        'against each other. Usually under a minute.'
      );
    }
    if (this.runningScope === 'all') {
      return (
        'Your change is applied to the process first, then all three views are laid out again ' +
        'from it. The previous version stays in the change history.'
      );
    }
    return (
      'One layout call, and the process model is not touched — so the other two views come ' +
      'back unchanged rather than being drawn again.'
    );
  }

  /** "All three views" / "SIPOC only" for a change-history entry. */
  revisionScopeLabel(r: DiagramRevision): string {
    if (r.version <= 1 && !r.instruction) return 'First draft';
    const scope = (r.scope || 'all') as DiagramScope;
    if (scope === 'all') return 'All three views';
    const label = this.diagramViews.find((v) => v.id === scope)?.label || scope;
    return `${label} only`;
  }

  /** Drives the badge colour: a process change reads differently from a re-draw of one view. */
  revisionScopeKind(r: DiagramRevision): 'first' | 'all' | 'view' {
    if (r.version <= 1 && !r.instruction) return 'first';
    return (r.scope || 'all') === 'all' ? 'all' : 'view';
  }

  /**
   * True when `blueprintError` is the server's "your follow-up changed nothing" advice.
   *
   * Nothing failed and nothing was lost in that case — the blueprint is still on the version
   * being looked at and the instruction is still in the box — so a red banner would overstate
   * it. The backend guarantees these messages open with this phrase (`NO_CHANGE_OPENING` in
   * `diagrams/build.py`); if that ever drifts the banner just goes back to red.
   */
  get blueprintNoChange(): boolean {
    return this.blueprintError.startsWith('Nothing changed');
  }

  /** Apply one plain-language change, to the process or to the view being looked at. */
  submitBlueprintFollowup(): void {
    if (!this.project || this.blueprintDrawing) return;
    const instruction = this.blueprintFollowup.trim();
    if (!instruction) {
      this.blueprintError = 'Describe the change you want first.';
      return;
    }
    const scope = this.followupScope;
    const from = this.blueprint?.version ?? 0;
    this.lastFollowupScope = scope;
    this.runningScope = scope;
    this.blueprintDrawing = true;
    this.blueprintError = '';
    this.blueprintMessage =
      scope === 'all'
        ? 'Applying your change to the process…'
        : `Re-drawing the ${this.activeViewLabel} only…`;
    this.cdr.detectChanges();
    this.api.refineDiagrams(this.project.id, instruction, scope).subscribe({
      next: (p) => {
        this.project = p;
        // The instruction stays in the box until a version actually lands. A follow-up can come
        // back having changed nothing — the model reads an under-specified request as a question
        // and answers it instead of acting — and making the user retype what they just wrote to
        // add one more word is the wrong end of that. The poll clears it on success.
        this.startBlueprintPoll(from);
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.blueprintDrawing = false;
        this.runningScope = null;
        this.blueprintError = err?.error?.message || 'Could not apply that change';
        this.cdr.detectChanges();
      },
    });
  }

  /** The banner after a version lands — it names what moved, which is what was just chosen. */
  private redrawnMessage(fromVersion: number): string {
    if (fromVersion === 0) return 'Blueprint ready — review the three views below.';
    if (this.lastFollowupScope === 'all') {
      return 'Process revised — all three views redrawn from it.';
    }
    const label =
      this.diagramViews.find((v) => v.id === this.lastFollowupScope)?.label || 'the view';
    return `${label} re-drawn. The process and the other two views are unchanged.`;
  }

  /**
   * Watch for the version bump.
   *
   * `progress` alone is not enough to finish on: it is cleared at the very end of the job, and
   * a poll that lands in the gap before the first update would read an empty string and stop
   * before anything was drawn. The version is the durable signal, so this waits for either a
   * bump past `fromVersion` or an error.
   */
  private startBlueprintPoll(fromVersion: number): void {
    if (!this.project) return;
    this.blueprintPollSub?.unsubscribe();
    let ticks = 0;
    this.blueprintPollSub = interval(2000)
      .pipe(switchMap(() => this.api.getProject(this.project!.id)))
      .subscribe({
        next: (p) => {
          this.project = p;
          // Version first. The server writes the new set and only then clears `progress`, so a
          // tick landing in between would otherwise report progress and wait another 2s for a
          // result that was already on disk.
          if ((p.diagram_version ?? 0) > fromVersion) {
            this.blueprintPollSub?.unsubscribe();
            this.blueprintDrawing = false;
            this.blueprintMessage = this.redrawnMessage(fromVersion);
            this.runningScope = null;
            // Only now is the instruction spent — a version landed, so it was carried out.
            this.blueprintFollowup = '';
            this.reloadBlueprint();
            return;
          }
          if (p.progress?.startsWith('Blueprint:')) {
            this.blueprintMessage = p.progress.replace(/^Blueprint:\s*/, '');
            // Progress means the job is alive, so the stall guard below starts over.
            ticks = 0;
            this.cdr.detectChanges();
            return;
          }
          if (p.error) {
            this.blueprintPollSub?.unsubscribe();
            this.blueprintDrawing = false;
            this.runningScope = null;
            this.blueprintMessage = '';
            this.blueprintError = p.error;
            this.cdr.detectChanges();
            return;
          }
          // No version, no progress, no error. A job that died with the process (an API
          // restart mid-draw) leaves exactly this, and the spinner would run forever.
          if (++ticks >= 45) {
            this.blueprintPollSub?.unsubscribe();
            this.blueprintDrawing = false;
            this.runningScope = null;
            this.blueprintMessage = '';
            this.blueprintError =
              'The blueprint job stopped responding — nothing was written. Check the API log, ' +
              'then draw it again.';
            this.cdr.detectChanges();
          }
        },
        error: () => {
          this.blueprintPollSub?.unsubscribe();
          this.blueprintDrawing = false;
          this.runningScope = null;
          this.cdr.detectChanges();
        },
      });
  }

  /** Re-read the set after a version bump (bypasses the `blueprintLoading` guard). */
  private reloadBlueprint(): void {
    if (!this.project) return;
    this.api.getDiagrams(this.project.id).subscribe({
      next: (set) => {
        this.blueprint = set;
        // A redraw changes every diagram's dimensions, so a carried-over zoom would land on a
        // different part of a different picture. Back to the first view at its own default —
        // and out of history, which is now one version further back.
        //
        // Except after a single-view follow-up: the one thing that changed is the view the user
        // was looking at, and sending them to the SIPOC to admire an unchanged picture is the
        // opposite of what they asked for.
        this.blueprintTab = this.lastFollowupScope === 'all' ? 'sipoc' : this.lastFollowupScope;
        this.backToLatest();
        if (set) this.loadDiagramImages();
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.blueprintError = err?.error?.message || 'Could not load the redrawn blueprint';
        this.cdr.detectChanges();
      },
    });
  }

  /**
   * Pull the three PNGs as blobs and swap the object URLs in.
   *
   * The old URLs are revoked only after the new ones are in place, so the visible image is
   * never pointed at a revoked blob mid-refresh (which renders as a broken-image icon).
   */
  private loadDiagramImages(): void {
    this.fetchDiagramPngs(undefined, (urls) => {
      const stale = Object.values(this.diagramUrls);
      this.diagramUrls = urls;
      for (const url of stale) {
        if (url) URL.revokeObjectURL(url);
      }
      this.cdr.detectChanges();
    });
  }

  /** Fetch all three PNGs of one version as object URLs, then hand them over together. */
  private fetchDiagramPngs(
    version: number | undefined,
    done: (urls: Partial<Record<DiagramKind, string>>) => void,
  ): void {
    if (!this.project) return;
    const id = this.project.id;
    const kinds: DiagramKind[] = ['sipoc', 'flow', 'swimlane'];
    const next: Partial<Record<DiagramKind, string>> = {};
    let pending = kinds.length;
    const settle = () => {
      if (--pending > 0) return;
      done(next);
    };
    for (const kind of kinds) {
      this.api.diagramPngBlob(id, kind, version).subscribe({
        next: (blob) => {
          next[kind] = URL.createObjectURL(blob);
          settle();
        },
        error: () => settle(),
      });
    }
  }

  /**
   * Show the diagrams as they were at one version from the change history.
   *
   * The PNGs are drawn on demand from the views stored with that revision, so this is a real
   * fetch and not a cache lookup — a version that has aged out of the history comes back with
   * nothing, which is why `viewable` is checked first and an empty result is reported.
   */
  viewRevision(version: number): void {
    if (!this.project || this.historyLoading) return;
    if (version === this.blueprint?.version) {
      this.backToLatest();
      return;
    }
    const rev = this.blueprint?.revisions.find((r) => r.version === version);
    if (rev && rev.viewable === false) {
      this.historyError = `v${version} is too old to redraw — only the most recent versions keep their diagrams.`;
      this.cdr.detectChanges();
      return;
    }
    this.historyLoading = true;
    this.historyError = '';
    this.cdr.detectChanges();
    this.fetchDiagramPngs(version, (urls) => {
      this.historyLoading = false;
      if (!Object.keys(urls).length) {
        this.historyError = `Could not redraw v${version} — its diagrams are no longer stored.`;
        this.cdr.detectChanges();
        return;
      }
      this.releaseHistoryUrls();
      this.historyUrls = urls;
      this.historyVersion = version;
      // A past version has different dimensions, so start from the first view again — the
      // viewer resets its own zoom when the URLs it was handed change.
      this.blueprintTab = 'sipoc';
      this.cdr.detectChanges();
    });
  }

  /**
   * Fetch the three architecture PNGs so the markdown preview can show SDD §3.2's figures.
   *
   * Once per project: the pictures are redrawn server-side from the current design, and the
   * cheap way to pick up a redraw is `releaseArchitectureUrls()` on a reload, not polling.
   * Nothing is fetched until something actually renders a figure — most visits to Review
   * never open SDD.md, and these are ~250 KB each at 3×.
   *
   * The markdown cache is keyed on the source text, which does not change when an image
   * arrives, so it has to be cleared here or the preview would keep the "not loaded yet"
   * placeholder until the next edit.
   */
  private ensureArchitectureImages(): void {
    if (!this.project || this.architectureFetched) return;
    this.architectureFetched = true;
    const id = this.project.id;
    const next: Partial<Record<ArchitectureKind, string>> = {};
    let pending = this.architectureViews.length;
    const settle = () => {
      if (--pending > 0) return;
      if (!Object.keys(next).length) {
        // Nothing came back (no SDD.md yet, or the design has no drawable structure). Allow a
        // later attempt rather than leaving the figures as placeholders for the whole session.
        this.architectureFetched = false;
        return;
      }
      this.releaseArchitectureUrls();
      this.architectureUrls = next;
      this.clearMarkdownCache();
      this.cdr.detectChanges();
    };
    for (const view of this.architectureViews) {
      this.api.architecturePngBlob(id, view.kind).subscribe({
        next: (blob) => {
          next[view.kind] = URL.createObjectURL(blob);
          settle();
        },
        error: () => settle(),
      });
    }
  }

  private releaseArchitectureUrls(): void {
    for (const url of Object.values(this.architectureUrls)) {
      if (url) URL.revokeObjectURL(url);
    }
    this.architectureUrls = {};
  }

  /** Stop looking at history. The current URLs were never released, so this is instant. */
  backToLatest(): void {
    if (this.historyVersion === null) return;
    this.releaseHistoryUrls();
    this.historyVersion = null;
    this.historyError = '';
    this.cdr.detectChanges();
  }

  private releaseHistoryUrls(): void {
    for (const url of Object.values(this.historyUrls)) {
      if (url) URL.revokeObjectURL(url);
    }
    this.historyUrls = {};
  }

  /** True for the revision row currently on screen, so the list shows where you are. */
  isViewingRevision(version: number): boolean {
    return this.historyVersion === version;
  }

  /** Steps matching `stepFilter`. Everything when the box is empty. */
  get filteredSteps(): ProcessStep[] {
    const steps = this.blueprint?.model.steps || [];
    const needle = this.stepFilter.trim().toLowerCase();
    if (!needle) return steps;
    return steps.filter((s) =>
      [s.id, s.name, s.actor, s.description, s.kind, s.systems.join(' ')]
        .join(' ')
        .toLowerCase()
        .includes(needle),
    );
  }

  freezeBlueprint(): void {
    if (!this.project || this.blueprintDrawing) return;
    this.run(this.api.freezeDiagrams(this.project.id, true), (set) => {
      this.blueprint = set;
      this.blueprintMessage = 'Blueprint approved — agents, skills and rules will follow it.';
      this.refresh();
    });
  }

  unfreezeBlueprint(): void {
    if (!this.project || this.blueprintDrawing) return;
    this.run(this.api.freezeDiagrams(this.project.id, false), (set) => {
      this.blueprint = set;
      this.blueprintMessage = 'Reopened for changes.';
      this.refresh();
    });
  }

  /**
   * Drop the blueprint entirely.
   *
   * The project then generates straight from the brief, which is exactly the pre-existing
   * flow — this is the escape hatch, not a dead end.
   */
  discardBlueprint(): void {
    if (!this.project || this.blueprintDrawing) return;
    if (!confirm('Discard the process blueprint? The project will generate from the brief alone.')) {
      return;
    }
    this.run(this.api.deleteDiagrams(this.project.id), (p) => {
      this.project = p;
      this.blueprint = null;
      this.releaseDiagramUrls();
      this.blueprintMessage = 'Blueprint discarded — generation now reads the brief directly.';
    });
  }

  /** Frozen (or deliberately absent) — move on to the IDE choice. */
  continueFromBlueprint(): void {
    if (this.blueprintDrawing) return;
    if (this.hasBlueprint && !this.blueprintFrozen) {
      this.blueprintError = 'Approve the blueprint first, or discard it, before choosing an IDE.';
      return;
    }
    this.blueprintPassed = true;
    this.blueprintError = '';
    this.selectedPlatform = this.project?.platform || this.selectedPlatform;
    // Reached from the generate step (the freeze gate sent the user back here), the IDE is
    // already chosen — returning to the IDE picker would ask a question already answered.
    this.step = this.blueprintRevisit ? 'generate' : 'platform';
    this.cdr.detectChanges();
  }

  /** True when the blueprint step was reopened from further along the wizard. */
  get blueprintRevisit(): boolean {
    const s = this.project?.state;
    return s === 'PLATFORM_SELECTED' || s === 'GENERATING';
  }

  /** Back out of the blueprint step without undoing a choice made after it. */
  blueprintBack(): void {
    if (this.blueprintDrawing || this.busy) return;
    if (this.blueprintRevisit) {
      this.blueprintPassed = true;
      this.step = 'generate';
      this.cdr.detectChanges();
      return;
    }
    this.rollbackWizard();
  }

  /** Go on without drawing anything. Only offered while there is no blueprint at all. */
  skipBlueprint(): void {
    if (this.hasBlueprint) return;
    this.blueprintMessage = '';
    this.continueFromBlueprint();
  }

  /** Label for the skip button — the IDE is already chosen on a revisit. */
  get blueprintSkipLabel(): string {
    return this.blueprintRevisit ? 'Back to generation →' : 'Skip to IDE →';
  }

  /** Back to the blueprint from the generate step when the freeze gate refused. */
  goToBlueprint(): void {
    this.blueprintPassed = false;
    this.step = 'blueprint';
    this.blueprintError = '';
    this.loadBlueprint();
    this.cdr.detectChanges();
  }

  private releaseDiagramUrls(): void {
    for (const url of Object.values(this.diagramUrls)) {
      if (url) URL.revokeObjectURL(url);
    }
    this.diagramUrls = {};
    this.releaseHistoryUrls();
    this.historyVersion = null;
    this.releaseArchitectureUrls();
    // Cleared too, so the next project's SDD.md refetches instead of showing this one's views.
    this.architectureFetched = false;
    this.clearMarkdownCache();
  }

  selectPlatform(id: string): void {
    this.selectedPlatform = id;
    this.cdr.detectChanges();
  }

  confirmPlatform(): void {
    if (!this.project || !this.selectedPlatform) return;
    this.run(this.api.setPlatform(this.project.id, this.selectedPlatform), (p) => {
      this.project = p;
      this.syncStepFromState();
    });
  }

  startGenerate(): void {
    if (!this.project) return;
    this.busy = true;
    this.error = '';
    this.cdr.detectChanges();
    this.api.generate(this.project.id).subscribe({
      next: (p) => {
        this.project = p;
        this.syncStepFromState();
        this.pollUntilDone();
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.busy = false;
        this.error = err?.error?.message || 'Generation failed';
        this.refresh();
        this.cdr.detectChanges();
      },
    });
  }

  private pollUntilDone(): void {
    this.pollSub?.unsubscribe();
    this.pollSub = interval(900)
      .pipe(switchMap(() => this.api.getProject(this.project!.id)))
      .subscribe((p) => {
        this.project = p;
        if (p.state === 'READY_FOR_REVIEW') {
          this.busy = false;
          this.pollSub?.unsubscribe();
          this.syncStepFromState();
        } else if (p.state === 'PLATFORM_SELECTED') {
          this.busy = false;
          this.pollSub?.unsubscribe();
          this.error = p.error || 'Generation failed';
          this.syncStepFromState();
        } else {
          this.step = 'generate';
        }
        this.cdr.detectChanges();
      });
  }

  refresh(): void {
    if (!this.project) return;
    this.api.getProject(this.project.id).subscribe((p) => {
      this.project = p;
      this.syncStepFromState();
      this.cdr.detectChanges();
    });
  }

  setReviewTab(tab: ReviewTab): void {
    this.reviewTab = tab;
    this.expandedIndex = null;
    this.cdr.detectChanges();
    if (tab === 'files' && !this.previewFiles.length) {
      this.loadFilePreview();
    }
    if (tab === 'diagrams') {
      this.ensureDiagramImages();
      // The tab shows SDD §3.2's figures too, and they come from a different endpoint.
      this.ensureArchitectureImages();
    }
  }

  /**
   * Folder and extension the rules land in, which differ per IDE.
   *
   * Cursor uses `.mdc` with `alwaysApply`/`globs` frontmatter; Claude Code and Windsurf
   * use plain `.md`. Claude has no frontmatter dialect at all — its always-on rules reach
   * context because `CLAUDE.md` imports them with `@`, so the hint says so rather than
   * implying a field that does not exist.
   */
  get rulesFolderHint(): string {
    const p = this.project?.platform || this.selectedPlatform;
    if (p === 'claude_code') {
      return (
        'Exported to .claude/rules/ as plain .md — no frontmatter. Always-on rules are ' +
        'imported by CLAUDE.md so they load every session; scoped rules state their globs ' +
        'in their own header.'
      );
    }
    if (p === 'cursor') {
      return 'Exported to .cursor/rules/ as .mdc with alwaysApply or globs frontmatter.';
    }
    if (p === 'github_copilot') {
      return 'Exported to .github/instructions/ as .instructions.md with applyTo frontmatter.';
    }
    return 'Exported to .windsurf/rules/ as .md with a trigger frontmatter field.';
  }

  /** Rule file as it appears in the export, so the UI matches the folder on disk. */
  ruleFileName(rule: { name: string }): string {
    const p = this.project?.platform || this.selectedPlatform;
    return `${rule.name}${p === 'cursor' ? '.mdc' : p === 'github_copilot' ? '.instructions.md' : '.md'}`;
  }

  ruleModeLabel(rule: { always_apply?: boolean; globs?: string[] }): string {
    if (rule.always_apply) return 'always';
    return rule.globs?.length ? `globs: ${rule.globs.join(', ')}` : 'model decides';
  }

  toggleExpand(i: number): void {
    this.expandedIndex = this.expandedIndex === i ? null : i;
    this.contentEditMode = false;
    this.cdr.detectChanges();
  }

  /**
   * Flip an agent/skill/rule card between markdown preview and its editable body.
   *
   * Called instead of mutating the flag inline in the template so the swap is painted on
   * this frame rather than on the next coalesced pass ([app.config.ts]). Card bodies are
   * small — the largest here is 46 lines — so unlike the file pane there is nothing to
   * gain from keeping both panes mounted; see `toggleFileEditMode` for that case.
   */
  toggleContentEditMode(): void {
    this.contentEditMode = !this.contentEditMode;
    this.cdr.detectChanges();
  }

  /** Same same-frame repaint for the plan summary's Edit/Done button. */
  toggleSummaryEdit(): void {
    this.editingSummary = !this.editingSummary;
    this.cdr.detectChanges();
  }

  savePlan(): void {
    if (!this.project || !this.draftPlan || this.busy) return;
    if (!this.isPlanDirty && this.planSaved) {
      this.saveMessage = 'Already saved — no changes to persist.';
      return;
    }
    this.run(this.api.updatePlan(this.project.id, this.draftPlan), (p) => {
      this.project = p;
      this.hydrateDraft();
      this.contentEditMode = false;
      this.fileEditMode = false;
      this.editingSummary = false;
      this.captureSavedPlan();
      this.saveMessage = 'Everything saved.';
      this.loadFilePreview();
    });
  }

  /** Persist this card’s edits and return to markdown preview. */
  saveCard(): void {
    if (!this.project || !this.draftPlan) return;
    this.run(this.api.updatePlan(this.project.id, this.draftPlan), (p) => {
      this.project = p;
      this.contentEditMode = false;
      this.captureSavedPlan();
      this.saveMessage = 'Saved — showing markdown preview.';
      this.loadFilePreview();
    });
  }

  selectExportFile(path: string): void {
    this.selectedFile = path;
    // Leave edit mode *before* assigning the body: the setter only refreshes the preview
    // snapshot while the preview is the visible pane, so the other order would show the
    // previous file's markdown next to the new file's name.
    this.fileEditMode = false;
    this.selectedFileBody = this.fileContents[path] || '';
    this.fileCopyMessage = '';
    // A blueprint PNG opens the diagram viewer instead of the editor — same zoom / fit /
    // 100% / pan / full-screen controls as the Blueprint step, on the same bytes the zip
    // holds. There is nothing to edit in a render, and an empty textarea beside a filename
    // reads as a missing file.
    const ref = this.diagramFileRef(path);
    if (ref) {
      this.blueprintTab = ref.kind;
      this.ensureDiagramImages();
    }
    // Same for an architecture view, and for SDD.md itself — its §3.2 embeds all three, and
    // starting the fetch here means the preview usually renders the pictures on first paint
    // instead of showing placeholders and then replacing them.
    if (this.architectureFileRef(path) || this.selectedFileBody.includes('docs/architecture/')) {
      this.ensureArchitectureImages();
    }
    this.cdr.detectChanges();
  }

  /**
   * `docs/architecture/logical-view.png` → the view it is, or null.
   *
   * No version in the name, unlike `diagramFileRef`: these are drawn from the current design
   * rather than from an approved revision, so there is only ever one of each.
   */
  architectureFileRef(path: string | null): { kind: ArchitectureKind; label: string } | null {
    const p = this.normalizePath(path || '');
    const view = this.architectureViews.find((v) => v.path === p);
    return view ? { kind: view.kind, label: view.label } : null;
  }

  /** The architecture view the file pane is showing, if that is what was clicked. */
  get selectedFileArchitecture(): { kind: ArchitectureKind; label: string } | null {
    return this.architectureFileRef(this.selectedFile);
  }

  /** Object URL for one view — empty until `ensureArchitectureImages` has it. */
  architectureUrlFor(kind: ArchitectureKind): string {
    return this.architectureUrls[kind] || '';
  }

  /** The blueprint PNGs the workspace holds — the first half of the Diagrams tab. */
  get blueprintPreviewImages(): string[] {
    return this.previewImages.filter((p) => !!this.diagramFileRef(p));
  }

  /** The `docs/architecture/` PNGs — the other half, and the other half of the tab's count. */
  get architecturePreviewImages(): string[] {
    return this.previewImages.filter((p) => !!this.architectureFileRef(p));
  }

  /** True once SDD §3.2's figures exist in the workspace, so the tab can offer them. */
  get hasArchitectureViews(): boolean {
    return this.architecturePreviewImages.length > 0;
  }

  /** Which architectural view the Diagrams tab's own strip is on. */
  setArchitectureTab(kind: string): void {
    if (!isArchitectureKind(kind)) return;
    this.architectureTab = kind;
    this.ensureArchitectureImages();
    this.cdr.detectChanges();
  }

  /**
   * Open a workspace file from anywhere — a filename under a diagram, a figure's caption.
   *
   * The Diagrams tab used to print the six paths as plain `<code>` spans, which read as links
   * and did nothing. Switching to the Files pane, revealing the row and selecting it is what
   * "click the filename" means everywhere else in this pane.
   */
  openWorkspaceFile(path: string): void {
    const p = this.normalizePath(path);
    if (!p) return;
    this.setReviewTab('files');
    if (!this.previewFiles.length) {
      // The pane is still loading (`setReviewTab` kicked it off). Selected on arrival instead,
      // otherwise the default file would win the race.
      this.pendingFileSelection = p;
      return;
    }
    this.revealInTree(p);
    this.selectExportFile(p);
  }

  /** `SDD.md` in the Files pane. Named so the two callers cannot disagree on the path. */
  openSolutionDesign(): void {
    this.openWorkspaceFile(SDD_FILENAME);
  }

  /** Is this row the solution design? What decides whether the download buttons are offered. */
  isSolutionDesignFile(path: string | null): boolean {
    return this.normalizePath(path || '') === SDD_FILENAME;
  }

  /**
   * Download SDD.md as a PDF or a Word document.
   *
   * The server lays it out from the same markdown this pane previews — one parser, two
   * emitters — so the file that lands is the document the reader just read, with §3.2's three
   * architecture views embedded rather than three broken image links.
   *
   * The filename comes from the response's own `Content-Disposition` when there is one: the
   * server names it after the project, and duplicating that rule here is how the two drift.
   */
  downloadSolutionDesign(fmt: DocumentFormat): void {
    if (!this.project || this.sddDownloading) return;
    const project = this.project;
    this.sddDownloading = fmt;
    this.sddDownloadMessage = `Building the ${fmt.toUpperCase()}…`;
    this.error = '';
    this.cdr.detectChanges();
    this.api
      .solutionDesignBlob(project.id, fmt)
      .pipe(finalize(() => {
        this.sddDownloading = null;
        this.cdr.detectChanges();
      }))
      .subscribe({
        next: (blob) => {
          // A JSON body with a 200 is an error the server wrapped — saving it as `.pdf` would
          // hand the reader a file their viewer refuses to open with nothing said about why.
          if (blob.type && blob.type.includes('application/json')) {
            this.sddDownloadMessage = '';
            void blob.text().then((text) => {
              try {
                this.error = (JSON.parse(text) as { message?: string }).message || 'Download failed';
              } catch {
                this.error = 'Download failed';
              }
              this.cdr.detectChanges();
            });
            return;
          }
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          a.download = `${this.exportZipBasename(project.name)}-SDD.${fmt}`;
          a.click();
          URL.revokeObjectURL(url);
          this.sddDownloadMessage = `${fmt.toUpperCase()} downloaded.`;
        },
        error: (err) => {
          this.sddDownloadMessage = '';
          this.error =
            err?.error?.message || `Could not build the ${fmt.toUpperCase()} — try again.`;
          this.auth.handleAuthError(err);
        },
      });
  }

  /**
   * Expand every folder above a file so the selected row is actually on screen.
   *
   * Selecting a row inside a closed folder highlights something nobody can see, which looks
   * like the click having failed.
   */
  private revealInTree(path: string): void {
    const segs = path.split('/').filter(Boolean);
    if (segs.length < 2) return;
    let prefix = '';
    for (const name of segs.slice(0, -1)) {
      prefix = prefix ? `${prefix}/${name}` : name;
      this.openDirs.add(prefix);
    }
    const walk = (nodes: FileTreeNode[]) => {
      for (const n of nodes) {
        if (!n.children) continue;
        if (n.dir && this.openDirs.has(n.dir)) n.expanded = true;
        walk(n.children);
      }
    };
    walk(this.fileTree);
    this.invalidateTreeRows();
  }

  /**
   * Switch architecture view from inside the viewer — the ← → keys, since the file tree does
   * the choosing here and there is no tab strip.
   *
   * Selecting the row rather than only swapping the image keeps the tree honest: highlighting
   * `logical-view.png` while the deployment view is on screen is the same defect
   * `showDiagramKind` exists to avoid on the blueprint side.
   */
  showArchitectureKind(kind: string): void {
    if (!isArchitectureKind(kind)) return;
    const view = this.architectureViews.find((v) => v.kind === kind);
    if (view) this.selectExportFile(view.path);
  }

  /**
   * Open the figure the reader clicked in a rendered document, in the full viewer.
   *
   * SDD §3.2 embeds all three views inline, fitted to the pane width — readable, but not the
   * size the labels were laid out for. A click selects that PNG's own row, which is the viewer
   * with fit / 100% / drag-to-pan / full screen on it. Returns whether it matched, so
   * `onPreviewClick` knows to swallow the event.
   */
  private openFigureFromPreview(img: HTMLImageElement): boolean {
    const src = img.getAttribute('src') || '';
    const kind = (Object.keys(this.architectureUrls) as ArchitectureKind[]).find(
      (k) => this.architectureUrls[k] === src
    );
    const view = kind ? this.architectureViews.find((v) => v.kind === kind) : undefined;
    if (!view) return false;
    this.selectExportFile(view.path);
    return true;
  }

  /**
   * `docs/diagrams/process-flow-v2.png` → `{kind: 'flow', version: 2}`; anything else → null.
   *
   * The exported filenames carry the version (see `export_filename` in the backend), which is
   * also what lets a row opened here be matched to the render it came from.
   */
  diagramFileRef(path: string | null): { kind: DiagramKind; version: number } | null {
    const m = /^docs\/diagrams\/(sipoc|process-flow|swimlane)-v(\d+)\.png$/i.exec(
      this.normalizePath(path || '')
    );
    if (!m) return null;
    const stem = m[1].toLowerCase();
    const kind = (stem === 'process-flow' ? 'flow' : stem) as DiagramKind;
    return { kind, version: Number(m[2]) };
  }

  /** The blueprint image the file pane is showing, or nothing when a text file is selected. */
  get selectedFileDiagram(): { kind: DiagramKind; version: number } | null {
    return this.diagramFileRef(this.selectedFile);
  }

  /** True once the workspace has blueprint images — drives the Review step's Diagrams tab. */
  get hasExportedDiagrams(): boolean {
    return this.previewImages.some((p) => !!this.diagramFileRef(p));
  }

  /** Exported path for one view, so the tree selection can follow a view switch. */
  private diagramPathFor(kind: DiagramKind): string | null {
    return this.previewImages.find((p) => this.diagramFileRef(p)?.kind === kind) || null;
  }

  /**
   * Show a view, from wherever the viewer happens to be.
   *
   * In the Files pane the picture is a selected row, so switching view has to move the
   * selection too — otherwise the tree highlights `sipoc-v2.png` while the swimlane is on
   * screen. Everywhere else it is just the tab.
   *
   * A string in, because the viewer also carries the architecture views and so cannot name
   * either union — `isDiagramKind` is the gate, and an id from the other family is ignored
   * rather than cast into a blueprint kind that does not exist.
   */
  showDiagramKind(kind: string): void {
    if (!isDiagramKind(kind)) return;
    if (this.selectedFileDiagram) {
      const path = this.diagramPathFor(kind);
      if (path) {
        this.selectExportFile(path);
        return;
      }
    }
    this.setBlueprintTab(kind);
  }

  /**
   * Fetch the blueprint and its PNGs the first time something needs them.
   *
   * Not part of the Review step's load: the three renders are several megabytes at 3×, and
   * most visits to Review never open a diagram. Called when the Diagrams tab opens or an
   * exported PNG is clicked. `loadBlueprint` guards its own in-flight request.
   */
  ensureDiagramImages(): void {
    if (!this.project || this.blueprint) return;
    this.loadBlueprint();
  }

  isMarkdownFile(path: string | null): boolean {
    if (!path) return false;
    const p = path.toLowerCase();
    return p.endsWith('.md') || p.endsWith('.mdc') || p.endsWith('.markdown');
  }

  /**
   * Flip the file pane between markdown preview and a textarea.
   *
   * What made this slow was rebuilding the preview, not setting the flag. The preview div
   * used to sit in the `@else` of an `@if`, so every toggle destroyed and re-created it —
   * and on a real workspace that is a lot of DOM: the largest WORKBREAKDOWN.md here is
   * 2091 lines, ~463 KB of HTML and ~1700 elements, built synchronously before the
   * browser can paint. The template now keeps that div alive and just hides it, so
   * returning to Preview is a style change instead of a re-parse.
   *
   * `filePreviewBody` is what the preview renders, and it is deliberately *not*
   * `selectedFileBody`: a live binding would re-parse all 463 KB on every keystroke now
   * that the div is always present. It is re-synced here, on the way back to Preview, so
   * unsaved edits still show up.
   */
  toggleFileEditMode(): void {
    this.fileEditMode = !this.fileEditMode;
    if (this.fileEditMode) {
      this.fileCopyMessage = '';
    } else {
      this.filePreviewBody = this.selectedFileBody;
    }
    this.cdr.detectChanges();
  }

  copySelectedFileContent(): void {
    const text = this.selectedFileBody || '';
    if (!text.trim()) {
      this.fileCopyMessage = 'Nothing to copy';
      return;
    }
    const done = (msg: string) => {
      this.fileCopyMessage = msg;
      this.cdr.detectChanges();
      window.setTimeout(() => {
        if (this.fileCopyMessage === msg) {
          this.fileCopyMessage = '';
          this.cdr.detectChanges();
        }
      }, 2500);
    };
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(
        () => done('Copied'),
        () => this.fallbackCopyFileContent(text, done)
      );
    } else {
      this.fallbackCopyFileContent(text, done);
    }
  }

  private fallbackCopyFileContent(text: string, done: (msg: string) => void): void {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      done('Copied');
    } catch {
      done('Copy failed');
    }
  }

  /**
   * Confirmation line after a refresh, naming both counts.
   *
   * The old text said only "N workspace files", which sat beside a "Files (N)" tab badge
   * holding a smaller number and looked like a desync. Both are correct — one counts the
   * app scaffold, the other every path in the export — so both are now labelled.
   */
  private setRefreshMessage(): void {
    const extras = this.isClaudePlatform
      ? `IDE folder, README, ${WBS_FILENAME}, ${SDD_FILENAME}, .gitignore, CLAUDE.md`
      : `IDE folder, README, ${WBS_FILENAME}, ${SDD_FILENAME}, .gitignore`;
    this.fileRefreshMessage =
      `Refreshed · ${this.workspaceFileCount} workspace files ` +
      `(${this.sourceFileCount} source + ${extras})`;
  }

  /**
   * Re-render the workspace from the saved plan.
   *
   * Not a regeneration: it asks the server to render the export again, which picks up
   * plan edits you saved and `WORKBREAKDOWN.md` / `SDD.md` once background generation
   * finishes. It also backfills `source_tree` on older projects so the scaffold becomes
   * editable.
   */
  refreshFileTree(): void {
    if (!this.project) return;
    this.wbsGenerationAttempted = false;
    this.sddGenerationAttempted = false;
    this.fileRefreshMessage = 'Refreshing file tree…';
    this.filesPreviewBusy = true;
    const keep = this.selectedFile;
    this.api.previewFiles(this.project.id).subscribe({
      next: (r) => {
        this.filesPreviewBusy = false;
        this.previewFiles = r.files || [];
        this.previewImages = r.images || [];
        this.fileContents = r.contents || {};
        this.rebuildFileTree();
        const visible = this.visiblePreviewFiles();
        const keepNorm = keep ? this.normalizePath(keep) : '';
        if (keep && visible.some((p) => this.normalizePath(p) === keepNorm) && this.fileContents[keep] != null) {
          this.selectExportFile(keep);
        } else if (visible.length) {
          this.selectExportFile(this.defaultExportFile(visible));
        } else {
          this.selectedFile = null;
          this.selectedFileBody = '';
        }
        // Reload plan so backfilled source_tree (existing projects) is editable
        this.api.getProject(this.project!.id).subscribe({
          next: (p) => {
            this.project = p;
            if (p.plan) {
              this.draftPlan = structuredClone(p.plan);
              // Reloaded from the server, so the draft is clean.
              this.captureSavedPlan();
              this.rebuildFileTree();
            }
            this.maybeGenerateWorkspaceDocuments();
            // Both numbers, spelled out — "165 workspace files" next to a "Files (139)"
            // badge read like a mismatch when it was really two different quantities.
            this.setRefreshMessage();
            this.cdr.detectChanges();
          },
          // Plan reload is only for editability; the tree already rendered.
          error: () => this.setRefreshMessage(),
        });
      },
      error: (err) => {
        this.filesPreviewBusy = false;
        this.fileRefreshMessage = '';
        this.error = err?.error?.message || 'Could not refresh file tree — save the plan first';
      },
    });
  }

  /**
   * Apply edited export file into the draft plan, persist overrides, and show live markdown preview.
   */
  applyFileEditToPlan(): void {
    if (!this.selectedFile || !this.project) return;
    if (!this.draftPlan && this.project.plan) {
      this.hydrateDraft();
    }
    if (!this.draftPlan) {
      this.error = 'Plan not loaded — open Review or refresh, then try Save again.';
      return;
    }

    const path = this.selectedFile.replace(/\\/g, '/').replace(/^\/+/, '');
    const body = this.selectedFileBody;

    // Always persist the exact edited file so reopen/refresh keeps the change.
    this.draftPlan.file_overrides = {
      ...(this.draftPlan.file_overrides || {}),
      [path]: body,
    };

    // Keep structured plan fields in sync when the path maps to an artifact.
    const agentMatch =
      path.match(/\.windsurf\/agents\/([^/]+)\/AGENT\.md$/) ||
      path.match(/\.github\/agents\/([^/]+)\.agent\.md$/) ||
      path.match(/(?:\.claude|\.cursor|\.agents)\/agents\/([^/]+)\.md$/);
    if (agentMatch) {
      const agent = this.draftPlan.agents.find((a) => a.name === agentMatch[1]);
      if (agent) {
        agent.system_prompt = this.stripFrontmatter(body);
      }
    }

    const skillMatch = path.match(/skills\/([^/]+)\/SKILL\.md$/);
    if (skillMatch) {
      const skill = this.draftPlan.skills.find((s) => s.name === skillMatch[1]);
      if (skill) {
        skill.instructions = this.stripFrontmatter(body);
      }
    }

    const ruleMatch = path.match(/rules\/([^/]+)\.(mdc|md)$/);
    if (ruleMatch) {
      const rule = this.draftPlan.rules.find((r) => r.name === ruleMatch[1]);
      if (rule) {
        rule.body = this.stripFrontmatter(body);
      }
    }

    if (path === WBS_FILENAME) {
      this.draftPlan.work_breakdown = body;
      this.draftPlan.work_breakdown_llm = true;
      this.draftPlan.work_breakdown_complete = body.includes('### Agent prompt') &&
        (body.match(/## Phase \d+:/g) || []).length >= 4 &&
        !/\n###\s*$/.test(body.trim());
    }

    if (path === SDD_FILENAME) {
      this.draftPlan.solution_design = body;
      this.draftPlan.solution_design_llm = true;
      // A cheap stand-in for `is_solution_design_complete` in
      // backend/app/services/deduction/solution_design.py — the first and last numbered
      // sections plus the three architectural views. Mirroring all 26 required headings
      // here would go stale; what this has to get right is not calling a hand-edited
      // document incomplete, which would queue a regeneration over the user's edit.
      this.draftPlan.solution_design_complete =
        body.includes('## 1. Project Summary') &&
        body.includes('#### 3.2.1 Logical View') &&
        body.includes('#### 3.2.2 Development View') &&
        body.includes('#### 3.2.3 Deployment View') &&
        body.includes('## 7. Acceptance Criteria') &&
        !(body.trimEnd().split('\n').pop() || '').trimStart().startsWith('#');
    }

    if (this.draftPlan.source_tree?.length) {
      const file = this.draftPlan.source_tree.find((f) => f.path === path);
      if (file) {
        file.content = body;
      } else if (
        !path.startsWith('.cursor/') &&
        !path.startsWith('.claude/') &&
        !path.startsWith('.windsurf/') &&
        !path.startsWith('.agents/') &&
        !path.startsWith('.github/instructions/') &&
        !path.startsWith('.github/agents/') &&
        !path.startsWith('.github/skills/') &&
        path !== '.github/copilot-instructions.md' &&
        !path.endsWith('README.md') &&
        !path.endsWith('README.agentcraft.md') &&
        path !== WBS_FILENAME &&
        path !== SDD_FILENAME &&
        path !== 'AGENTS.md' &&
        path !== '.mcp.json'
      ) {
        this.draftPlan.source_tree.push({
          path,
          purpose: `Scaffold file ${path}`,
          content: body,
        });
      }
    }

    this.fileContents[path] = body;
    // Save returns to the preview, so drop edit mode first — the setter only refreshes
    // the preview snapshot while the preview is the visible pane.
    this.fileEditMode = false;
    this.selectedFileBody = body;

    this.run(this.api.updatePlan(this.project.id, this.draftPlan), (p) => {
      this.project = p;
      if (p.plan) {
        this.draftPlan = structuredClone(p.plan);
        this.captureSavedPlan();
      }
      this.fileEditMode = false;
      this.saveMessage = 'Saved permanently — live markdown preview.';
      const keepPath = path;
      const keepBody = body;
      this.api.previewFiles(this.project!.id).subscribe({
        next: (r) => {
          this.previewFiles = r.files || [];
          this.previewImages = r.images || [];
          this.fileContents = r.contents || {};
          this.rebuildFileTree();
          this.selectedFile = keepPath;
          this.fileEditMode = false;
          // Overrides must win — use server content if present, else local body
          this.selectedFileBody =
            this.fileContents[keepPath] != null ? this.fileContents[keepPath] : keepBody;
          this.fileContents[keepPath] = this.selectedFileBody;
        },
        error: () => {
          this.fileEditMode = false;
          this.selectedFile = keepPath;
          this.selectedFileBody = keepBody;
          this.fileContents[keepPath] = keepBody;
        },
      });
    });
  }

  private stripFrontmatter(text: string): string {
    if (!text.startsWith('---')) return text.trim();
    const parts = text.split(/^---\s*$/m);
    if (parts.length >= 3) {
      return parts.slice(2).join('---').trim();
    }
    return text.trim();
  }

  goExport(): void {
    this.continueToExport();
  }

  /** Save if needed, download zip, then open Sessions. */
  continueToExport(): void {
    if (!this.project || this.busy) return;
    if (this.isPlanDirty && this.draftPlan) {
      this.busy = true;
      this.error = '';
      this.api.updatePlan(this.project.id, this.draftPlan).subscribe({
        next: (p) => {
          this.project = p;
          this.hydrateDraft();
          this.captureSavedPlan();
          this.saveMessage = 'Saved before export.';
          this.busy = false;
          this.doExport();
        },
        error: (err) => {
          this.busy = false;
          this.error = err?.error?.message || 'Could not save plan before export';
        },
      });
      return;
    }
    this.doExport();
  }

  private defaultExportFile(files: string[]): string {
    const normalized = files.map((f) => this.normalizePath(f));
    const readme =
      normalized.find((p) => p === 'README.md') ||
      normalized.find((p) => p.endsWith('/README.md')) ||
      normalized.find((p) => p === 'README.agentcraft.md') ||
      normalized.find((p) => p.endsWith('/README.agentcraft.md'));
    const wbs = normalized.find((p) => p === WBS_FILENAME);
    const sdd = normalized.find((p) => p === SDD_FILENAME);
    if (readme) return readme;
    if (wbs) return wbs;
    // Behind the work breakdown only because that is the file a returning user opens most
    // often; SDD.md still wins over any scaffold file.
    if (sdd) return sdd;

    if (this.step !== 'export') {
      const main =
        normalized.find((p) => p === 'main.py') ||
        normalized.find((p) => p.endsWith('/main.py'));
      if (main) return main;
      return files[0];
    }

    const agentsDoc =
      normalized.find((p) => p.endsWith('/AGENTS.md')) ||
      normalized.find((p) => p === 'AGENTS.md');
    if (agentsDoc) return agentsDoc;

    return files[0];
  }

  doExport(): void {
    if (!this.project) return;
    const id = this.project.id;
    const filename = `${this.exportZipBasename(this.project.name)}.zip`;
    this.busy = true;
    this.error = '';
    this.saveMessage = 'Preparing download…';
    this.api
      .exportZip(id)
      .pipe(switchMap(() => this.api.downloadZipBlob(id)))
      .subscribe({
        next: (blob) => {
          this.busy = false;
          if (blob.type && blob.type.includes('application/json')) {
            void blob.text().then((t) => {
              try {
                const j = JSON.parse(t) as { message?: string };
                this.error = j.message || 'Download failed';
              } catch {
                this.error = 'Download failed';
              }
            });
            return;
          }
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          a.download = filename;
          a.click();
          URL.revokeObjectURL(url);
          this.saveMessage = 'Download started — opening Sessions…';
          setTimeout(() => this.router.navigateByUrl('/sessions'), 500);
        },
        error: (err) => {
          this.busy = false;
          this.error = err?.error?.message || 'Export / download failed';
          this.auth.handleAuthError(err);
        },
      });
  }

  goSessions(): void {
    this.router.navigateByUrl('/sessions');
  }

  /** Close current session and start a fresh wizard workspace. */
  startNewProject(): void {
    this.router.navigateByUrl('/wizard');
  }

  goAdmin(): void {
    this.router.navigateByUrl('/admin');
  }

  logout(): void {
    this.auth.logout();
  }

  private exportZipBasename(name: string | null | undefined): string {
    const raw = (name || 'agentcraft-project').trim();
    const slug = raw
      .replace(/[^\w\s-]+/g, '')
      .trim()
      .replace(/[\s_]+/g, '-')
      .replace(/-+/g, '-')
      .replace(/^-|-$/g, '')
      .toLowerCase();
    return (slug || 'agentcraft-project').slice(0, 64);
  }

  platformLabel(id: string | null | undefined): string {
    return this.platforms.find((p) => p.id === id)?.label || id || '—';
  }
}
