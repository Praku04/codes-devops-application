import { ChangeDetectorRef, Component, OnDestroy, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { finalize, timeout } from 'rxjs';
import { ApiService } from '../services/api.service';
import { AuthService } from '../services/auth.service';
import {
  AdminSessionInfo,
  AdminUserView,
  ArchitectureKind,
  DiagramKind,
  DiagramRevision,
  DiagramScope,
  DiagramSetView,
  DocumentFormat,
  isArchitectureKind,
  isDiagramKind,
} from '../models';
import {
  ARCHITECTURE_RENDER_SCALE,
  ARCHITECTURE_VIEWS,
  DiagramViewerComponent,
} from '../shared/diagram-viewer/diagram-viewer.component';

interface AdminCounts {
  pending: number;
  approved: number;
  rejected: number;
  deleted: number;
  sessions: number;
}

type AdminFilter = 'all' | 'pending' | 'approved' | 'rejected' | 'deleted';

@Component({
  selector: 'ac-admin',
  standalone: true,
  imports: [CommonModule, DiagramViewerComponent],
  templateUrl: './admin.component.html',
  styleUrl: './admin.component.css',
})
export class AdminComponent implements OnInit, OnDestroy {
  private cdr = inject(ChangeDetectorRef);

  users: AdminUserView[] = [];
  busy = false;
  error = '';
  success = '';
  filter: AdminFilter = 'pending';
  expandedUserId: string | null = null;
  expandedSessionId: string | null = null;
  actionId: string | null = null;
  /** User awaiting reject confirmation — an in-app modal, never a blocking window.confirm. */
  pendingReject: AdminUserView | null = null;
  /** User awaiting delete confirmation. Revokes login; workspaces are held for restore. */
  pendingDelete: AdminUserView | null = null;
  /** Deleted user awaiting purge confirmation — the one action that truly erases data. */
  pendingPurge: AdminUserView | null = null;

  // ── The expanded session's blueprint ────────────────────────────────
  //
  // Fetched when a session is opened, not with the user list: the three PNGs are megabytes
  // each at 3× supersampling, and /admin/users returns every session of every user. The
  // session card already carries the metadata (`has_diagrams`, version, counts) to decide
  // whether there is anything to fetch, so nothing is requested speculatively.
  //
  // Only one session is ever expanded, so one set of URLs is enough — but they are object
  // URLs over blobs, so they must be revoked when the session closes, or the browser holds
  // the images for the life of the tab.

  /** The session whose blueprint is loaded. Guards a late response against a fast click. */
  private diagramSession: string | null = null;
  diagramSet: DiagramSetView | null = null;
  diagramUrls: Partial<Record<DiagramKind, string>> = {};
  diagramKind: DiagramKind = 'sipoc';
  diagramLoading = false;
  diagramError = '';

  /**
   * A version from the change history the admin is looking at, or null for the current one.
   *
   * The same read-only route serves it with `?version=`, and its URLs are kept apart from the
   * current set's so going back does not re-download anything. Superseded PNGs are not stored —
   * only the views needed to redraw them — so this is a render on demand, which is why an entry
   * whose views have aged out is disabled rather than hidden.
   */
  historyVersion: number | null = null;
  historyUrls: Partial<Record<DiagramKind, string>> = {};
  historyLoading = false;

  // ── The expanded session's architectural views ──────────────────────
  //
  // SDD.md §3.2's logical, development and deployment figures — the same three the owner sees
  // in the studio's `docs/architecture/` rows and embedded in SDD.md itself. They were missing
  // here, which meant an admin reviewing a session could read the process blueprint but not the
  // software design that came out of it.
  //
  // Fetched alongside the blueprint when a session is opened, and only when `has_plan` says
  // there is a design to draw. Same blob-and-revoke discipline as `diagramUrls`.

  /** The three views and the supersample factor, shared with the wizard so labels cannot drift. */
  readonly architectureViews = ARCHITECTURE_VIEWS;
  readonly architectureRenderScale = ARCHITECTURE_RENDER_SCALE;
  /** Whether this session has a plan, hence an SDD and three views to show. */
  architectureAvailable = false;
  architectureUrls: Partial<Record<ArchitectureKind, string>> = {};
  architectureKind: ArchitectureKind = 'logical';
  architectureLoading = false;
  architectureError = '';

  /**
   * The expanded session's SDD.md, as something to read away from this panel.
   *
   * The admin route serves the same bytes the owner's does — one service method lays the
   * document out from the stored markdown — so a reviewer downloading a PDF here is reading
   * what the user has, not a second rendering of it. Single-flight, like the URLs above: only
   * one session is ever expanded, and both buttons wait on whichever render is running.
   */
  sddDownloading: DocumentFormat | null = null;
  sddDownloadMessage = '';

  /**
   * Which of the two viewers the bare-key shortcuts belong to.
   *
   * `ac-diagram-viewer` listens on the document, because its keys are bare (`f`, `0`, `1`, the
   * arrows) and nothing else on the page wants them — which was sound while only one viewer was
   * ever mounted. This panel now mounts two, and without a gate `f` would ask both to go full
   * screen and an arrow key would page both tab strips. `live` is the input that exists for
   * exactly this, and the pointer decides: whichever picture the admin is over is the one the
   * keyboard drives. Defaults to the blueprint, which is the block above.
   */
  keyboardOwner: 'blueprint' | 'architecture' = 'blueprint';

  claimKeyboard(owner: 'blueprint' | 'architecture'): void {
    if (this.keyboardOwner === owner) return;
    this.keyboardOwner = owner;
    this.cdr.detectChanges();
  }

  constructor(
    public auth: AuthService,
    private api: ApiService,
    private router: Router,
  ) {}

  ngOnInit(): void {
    this.reload();
  }

  goStudio(): void {
    void this.router.navigateByUrl('/sessions');
  }

  startNewWorkspace(): void {
    void this.router.navigateByUrl('/wizard');
  }

  /**
   * Memoised derived views.
   *
   * The template reads `filtered` twice and the four counters six times per render,
   * and each one used to walk the whole user list on every change-detection pass —
   * so clicking a filter chip or expanding a user got slower as users accumulated.
   * A revision counter bumped whenever `users` is replaced is enough to invalidate.
   */
  private usersRevision = 0;
  private countsCache: { key: string; value: AdminCounts } | null = null;
  private filteredCache: { key: string; value: AdminUserView[] } | null = null;

  private get derivedKey(): string {
    return `${this.usersRevision}|${this.filter}`;
  }

  private get counts(): AdminCounts {
    const key = this.derivedKey;
    if (this.countsCache?.key === key) return this.countsCache.value;
    let pending = 0;
    let approved = 0;
    let rejected = 0;
    let deleted = 0;
    let sessions = 0;
    for (const u of this.users) {
      // Admins are never "pending approval" in the UI's sense — they bypass the gate.
      if (u.role !== 'admin' && u.status === 'pending') pending++;
      else if (u.role !== 'admin' && u.status === 'approved') approved++;
      if (u.status === 'rejected') rejected++;
      if (u.status === 'deleted') {
        deleted++;
        // Their workspaces are held, not live — counting them would inflate the KPI.
        continue;
      }
      sessions += u.session_count || 0;
    }
    const value = { pending, approved, rejected, deleted, sessions };
    this.countsCache = { key, value };
    return value;
  }

  get filtered(): AdminUserView[] {
    const key = this.derivedKey;
    if (this.filteredCache?.key === key) return this.filteredCache.value;
    // "All" means all *live* accounts — deleted ones are an archive with their own chip,
    // so mixing them in would make the list read as if those people still had access.
    const value =
      this.filter === 'all'
        ? this.users.filter((u) => u.status !== 'deleted')
        : this.users.filter((u) => u.status === this.filter);
    this.filteredCache = { key, value };
    return value;
  }

  get pendingCount(): number {
    return this.counts.pending;
  }

  get approvedCount(): number {
    return this.counts.approved;
  }

  get rejectedCount(): number {
    return this.counts.rejected;
  }

  get deletedCount(): number {
    return this.counts.deleted;
  }

  get totalSessions(): number {
    return this.counts.sessions;
  }

  /** Capped so a long user list never waits on a multi-second cascade. */
  cardDelay(index: number): number {
    return Math.min(index, 8) * 30;
  }

  reload(): void {
    this.busy = true;
    this.error = '';
    this.api
      .listAdminUsers()
      .pipe(
        timeout(20000),
        finalize(() => {
          this.busy = false;
          this.cdr.detectChanges();
        }),
      )
      .subscribe({
        next: (rows) => {
          this.users = rows;
          this.usersRevision++;
          if (this.filter === 'pending' && this.pendingCount === 0 && this.users.length) {
            this.filter = 'all';
          }
          this.cdr.detectChanges();
        },
        error: (err) => {
          this.error = err?.error?.message || 'Could not load users';
          this.auth.handleAuthError(err);
          this.cdr.detectChanges();
        },
      });
  }

  setFilter(f: AdminFilter): void {
    this.filter = f;
    this.expandedUserId = null;
    this.expandedSessionId = null;
    this.clearDiagrams();
    this.pendingReject = null;
    this.pendingDelete = null;
    this.pendingPurge = null;
    this.cdr.detectChanges();
  }

  toggleUser(id: string): void {
    if (this.expandedUserId === id) {
      this.expandedUserId = null;
      this.expandedSessionId = null;
    } else {
      this.expandedUserId = id;
      this.expandedSessionId = null;
    }
    // Collapsing a user closes its open session too, so its images leave the screen with it.
    this.clearDiagrams();
    this.cdr.detectChanges();
  }

  toggleSession(s: AdminSessionInfo, ev: Event): void {
    ev.stopPropagation();
    const opening = this.expandedSessionId !== s.id;
    this.expandedSessionId = opening ? s.id : null;
    // Either way the previous session's blobs are finished with — only one is ever expanded.
    this.clearDiagrams();
    if (opening) this.loadSessionDiagrams(s);
    this.cdr.detectChanges();
  }

  formatIst(iso: string | null | undefined): string {
    if (!iso) return '—';
    const raw = iso.trim();
    const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw) ? raw : `${raw}Z`;
    const t = Date.parse(normalized);
    if (!Number.isFinite(t)) return '—';
    return (
      new Intl.DateTimeFormat('en-IN', {
        timeZone: 'Asia/Kolkata',
        day: '2-digit',
        month: 'short',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        hour12: true,
      }).format(new Date(t)) + ' IST'
    );
  }

  approve(u: AdminUserView, ev: Event): void {
    ev.stopPropagation();
    this.actionId = u.id;
    this.success = '';
    this.error = '';
    this.cdr.detectChanges();
    this.api.approveUser(u.id).subscribe({
      next: (updated) => {
        this.actionId = null;
        this.replaceUser(updated);
        this.success = `Approved ${updated.name || updated.email} — they can log in now.`;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.actionId = null;
        this.error = err?.error?.message || 'Approve failed';
        this.cdr.detectChanges();
      },
    });
  }

  /** Open the reject confirmation. Rejecting is not reversible from this screen. */
  askReject(u: AdminUserView, ev: Event): void {
    ev.stopPropagation();
    this.pendingReject = u;
    this.pendingDelete = null;
    this.success = '';
    this.error = '';
    this.cdr.detectChanges();
  }

  cancelReject(ev?: Event): void {
    ev?.stopPropagation();
    this.pendingReject = null;
    this.cdr.detectChanges();
  }

  confirmReject(ev?: Event): void {
    ev?.stopPropagation();
    const u = this.pendingReject;
    if (!u) return;
    this.actionId = u.id;
    this.error = '';
    this.cdr.detectChanges();
    this.api.rejectUser(u.id).subscribe({
      next: (updated) => {
        this.actionId = null;
        this.pendingReject = null;
        this.replaceUser(updated);
        this.success = `Rejected ${updated.name || updated.email}.`;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.actionId = null;
        this.error = err?.error?.message || 'Reject failed';
        this.cdr.detectChanges();
      },
    });
  }

  /** Open the delete confirmation. Reversible via Restore until the account is purged. */
  askDelete(u: AdminUserView, ev: Event): void {
    ev.stopPropagation();
    this.pendingDelete = u;
    this.pendingReject = null;
    this.pendingPurge = null;
    this.success = '';
    this.error = '';
    this.cdr.detectChanges();
  }

  cancelDelete(ev?: Event): void {
    ev?.stopPropagation();
    this.pendingDelete = null;
    this.cdr.detectChanges();
  }

  confirmDelete(ev?: Event): void {
    ev?.stopPropagation();
    const u = this.pendingDelete;
    if (!u) return;
    this.actionId = u.id;
    this.error = '';
    this.cdr.detectChanges();
    this.api.deleteUser(u.id).subscribe({
      next: (res) => {
        this.actionId = null;
        this.pendingDelete = null;
        // The card moves to the Deleted archive rather than vanishing, so the admin can
        // see what they just did — and undo it.
        this.replaceUser({
          ...u,
          status: 'deleted',
          previous_status: u.status,
          deleted_at: new Date().toISOString(),
          deleted_by: this.auth.user?.email || null,
          restorable: res.restorable !== false,
        });
        this.success = res.message || `Deleted ${u.name || u.email}.`;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.actionId = null;
        this.error = err?.error?.message || 'Delete failed';
        this.cdr.detectChanges();
      },
    });
  }

  /** Put a deleted account back — no confirmation, since it only grants access back. */
  restore(u: AdminUserView, ev: Event): void {
    ev.stopPropagation();
    this.actionId = u.id;
    this.success = '';
    this.error = '';
    this.cdr.detectChanges();
    this.api.restoreUser(u.id).subscribe({
      next: (res) => {
        this.actionId = null;
        this.replaceUser(res.user);
        // Jump to the tab the restored account landed on, or the card would seem to
        // disappear: it is no longer in the Deleted list the admin is looking at.
        this.filter = (res.user.status as AdminFilter) || 'all';
        this.success = res.message || `Restored ${u.name || u.email}.`;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.actionId = null;
        this.error = err?.error?.message || 'Restore failed';
        this.cdr.detectChanges();
      },
    });
  }

  /** Open the purge confirmation — the only action that destroys data for good. */
  askPurge(u: AdminUserView, ev: Event): void {
    ev.stopPropagation();
    this.pendingPurge = u;
    this.pendingDelete = null;
    this.pendingReject = null;
    this.success = '';
    this.error = '';
    this.cdr.detectChanges();
  }

  cancelPurge(ev?: Event): void {
    ev?.stopPropagation();
    this.pendingPurge = null;
    this.cdr.detectChanges();
  }

  confirmPurge(ev?: Event): void {
    ev?.stopPropagation();
    const u = this.pendingPurge;
    if (!u) return;
    this.actionId = u.id;
    this.error = '';
    this.cdr.detectChanges();
    this.api.purgeUser(u.id).subscribe({
      next: (res) => {
        this.actionId = null;
        this.pendingPurge = null;
        this.removeUser(u.id);
        this.success = res.message || `Erased ${u.email}.`;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.actionId = null;
        this.error = err?.error?.message || 'Purge failed';
        this.cdr.detectChanges();
      },
    });
  }

  private replaceUser(updated: AdminUserView): void {
    this.users = this.users.map((x) => (x.id === updated.id ? updated : x));
    this.usersRevision++;
  }

  /** Drop the card locally instead of refetching — the row is gone server-side. */
  private removeUser(id: string): void {
    this.users = this.users.filter((x) => x.id !== id);
    if (this.expandedUserId === id) {
      this.expandedUserId = null;
      this.expandedSessionId = null;
      this.clearDiagrams();
    }
    this.usersRevision++;
  }

  platformLabel(id: string | null | undefined): string {
    if (!id) return '—';
    if (id === 'claude_code') return 'Claude Code';
    if (id === 'cursor') return 'Cursor';
    if (id === 'windsurf') return 'Windsurf';
    if (id === 'github_copilot') return 'GitHub Copilot';
    return id;
  }

  stateLabel(state: string): string {
    return (state || '').replace(/_/g, ' ');
  }

  hasArtifacts(s: AdminSessionInfo): boolean {
    return (s.agent_count || 0) + (s.skill_count || 0) + (s.rule_count || 0) > 0;
  }

  // ── Which context pipeline a session followed ────────────────────────
  //
  // The two paths produce very different briefs — uploaded documents versus answers typed
  // into the guided interview — and the state name does not say which one was taken. It is
  // the first thing an admin looking at someone else's workspace needs to know, so it sits
  // on the collapsed row rather than behind the expander.

  /** `Documents` / `Interview` / `Path not chosen`, as the badge reads it. */
  pipelineLabel(s: AdminSessionInfo): string {
    if (s.path === 'docs') return 'Documents';
    if (s.path === 'interview') return 'Interview';
    return 'Path not chosen';
  }

  /** Styling hook — the three states are told apart by colour as well as by wording. */
  pipelineClass(s: AdminSessionInfo): string {
    if (s.path === 'docs') return 'docs';
    if (s.path === 'interview') return 'interview';
    return 'none';
  }

  /**
   * The sentence under the badge: what the session actually handed in.
   *
   * `path_inferred` is reported rather than hidden. Sessions created before the column
   * existed store no path, and they did take one — reading the documents or answers back
   * off the brief is the honest way to say so, and saying it out loud stops an admin
   * treating a deduction as a record.
   */
  pipelineDetail(s: AdminSessionInfo): string {
    const docs = s.document_names?.length || 0;
    const answers = s.interview_answer_count || 0;
    if (s.path === 'docs') {
      const held = docs
        ? `${docs} document${docs === 1 ? '' : 's'} uploaded`
        : 'no document has been uploaded yet';
      return `Followed the document pipeline — ${held}.`;
    }
    if (s.path === 'interview') {
      const done = answers
        ? `${answers} question${answers === 1 ? '' : 's'} answered`
        : 'no question answered yet';
      return `No documents — the brief came from the guided interview, ${done}.`;
    }
    return 'Neither pipeline was started — no documents uploaded and no interview answers given.';
  }

  /** True when the pipeline was worked out from the brief rather than read off the row. */
  pipelineInferred(s: AdminSessionInfo): boolean {
    return !!s.path_inferred;
  }

  // ── Blueprint, read-only ────────────────────────────────────

  /** Short blueprint summary for the collapsed row — enough to know whether to open it. */
  blueprintChip(s: AdminSessionInfo): string {
    if (!s.has_diagrams) return 'No blueprint';
    const chip = `Blueprint v${s.diagram_version || 1} · ${s.diagrams_frozen ? 'approved' : 'draft'}`;
    // The scope of the last change is the difference between a reworked process and a tidied
    // picture, so it belongs on the collapsed row rather than only inside the panel.
    const scope = s.diagram_last_scope;
    if (!scope) return chip;
    return scope === 'all' ? `${chip} · all views` : `${chip} · ${this.viewLabel(scope)} only`;
  }

  // ── What the follow-ups were allowed to change ───────────────────────
  //
  // A version number alone says how many times the user came back, not what they came back
  // for. A change that rewrote the process moved all three views; a change scoped to one view
  // re-drew that picture and left the process — and therefore the generated agents — alone.
  // From outside the workspace that distinction is invisible unless the panel prints it, and
  // it is the difference between "they reworked the process three times" and "they tidied up
  // one diagram three times".

  /** `All three views` / `SIPOC only`, for one change-history entry. */
  revisionScopeLabel(r: DiagramRevision): string {
    if ((r.version || 0) <= 1 && !r.instruction) return 'First draft';
    const scope = (r.scope || 'all') as DiagramScope;
    if (scope === 'all') return 'All three views';
    return `${this.viewLabel(scope)} only`;
  }

  /** Badge colour: a process change reads differently from a re-draw of one view. */
  revisionScopeKind(r: DiagramRevision): 'first' | 'all' | 'view' {
    if ((r.version || 0) <= 1 && !r.instruction) return 'first';
    return (r.scope || 'all') === 'all' ? 'all' : 'view';
  }

  /** `SIPOC` / `Process flow` / `Swimlane`. */
  viewLabel(kind: string): string {
    if (kind === 'sipoc') return 'SIPOC';
    if (kind === 'flow') return 'Process flow';
    if (kind === 'swimlane') return 'Swimlane';
    return kind;
  }

  /**
   * The follow-up line in the pipeline strip, from the session card alone.
   *
   * Written from `diagram_revision_count` and `diagram_last_scope` so it is on screen the
   * instant the session opens — the full history arrives with the blueprint a moment later.
   */
  followupSummary(s: AdminSessionInfo): string {
    if (!s.has_diagrams) return '';
    const revisions = s.diagram_revision_count || 0;
    const changes = Math.max(0, revisions - 1);
    if (!changes) return 'Never revised — the first draft is what they kept.';
    const scope = s.diagram_last_scope;
    const last =
      !scope || scope === 'all'
        ? 'the last one changed the process, so all three views were redrawn'
        : `the last one re-drew the ${this.viewLabel(scope)} only, leaving the process as it was`;
    return `${changes} follow-up change${changes === 1 ? '' : 's'} — ${last}.`;
  }

  /** The set of URLs the viewer is showing — the current version's, or a history version's. */
  get activeDiagramUrls(): Partial<Record<DiagramKind, string>> {
    return this.historyVersion !== null ? this.historyUrls : this.diagramUrls;
  }

  isViewingRevision(version: number): boolean {
    return this.historyVersion === version;
  }

  /**
   * Show the blueprint as it stood at one version from the change history.
   *
   * A real render, not a cache lookup: superseded PNGs are not kept, only the views needed to
   * redraw them, and only for the last few versions — so an entry that has aged out is left in
   * the list as a record of what changed and refuses to open.
   */
  viewRevision(version: number): void {
    const projectId = this.diagramSession;
    if (!projectId || !this.diagramSet || this.historyLoading) return;
    if (version === this.diagramSet.version) {
      this.backToLatest();
      return;
    }
    if (this.historyVersion === version) return;
    const rev = this.diagramSet.revisions.find((r) => r.version === version);
    if (rev && rev.viewable === false) {
      this.diagramError = `v${version} is too old to redraw — only the most recent versions keep their diagrams.`;
      this.cdr.detectChanges();
      return;
    }
    this.historyLoading = true;
    this.diagramError = '';
    this.cdr.detectChanges();
    this.fetchDiagramPngs(projectId, version, (urls) => {
      this.historyLoading = false;
      if (!Object.keys(urls).length) {
        this.diagramError = `Could not redraw v${version} — its diagrams are no longer stored.`;
        this.cdr.detectChanges();
        return;
      }
      this.releaseHistoryUrls();
      this.historyUrls = urls;
      this.historyVersion = version;
      // An older version has its own dimensions, so start from the first view again — the
      // viewer resets its zoom when the URLs it was handed change.
      this.diagramKind = 'sipoc';
      this.cdr.detectChanges();
    });
  }

  /** Back to the current version — its URLs were never released, so nothing is refetched. */
  backToLatest(): void {
    if (this.historyVersion === null) return;
    this.historyVersion = null;
    this.releaseHistoryUrls();
    this.diagramError = '';
    this.cdr.detectChanges();
  }

  private releaseHistoryUrls(): void {
    for (const url of Object.values(this.historyUrls)) {
      if (url) URL.revokeObjectURL(url);
    }
    this.historyUrls = {};
  }

  /** Caption under the image: which version, whether the user approved it, and its size. */
  get diagramCaption(): string {
    const set = this.diagramSet;
    if (!set) return '';
    const steps = set.model?.steps?.length || 0;
    const actors = set.model?.actors?.length || 0;
    // While a history version is on screen the counts below still describe the current model,
    // so say plainly which version the picture is — otherwise the caption reads as a lie.
    if (this.historyVersion !== null) {
      return `v${this.historyVersion} from the change history · current is v${set.version}`;
    }
    return (
      `v${set.version} · ${set.frozen ? 'approved by the user' : 'draft'} · ` +
      `${steps} steps · ${actors} actors`
    );
  }

  /**
   * The viewer owns nothing but the picture, so switching view is the panel's call.
   *
   * A string in: the viewer also carries SDD §3.2's architecture views in the wizard and so
   * cannot name `DiagramKind`. Only the blueprint's three ids get through here.
   */
  showDiagramKind(kind: string): void {
    if (!isDiagramKind(kind)) return;
    this.diagramKind = kind;
    this.cdr.detectChanges();
  }

  /** The architecture tab strip's counterpart. Only SDD §3.2's three ids get through. */
  showArchitectureKind(kind: string): void {
    if (!isArchitectureKind(kind)) return;
    this.architectureKind = kind;
    this.cdr.detectChanges();
  }

  /** Caption under the architecture figure — which design it is a drawing of. */
  get architectureCaption(): string {
    return 'the same picture SDD.md §3.2 embeds, redrawn from the session\'s current design';
  }

  /**
   * Load one session's blueprint through the admin-only read endpoints.
   *
   * They exist because every `/projects/**` handler filters by owner and 404s on someone
   * else's project — correct for the studio, useless for the person reviewing it. These two
   * are admin-gated and read-only: an admin can look at a blueprint, never redraw, edit or
   * approve one.
   */
  private loadSessionDiagrams(s: AdminSessionInfo): void {
    this.diagramSession = s.id;
    this.diagramKind = 'sipoc';
    this.diagramError = '';
    // Independent of the blueprint: a session can have a generated plan and no drawn process,
    // or the reverse, so the SDD figures are not gated on `has_diagrams`.
    this.loadSessionArchitecture(s);
    if (!s.has_diagrams) {
      this.diagramSet = null;
      return;
    }
    this.diagramLoading = true;
    this.api.adminDiagrams(s.id).subscribe({
      next: (set) => {
        // A different session was opened while this was in flight — its own fetch owns the
        // state now, and writing this answer in would put one session's picture under
        // another's heading.
        if (this.diagramSession !== s.id) return;
        this.diagramSet = set;
        if (!set) {
          this.diagramLoading = false;
          this.cdr.detectChanges();
          return;
        }
        this.fetchDiagramPngs(s.id);
      },
      error: (err) => {
        if (this.diagramSession !== s.id) return;
        this.diagramLoading = false;
        this.diagramError = err?.error?.message || 'Could not load this blueprint.';
        this.auth.handleAuthError(err);
        this.cdr.detectChanges();
      },
    });
  }

  /**
   * Pull the three PNGs as blobs and swap the object URLs in together.
   *
   * Blobs rather than a bare URL in `<img src>`: auth is a Bearer header applied by the HTTP
   * interceptor, so a browser-issued image request would arrive unauthenticated and 401. The
   * old URLs are revoked only once the new ones are in place, so the visible image is never
   * pointed at a revoked blob.
   */
  private fetchDiagramPngs(
    projectId: string,
    version?: number,
    done?: (urls: Partial<Record<DiagramKind, string>>) => void,
  ): void {
    const kinds: DiagramKind[] = ['sipoc', 'flow', 'swimlane'];
    const next: Partial<Record<DiagramKind, string>> = {};
    let pending = kinds.length;
    const settle = () => {
      if (--pending > 0) return;
      if (this.diagramSession !== projectId) {
        // The session closed mid-flight; nothing will ever show these, so do not leak them.
        for (const url of Object.values(next)) if (url) URL.revokeObjectURL(url);
        return;
      }
      if (done) {
        done(next);
        return;
      }
      const stale = Object.values(this.diagramUrls);
      this.diagramUrls = next;
      for (const url of stale) if (url) URL.revokeObjectURL(url);
      this.diagramLoading = false;
      if (!Object.keys(next).length) {
        this.diagramError = 'The blueprint is recorded but its images could not be rendered.';
      }
      this.cdr.detectChanges();
    };
    for (const kind of kinds) {
      this.api.adminDiagramPngBlob(projectId, kind, version).subscribe({
        next: (blob) => {
          next[kind] = URL.createObjectURL(blob);
          settle();
        },
        error: () => settle(),
      });
    }
  }

  /**
   * Pull SDD §3.2's three figures as blobs, the same way and for the same reason.
   *
   * One difference from the blueprint: these carry no version. They are drawn from the plan as
   * it stands, so there is no history to page through — which is also why the viewer is given
   * `scale` explicitly (the endpoint returns bytes, not the render metadata the blueprint's
   * `/diagrams` response carries), and a download stem instead of a version number.
   */
  private loadSessionArchitecture(s: AdminSessionInfo): void {
    this.architectureKind = 'logical';
    this.architectureError = '';
    this.architectureAvailable = !!s.has_plan;
    if (!s.has_plan) return;
    this.architectureLoading = true;
    const kinds: ArchitectureKind[] = ['logical', 'development', 'deployment'];
    const next: Partial<Record<ArchitectureKind, string>> = {};
    let pending = kinds.length;
    const settle = () => {
      if (--pending > 0) return;
      if (this.diagramSession !== s.id) {
        // The session closed mid-flight; nothing will show these, so do not leak them.
        for (const url of Object.values(next)) if (url) URL.revokeObjectURL(url);
        return;
      }
      const stale = Object.values(this.architectureUrls);
      this.architectureUrls = next;
      for (const url of stale) if (url) URL.revokeObjectURL(url);
      this.architectureLoading = false;
      if (!Object.keys(next).length) {
        this.architectureError =
          'This session has a design, but its architecture views could not be rendered.';
      }
      this.cdr.detectChanges();
    };
    for (const kind of kinds) {
      this.api.adminArchitecturePngBlob(s.id, kind).subscribe({
        next: (blob) => {
          next[kind] = URL.createObjectURL(blob);
          settle();
        },
        error: () => settle(),
      });
    }
  }

  /**
   * Download this session's SDD.md as a PDF or a Word document.
   *
   * Read-only, like everything else in this panel: the document is laid out from the markdown
   * the user's session already holds, never regenerated. The filename carries the project so a
   * reviewer working through several sessions does not end up with three files called `SDD`.
   */
  downloadSolutionDesign(s: AdminSessionInfo, fmt: DocumentFormat, ev: Event): void {
    ev.stopPropagation();
    if (this.sddDownloading) return;
    this.sddDownloading = fmt;
    this.sddDownloadMessage = `Building the ${fmt.toUpperCase()}…`;
    this.error = '';
    this.cdr.detectChanges();
    this.api
      .adminSolutionDesignBlob(s.id, fmt)
      .pipe(
        finalize(() => {
          this.sddDownloading = null;
          this.cdr.detectChanges();
        }),
      )
      .subscribe({
        next: (blob) => {
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          a.download = `${this.sessionFileStem(s)}-SDD.${fmt}`;
          a.click();
          URL.revokeObjectURL(url);
          this.sddDownloadMessage = `${fmt.toUpperCase()} downloaded.`;
        },
        error: (err) => {
          this.sddDownloadMessage = '';
          this.error =
            err?.error?.message || `Could not build the ${fmt.toUpperCase()} for this session.`;
          this.auth.handleAuthError(err);
        },
      });
  }

  /** A session name reduced to something safe to put in a filename. */
  private sessionFileStem(s: AdminSessionInfo): string {
    const slug = (s.name || 'session')
      .replace(/[^\w\s-]+/g, '')
      .trim()
      .replace(/[\s_]+/g, '-')
      .replace(/-+/g, '-')
      .replace(/^-|-$/g, '')
      .toLowerCase();
    return (slug || 'session').slice(0, 64);
  }

  /** Drop the loaded blueprint and architecture and hand their blobs back to the browser. */
  private clearDiagrams(): void {
    for (const url of Object.values(this.diagramUrls)) {
      if (url) URL.revokeObjectURL(url);
    }
    this.diagramUrls = {};
    for (const url of Object.values(this.architectureUrls)) {
      if (url) URL.revokeObjectURL(url);
    }
    this.architectureUrls = {};
    this.architectureAvailable = false;
    this.architectureLoading = false;
    this.architectureError = '';
    this.architectureKind = 'logical';
    this.sddDownloadMessage = '';
    this.releaseHistoryUrls();
    this.historyVersion = null;
    this.historyLoading = false;
    this.diagramSet = null;
    this.diagramSession = null;
    this.diagramLoading = false;
    this.diagramError = '';
  }

  ngOnDestroy(): void {
    this.clearDiagrams();
  }
}
