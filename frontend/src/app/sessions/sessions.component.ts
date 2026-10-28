import { ChangeDetectorRef, Component, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { finalize, switchMap, timeout } from 'rxjs';
import { ApiService } from '../services/api.service';
import { AuthService } from '../services/auth.service';
import { SessionSummary } from '../models';
import { API_BASE_LABEL } from '../api-base';

type SortKey = 'updated' | 'name' | 'agents';
type PlatformFilter = 'all' | 'claude_code' | 'cursor' | 'windsurf' | 'github_copilot';
type StateFilter = 'all' | 'EXPORTED' | 'READY_FOR_REVIEW' | 'in_progress';
type ViewMode = 'table' | 'board';

@Component({
  selector: 'ac-sessions',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './sessions.component.html',
  styleUrl: './sessions.component.css',
})
export class SessionsComponent implements OnInit {
  private cdr = inject(ChangeDetectorRef);

  sessions: SessionSummary[] = [];
  busy = false;
  initialLoad = true;
  error = '';
  downloadingId: string | null = null;
  deletingId: string | null = null;
  pendingDelete: SessionSummary | null = null;
  menuOpenId: string | null = null;

  query = '';
  platformFilter: PlatformFilter = 'all';
  stateFilter: StateFilter = 'all';
  sortKey: SortKey = 'updated';
  expandedId: string | null = null;
  chipFilter: string | null = null;
  viewMode: ViewMode = 'table';

  constructor(
    public auth: AuthService,
    private api: ApiService,
    private router: Router,
  ) {}

  ngOnInit(): void {
    this.reload();
  }

  reload(): void {
    this.busy = true;
    this.error = '';
    this.api
      .listSessions()
      .pipe(
        timeout(20000),
        finalize(() => {
          this.busy = false;
          this.initialLoad = false;
          this.cdr.detectChanges();
        }),
      )
      .subscribe({
        next: (rows) => {
          this.sessions = Array.isArray(rows) ? rows : [];
          this.sessionsRevision++;
          this.cdr.detectChanges();
        },
        error: (err) => {
          const timedOut = err?.name === 'TimeoutError';
          // Resolved from the browser's own location, so the hint names the URL the app is
          // actually calling — a hardcoded one sends people to the wrong place, and on a
          // remote deployment it would name loopback while the call went elsewhere.
          this.error = timedOut
            ? `API timed out — start the backend on ${API_BASE_LABEL} (see README).`
            : err?.error?.message ||
              `Could not load sessions — is the API running on ${API_BASE_LABEL}?`;
          this.auth.handleAuthError(err);
          this.cdr.detectChanges();
        },
      });
  }

  /**
   * Memoised derived views.
   *
   * The template reads `filteredSessions` 6× and `stats` 7× per render. Both used
   * to recompute from scratch every change-detection pass — regex-normalising each
   * session and re-sorting the list on every keystroke and click. Caching on a
   * cheap key makes typing and clicking independent of session count.
   */
  private statsCache: { key: string; value: ReturnType<SessionsComponent['computeStats']> } | null =
    null;
  private filteredCache: { key: string; value: SessionSummary[] } | null = null;

  /** Changes whenever anything the derived views depend on changes. */
  private get derivedKey(): string {
    return [
      this.sessions.length,
      this.sessionsRevision,
      this.query,
      this.platformFilter,
      this.stateFilter,
      this.chipFilter,
      this.sortKey,
    ].join('|');
  }

  /** Bumped when the session list itself is replaced. */
  private sessionsRevision = 0;

  private computeStats() {
    const all = this.sessions;
    let exported = 0;
    let ready = 0;
    let cursor = 0;
    let claude = 0;
    let windsurf = 0;
    let githubCopilot = 0;
    for (const s of all) {
      if (s.state === 'EXPORTED') exported++;
      else if (s.state === 'READY_FOR_REVIEW') ready++;
      if (s.platform === 'cursor') cursor++;
      else if (s.platform === 'claude_code') claude++;
      else if (s.platform === 'windsurf') windsurf++;
      else if (s.platform === 'github_copilot') githubCopilot++;
    }
    return {
      total: all.length,
      exported,
      ready,
      active: all.length - exported - ready,
      cursor,
      claude,
      windsurf,
      githubCopilot,
    };
  }

  get stats() {
    const key = this.derivedKey;
    if (this.statsCache?.key === key) return this.statsCache.value;
    const value = this.computeStats();
    this.statsCache = { key, value };
    return value;
  }

  get filteredSessions(): SessionSummary[] {
    const key = this.derivedKey;
    if (this.filteredCache?.key === key) return this.filteredCache.value;
    const value = this.computeFiltered();
    this.filteredCache = { key, value };
    return value;
  }

  private computeFiltered(): SessionSummary[] {
    const tokens = this.searchTokens(this.query);
    let rows = this.sessions.filter((s) => {
      if (this.platformFilter !== 'all' && s.platform !== this.platformFilter) return false;
      if (this.stateFilter === 'EXPORTED' && s.state !== 'EXPORTED') return false;
      if (this.stateFilter === 'READY_FOR_REVIEW' && s.state !== 'READY_FOR_REVIEW') return false;
      if (this.stateFilter === 'in_progress') {
        if (s.state === 'EXPORTED' || s.state === 'READY_FOR_REVIEW') return false;
      }
      if (this.chipFilter) {
        const needle = this.normalize(this.chipFilter);
        const names = [
          ...(s.agent_names || []),
          ...(s.skill_names || []),
          ...(s.rule_names || []),
        ].map((n) => this.normalize(n));
        if (!names.some((n) => n === needle || n.includes(needle))) return false;
      }
      if (!tokens.length) return true;
      const hay = this.normalize(
        [
          s.name,
          s.summary || '',
          s.state,
          this.stateLabel(s.state),
          s.platform || '',
          this.platformLabel(s.platform),
          this.platformShort(s.platform),
          this.artifactLine(s),
          ...(s.agent_names || []),
          ...(s.skill_names || []),
          ...(s.rule_names || []),
        ].join(' '),
      );
      // Every token must appear (AND). Supports "nexus pay", "claude exported", agent fragments.
      return tokens.every((t) => hay.includes(t));
    });

    return [...rows].sort((a, b) => {
      if (this.sortKey === 'name') return (a.name || '').localeCompare(b.name || '');
      if (this.sortKey === 'agents') return (b.agent_count || 0) - (a.agent_count || 0);
      const ta = this.parseInstant(a.updated_at);
      const tb = this.parseInstant(b.updated_at);
      return tb - ta;
    });
  }

  /** Live search handler — keeps query in sync and avoids type=search Enter quirks. */
  onSearchInput(ev: Event): void {
    const el = ev.target as HTMLInputElement | null;
    this.query = el?.value ?? '';
  }

  onSearchKeydown(ev: KeyboardEvent): void {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      ev.stopPropagation();
    }
  }

  private normalize(value: string): string {
    return (value || '')
      .toLowerCase()
      .replace(/[_/.-]+/g, ' ')
      .replace(/[^\p{L}\p{N}\s]+/gu, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  private searchTokens(raw: string): string[] {
    const n = this.normalize(raw);
    return n ? n.split(' ').filter(Boolean) : [];
  }

  get hasActiveFilters(): boolean {
    return !!(
      this.query.trim() ||
      this.platformFilter !== 'all' ||
      this.stateFilter !== 'all' ||
      this.chipFilter
    );
  }

  clearFilters(): void {
    this.query = '';
    this.platformFilter = 'all';
    this.stateFilter = 'all';
    this.chipFilter = null;
    this.cdr.detectChanges();
  }

  applyStat(kind: 'total' | 'exported' | 'ready' | 'active' | 'cursor' | 'claude' | 'windsurf' | 'githubCopilot'): void {
    this.clearFilters();
    if (kind === 'exported') this.stateFilter = 'EXPORTED';
    else if (kind === 'ready') this.stateFilter = 'READY_FOR_REVIEW';
    else if (kind === 'active') this.stateFilter = 'in_progress';
    else if (kind === 'cursor') this.platformFilter = 'cursor';
    else if (kind === 'claude') this.platformFilter = 'claude_code';
    else if (kind === 'windsurf') this.platformFilter = 'windsurf';
    else if (kind === 'githubCopilot') this.platformFilter = 'github_copilot';
    this.cdr.detectChanges();
  }

  toggleExpand(s: SessionSummary, ev?: Event): void {
    ev?.stopPropagation();
    this.menuOpenId = null;
    this.pendingDelete = null;
    this.expandedId = this.expandedId === s.id ? null : s.id;
    this.cdr.detectChanges();
  }

  isExpanded(s: SessionSummary): boolean {
    return this.expandedId === s.id;
  }

  filterByChip(name: string, ev: Event): void {
    ev.stopPropagation();
    this.chipFilter = this.chipFilter === name ? null : name;
    this.cdr.detectChanges();
  }

  toggleMenu(s: SessionSummary, ev: Event): void {
    ev.stopPropagation();
    this.pendingDelete = null;
    this.menuOpenId = this.menuOpenId === s.id ? null : s.id;
    this.cdr.detectChanges();
  }

  closeMenus(): void {
    this.menuOpenId = null;
    this.cdr.detectChanges();
  }

  openSession(s: SessionSummary, ev?: Event): void {
    ev?.stopPropagation();
    void this.router.navigate(['/wizard', s.id]);
  }

  startNew(): void {
    this.router.navigate(['/wizard']);
  }

  goAdmin(): void {
    this.router.navigateByUrl('/admin');
  }

  downloadZip(s: SessionSummary, ev: Event): void {
    ev.stopPropagation();
    this.menuOpenId = null;
    if (!s.platform) {
      this.error = 'Open the session and finish IDE selection before downloading.';
      this.cdr.detectChanges();
      return;
    }
    if (!this.canDownload(s)) {
      this.error = 'Generate agents first — Zip is available when status is Ready or Exported.';
      this.cdr.detectChanges();
      return;
    }
    this.downloadingId = s.id;
    this.error = '';
    this.cdr.detectChanges();
    const filename = `${this.slug(s.name)}.zip`;
    this.api
      .exportZip(s.id)
      .pipe(switchMap(() => this.api.downloadZipBlob(s.id)))
      .subscribe({
        next: (blob) => {
          this.downloadingId = null;
          if (blob.type && blob.type.includes('application/json')) {
            void blob.text().then((t) => {
              try {
                const j = JSON.parse(t) as { message?: string };
                this.error = j.message || 'Download failed';
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
          a.download = filename;
          a.click();
          URL.revokeObjectURL(url);
          this.reload();
          this.cdr.detectChanges();
        },
        error: (err) => {
          this.downloadingId = null;
          this.error =
            err?.error?.message || 'Export failed — open the session and generate a plan first';
          this.auth.handleAuthError(err);
          this.cdr.detectChanges();
        },
      });
  }

  platformLabel(id: string | null | undefined): string {
    if (!id) return '—';
    if (id === 'claude_code') return 'Claude';
    if (id === 'cursor') return 'Cursor';
    if (id === 'windsurf') return 'Windsurf';
    if (id === 'github_copilot') return 'GitHub Copilot';
    return id;
  }

  platformShort(id: string | null | undefined): string {
    return this.platformLabel(id);
  }

  artifactLine(s: SessionSummary): string {
    // Every IDE exports rules, Claude Code included (.claude/rules/*.md), so the R count
    // is unconditional — gating it hid a populated rules folder from this list.
    return [`${s.agent_count}A`, `${s.skill_count}S`, `${s.rule_count}R`].join(' · ');
  }

  hasArtifactNames(s: SessionSummary): boolean {
    return !!(s.agent_names?.length || s.skill_names?.length || s.rule_names?.length);
  }

  askDelete(s: SessionSummary, ev: Event): void {
    ev.stopPropagation();
    this.menuOpenId = null;
    this.pendingDelete = s;
    this.cdr.detectChanges();
  }

  cancelDelete(ev?: Event): void {
    ev?.stopPropagation();
    this.pendingDelete = null;
    this.cdr.detectChanges();
  }

  confirmDelete(ev?: Event): void {
    ev?.stopPropagation();
    const s = this.pendingDelete;
    if (!s) return;
    this.deletingId = s.id;
    this.error = '';
    this.cdr.detectChanges();
    this.api.deleteSession(s.id).subscribe({
      next: () => {
        this.deletingId = null;
        this.pendingDelete = null;
        if (this.expandedId === s.id) this.expandedId = null;
        this.sessions = this.sessions.filter((x) => x.id !== s.id);
        this.sessionsRevision++;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.deletingId = null;
        this.error = err?.error?.message || 'Could not delete session';
        if (err?.status === 401) this.auth.logout();
        this.cdr.detectChanges();
      },
    });
  }

  stateLabel(state: string): string {
    return state.replace(/_/g, ' ');
  }

  stateTone(state: string): string {
    if (state === 'EXPORTED') return 'done';
    if (state === 'READY_FOR_REVIEW') return 'ready';
    if (state === 'GENERATING') return 'busy';
    return 'draft';
  }

  canDownload(s: SessionSummary): boolean {
    if (!s.platform || s.agent_count <= 0) return false;
    return s.state === 'READY_FOR_REVIEW' || s.state === 'EXPORTED';
  }

  relativeTime(iso: string | null | undefined): string {
    if (!iso) return '—';
    const t = this.parseInstant(iso);
    if (!Number.isFinite(t)) return '—';
    let sec = Math.round((Date.now() - t) / 1000);
    if (sec < 0 && sec > -120) sec = 0;
    if (sec < 0) return 'just now';
    if (sec < 60) return 'just now';
    if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
    if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
    if (sec < 86400 * 7) return `${Math.floor(sec / 86400)}d ago`;
    return this.formatIst(t);
  }

  /** Absolute timestamp in India Standard Time for tooltips / labels. */
  formatIst(isoOrMs: string | number | null | undefined): string {
    const t = typeof isoOrMs === 'number' ? isoOrMs : this.parseInstant(isoOrMs);
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

  private parseInstant(iso: string | null | undefined): number {
    if (!iso) return NaN;
    const raw = iso.trim();
    // Naive ISO (no offset) was historically UTC — treat as UTC; IST strings include +05:30
    const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw) ? raw : `${raw}Z`;
    return Date.parse(normalized);
  }

  private slug(name: string): string {
    return (
      name
        .trim()
        .replace(/[^\w\s-]+/g, '')
        .replace(/[\s_]+/g, '-')
        .replace(/-+/g, '-')
        .toLowerCase()
        .slice(0, 64) || 'agentcraft-project'
    );
  }
}
