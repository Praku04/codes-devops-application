import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import {
  ArchitectureKind,
  DiagramKind,
  DiagramScope,
  DiagramSetView,
  DocumentFormat,
  PlatformInfo,
  ProjectPlan,
  ProjectStatus,
  SessionSummary,
  AdminUserView,
  DeleteUserResult,
  PurgeUserResult,
  RestoreUserResult,
} from '../models';
import { API_BASE } from '../api-base';

/** Resolved from the browser's location + API_PORT — see api-base.ts. */
const API = API_BASE;

export interface ExportPreview {
  files: string[];
  contents: Record<string, string>;
  /**
   * Paths in `files` whose bytes are not text — the blueprint PNGs under `docs/diagrams/`.
   *
   * They are deliberately absent from `contents`: the preview is polled while
   * WORKBREAKDOWN.md generates, and each render is megabytes. A viewer fetches the one it
   * needs from the diagrams endpoint.
   */
  images?: string[];
}

@Injectable({ providedIn: 'root' })
export class ApiService {
  constructor(private http: HttpClient) {}

  health(): Observable<{ status: string }> {
    return this.http.get<{ status: string }>(`${API}/health`);
  }

  listSessions(): Observable<SessionSummary[]> {
    return this.http.get<SessionSummary[]>(`${API}/api/v1/projects/sessions`);
  }

  listAdminUsers(): Observable<AdminUserView[]> {
    return this.http.get<AdminUserView[]>(`${API}/api/v1/admin/users`);
  }

  approveUser(userId: string): Observable<AdminUserView> {
    return this.http.post<AdminUserView>(`${API}/api/v1/admin/users/${userId}/approve`, {});
  }

  rejectUser(userId: string): Observable<AdminUserView> {
    return this.http.post<AdminUserView>(`${API}/api/v1/admin/users/${userId}/reject`, {});
  }

  /** Super admin only — revokes login; workspaces are held so restore can undo it. */
  deleteUser(userId: string): Observable<DeleteUserResult> {
    return this.http.delete<DeleteUserResult>(`${API}/api/v1/admin/users/${userId}`);
  }

  /** Undo a delete — same password, prior status, workspaces reattached. */
  restoreUser(userId: string): Observable<RestoreUserResult> {
    return this.http.post<RestoreUserResult>(`${API}/api/v1/admin/users/${userId}/restore`, {});
  }

  /** Erase a deleted account for good — archive plus workspaces. Not restorable. */
  purgeUser(userId: string): Observable<PurgeUserResult> {
    return this.http.delete<PurgeUserResult>(`${API}/api/v1/admin/users/${userId}/purge`);
  }

  deleteSession(id: string): Observable<void> {
    return this.http.delete<void>(`${API}/api/v1/projects/${id}`);
  }

  createProject(name: string): Observable<ProjectStatus> {
    return this.http.post<ProjectStatus>(`${API}/api/v1/projects`, { name });
  }

  getProject(id: string): Observable<ProjectStatus> {
    return this.http.get<ProjectStatus>(`${API}/api/v1/projects/${id}`);
  }

  setPath(id: string, path: 'docs' | 'interview'): Observable<ProjectStatus> {
    return this.http.post<ProjectStatus>(`${API}/api/v1/projects/${id}/path`, { path });
  }

  rollback(id: string): Observable<ProjectStatus> {
    return this.http.post<ProjectStatus>(`${API}/api/v1/projects/${id}/rollback`, {});
  }

  submitDocuments(id: string, statement: string, files: File[]): Observable<ProjectStatus> {
    const form = new FormData();
    form.append('problem_statement', statement);
    for (const f of files) {
      form.append('files', f, f.name);
    }
    return this.http.post<ProjectStatus>(`${API}/api/v1/projects/${id}/documents`, form);
  }

  answerInterview(
    id: string,
    answers: Record<string, string>,
    name?: string,
  ): Observable<ProjectStatus> {
    return this.http.post<ProjectStatus>(`${API}/api/v1/projects/${id}/interview`, {
      answers,
      ...(name?.trim() ? { name: name.trim() } : {}),
    });
  }

  setPlatform(id: string, platform: string): Observable<ProjectStatus> {
    return this.http.post<ProjectStatus>(`${API}/api/v1/projects/${id}/platform`, { platform });
  }

  generate(id: string, demo = false): Observable<ProjectStatus> {
    return this.http.post<ProjectStatus>(
      `${API}/api/v1/projects/${id}/generate?demo=${demo}&wait=false`,
      {}
    );
  }

  // ── Process blueprint ─────────────────────────────────────────────────────
  //
  // Generation and follow-up run in the background; the wizard polls getProject() and
  // watches `diagram_version` for the bump, the same way it watches state during generate.

  /** Draw the first blueprint. Resolves as soon as the job is queued. */
  generateDiagrams(id: string, demo = false): Observable<ProjectStatus> {
    return this.http.post<ProjectStatus>(
      `${API}/api/v1/projects/${id}/diagrams?demo=${demo}&wait=false`,
      {}
    );
  }

  getDiagrams(id: string): Observable<DiagramSetView | null> {
    return this.http.get<DiagramSetView | null>(`${API}/api/v1/projects/${id}/diagrams`);
  }

  /** Apply one plain-language change; all three views are redrawn from the revised model. */
  /**
   * Apply one plain-language change.
   *
   * `scope` is sent every time, including the default: the server defaults it too, but a
   * follow-up whose scope is implicit is one the change history cannot label honestly.
   */
  refineDiagrams(
    id: string,
    instruction: string,
    scope: DiagramScope = 'all'
  ): Observable<ProjectStatus> {
    return this.http.post<ProjectStatus>(
      `${API}/api/v1/projects/${id}/diagrams/followup?wait=false`,
      { instruction, scope }
    );
  }

  freezeDiagrams(id: string, frozen: boolean): Observable<DiagramSetView> {
    const action = frozen ? 'freeze' : 'unfreeze';
    return this.http.post<DiagramSetView>(
      `${API}/api/v1/projects/${id}/diagrams/${action}`,
      {}
    );
  }

  deleteDiagrams(id: string): Observable<ProjectStatus> {
    return this.http.delete<ProjectStatus>(`${API}/api/v1/projects/${id}/diagrams`);
  }

  /**
   * PNG bytes as a blob. Same reason as downloadZipBlob: auth is a Bearer header applied by
   * the interceptor, so a bare URL in an `<img src>` is an unauthenticated request and 401s.
   */
  /**
   * One diagram PNG. `version` asks for an earlier version from the change history — omit it
   * for the current one. The server re-renders an archived version from the views it keeps,
   * and 404s once that version has aged out.
   */
  diagramPngBlob(id: string, kind: DiagramKind, version?: number): Observable<Blob> {
    return this.http.get(`${API}/api/v1/projects/${id}/diagrams/${kind}.png`, {
      responseType: 'blob',
      params: version ? { version: String(version) } : {},
    });
  }

  /**
   * One of SDD.md §3.2's architectural views — `logical`, `development` or `deployment`.
   *
   * Not `diagramPngBlob`: these carry no version (they are a drawing of the current design,
   * not an approved blueprint) and come from a different renderer. Same blob reason.
   */
  architecturePngBlob(id: string, kind: ArchitectureKind): Observable<Blob> {
    return this.http.get(`${API}/api/v1/projects/${id}/plan/architecture/${kind}.png`, {
      responseType: 'blob',
    });
  }

  /**
   * SDD.md as a PDF or a DOCX — the document this app previews, laid out for paper with §3.2's
   * three figures embedded. A blob for the same reason every download here is one: the token
   * travels in a header, so a bare URL in an `<a href>` downloads a 401 page.
   */
  solutionDesignBlob(id: string, fmt: DocumentFormat): Observable<Blob> {
    return this.http.get(`${API}/api/v1/projects/${id}/plan/solution-design.${fmt}`, {
      responseType: 'blob',
    });
  }

  // ── Admin's read-only view of someone else's blueprint ────────────────────
  //
  // Separate endpoints rather than the two above: every /projects handler filters by
  // owner and 404s on another user's project, which is right for a user and wrong for
  // the admin panel. These are admin-gated server-side and cannot draw or edit.

  adminDiagrams(projectId: string): Observable<DiagramSetView | null> {
    return this.http.get<DiagramSetView | null>(
      `${API}/api/v1/admin/projects/${projectId}/diagrams`
    );
  }

  adminDiagramPngBlob(
    projectId: string,
    kind: DiagramKind,
    version?: number
  ): Observable<Blob> {
    return this.http.get(`${API}/api/v1/admin/projects/${projectId}/diagrams/${kind}.png`, {
      responseType: 'blob',
      params: version ? { version: String(version) } : {},
    });
  }

  /** The same three SDD §3.2 figures the owner sees, for a session the admin does not own. */
  adminArchitecturePngBlob(projectId: string, kind: ArchitectureKind): Observable<Blob> {
    return this.http.get(`${API}/api/v1/admin/projects/${projectId}/architecture/${kind}.png`, {
      responseType: 'blob',
    });
  }

  /** A user's SDD.md as a PDF or DOCX — the same document `solutionDesignBlob` gives its owner. */
  adminSolutionDesignBlob(projectId: string, fmt: DocumentFormat): Observable<Blob> {
    return this.http.get(`${API}/api/v1/admin/projects/${projectId}/solution-design.${fmt}`, {
      responseType: 'blob',
    });
  }

  updatePlan(id: string, plan: ProjectPlan): Observable<ProjectStatus> {
    return this.http.put<ProjectStatus>(`${API}/api/v1/projects/${id}/plan`, plan);
  }

  /** Bedrock-generate detailed WORKBREAKDOWN.md (existing projects; runs in background). */
  generateWorkBreakdown(id: string): Observable<ProjectStatus> {
    return this.http.post<ProjectStatus>(
      `${API}/api/v1/projects/${id}/plan/work-breakdown/generate`,
      {}
    );
  }

  /** Bedrock-generate detailed SDD.md (existing projects; runs in background). */
  generateSolutionDesign(id: string): Observable<ProjectStatus> {
    return this.http.post<ProjectStatus>(
      `${API}/api/v1/projects/${id}/plan/solution-design/generate`,
      {}
    );
  }

  exportZip(id: string): Observable<{ files: string[]; download_path?: string }> {
    return this.http.post<{ files: string[]; download_path?: string }>(
      `${API}/api/v1/projects/${id}/export`,
      { mode: 'zip' }
    );
  }

  /** Authenticated zip bytes (Bearer token via interceptor). Do not use bare download URLs in <a href>. */
  downloadZipBlob(id: string): Observable<Blob> {
    return this.http.get(`${API}/api/v1/projects/${id}/export/download`, {
      responseType: 'blob',
    });
  }

  previewFiles(id: string): Observable<ExportPreview> {
    return this.http.get<ExportPreview>(`${API}/api/v1/projects/${id}/export/preview`);
  }

  expandBrief(seed: string): Observable<{ expanded: string; was_short: boolean }> {
    return this.http.post<{ expanded: string; was_short: boolean }>(
      `${API}/api/v1/projects/meta/expand-brief`,
      { seed }
    );
  }

  /**
   * Parallel async expand with live NDJSON progress (one line per event).
   * Uses fetch so we can stream; Bearer token from localStorage.
   */
  async expandBriefParallel(
    seed: string,
    onEvent: (ev: {
      event: string;
      sections?: string[];
      section?: string;
      title?: string;
      ok?: boolean;
      expanded?: string;
      was_short?: boolean;
      message?: string;
      mode?: string;
    }) => void,
  ): Promise<{ expanded: string; was_short: boolean }> {
    const token = localStorage.getItem('agentcraft_token');
    const res = await fetch(`${API}/api/v1/projects/meta/expand-brief/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({ seed }),
    });
    if (!res.ok) {
      let message = `Expand failed (${res.status})`;
      try {
        const j = (await res.json()) as { message?: string };
        if (j.message) message = j.message;
      } catch {
        /* ignore */
      }
      throw new Error(message);
    }
    if (!res.body) {
      throw new Error('Expand stream returned no body');
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let expanded = '';
    let wasShort = true;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        const ev = JSON.parse(trimmed) as {
          event: string;
          sections?: string[];
          section?: string;
          title?: string;
          ok?: boolean;
          expanded?: string;
          was_short?: boolean;
          message?: string;
          mode?: string;
        };
        onEvent(ev);
        if (ev.event === 'done' && ev.expanded) {
          expanded = ev.expanded;
          wasShort = !!ev.was_short;
        }
        if (ev.event === 'error') {
          throw new Error(ev.message || 'Could not expand brief');
        }
      }
    }
    if (!expanded) {
      throw new Error('Expand stream ended without a result');
    }
    return { expanded, was_short: wasShort };
  }

  platforms(): Observable<PlatformInfo[]> {
    return this.http.get<PlatformInfo[]>(`${API}/api/v1/projects/meta/platforms`);
  }
}
