import { CommonModule } from '@angular/common';
import {
  ChangeDetectorRef,
  Component,
  ElementRef,
  EventEmitter,
  HostListener,
  Input,
  OnChanges,
  OnDestroy,
  Output,
  SimpleChanges,
  ViewChild,
} from '@angular/core';
import { DiagramImage } from '../../models';

/**
 * One view this viewer can show, as the UI describes it.
 *
 * `hint` is what the picture contains, `use` is the question it answers. Both are shown:
 * "Suppliers, inputs, process, outputs, customers" only means something to a reader who
 * already knows SIPOC, and the person approving — or reviewing — this blueprint often does not.
 *
 * `id` is a plain string, not `DiagramKind`: the same viewer shows SDD §3.2's three
 * architecture views, whose ids are `ArchitectureKind`. Nothing in here inspects the id beyond
 * using it as a key into `urls`, so the two families cost one type rather than two components —
 * and a caller narrows what comes back out of `activeChange` with `isDiagramKind` /
 * `isArchitectureKind` from `models.ts`.
 */
export interface DiagramView {
  id: string;
  label: string;
  hint: string;
  use: string;
}

/**
 * The three views in the order the method reads them — SIPOC scopes, flow orders, swimlane
 * assigns. Exported so the wizard and the admin console describe them identically; a diagram
 * that means one thing to the person who drew it and another to the admin reviewing it would
 * be worse than no label at all.
 */
export const DIAGRAM_VIEWS: DiagramView[] = [
  {
    id: 'sipoc',
    label: 'SIPOC',
    hint: 'Suppliers, inputs, process, outputs, customers',
    use:
      'Scope on one page — who hands work in, what the process turns it into, who receives it, ' +
      'and how it is measured. Read this first to check nothing is missing.',
  },
  {
    id: 'flow',
    label: 'Process flow',
    hint: 'Steps, decisions, and loop-backs end to end',
    use:
      'The order of work — what happens after what, where a decision splits the path, and where ' +
      'a rejection sends work back for rework.',
  },
  {
    id: 'swimlane',
    label: 'Swimlane',
    hint: 'The same steps, split by who performs them',
    use:
      'Who owns what — the same steps placed in each team or system lane, so every handoff ' +
      'between them, and every wait it causes, is visible.',
  },
];

/**
 * SDD.md §3.2's three architectural views, in the order the document presents them.
 *
 * The second family this viewer shows. Ids are `ArchitectureKind` and match `ARCH_VIEWS` in
 * `backend/app/services/diagrams/architecture.py`. Exported for the same reason as
 * `DIAGRAM_VIEWS`: the wizard's file browser and the admin console both show these figures, and
 * a view that is "Development View — packages and imports" in one place and something else in
 * the other is worse than an unlabelled picture.
 *
 * `hint` and `use` carry more weight here than in the blueprint, because in the wizard's file
 * browser the file tree does the choosing and there is no tab strip: the sentence inside the
 * viewer is the only thing that says what the picture is for.
 */
export const ARCHITECTURE_VIEWS: DiagramView[] = [
  {
    id: 'logical',
    label: 'Logical View',
    hint: 'Layers, the components in each, and the calls between them',
    use:
      'What the system is made of — every component with the technology it is built on and ' +
      'the job it does, and the labelled call path from the browser down to storage.',
  },
  {
    id: 'development',
    label: 'Development View',
    hint: 'Packages, their key files, and the import chain',
    use:
      'How the code is organised — the packages a developer opens, the real filenames in ' +
      'each, and which package imports which.',
  },
  {
    id: 'deployment',
    label: 'Deployment View',
    hint: 'Nodes, the processes on them, and every hop',
    use:
      'What runs where — each process on its node, with the protocol, port and payload on ' +
      'every connection. The view an on-call engineer reads first.',
  },
];

/**
 * Supersample factor the architecture PNGs are drawn at — `RENDER_SCALE` in
 * `backend/app/services/diagrams/render.py`, and the `-s3` in the cached filename.
 *
 * The blueprint endpoint reports each render's `scale`; the architecture endpoint returns bytes
 * only, so it has to be told. See the viewer's `scale` input for what goes wrong without it.
 */
export const ARCHITECTURE_RENDER_SCALE = 3;

/**
 * The diagram viewer: zoom, fit, 100%, drag-to-pan, keyboard shortcuts and full screen.
 *
 * One component, four call sites in the wizard (Blueprint step, Review's **Diagrams** tab,
 * either file browser with an exported PNG selected) and one in the admin console. It used to
 * be an `<ng-template>` inside the wizard, which kept those four in step but left the admin
 * panel — the one reader who cannot open the wizard, because the project is not theirs — with
 * no way to look at a diagram at all. Extracting it means the admin reads the blueprint with
 * exactly the controls the user had, rather than a second implementation that drifts.
 *
 * It shows two families of picture, not one: the blueprint's three views, and SDD §3.2's three
 * architecture views when one of the `docs/architecture/` rows is the selected file. Nothing
 * here is specific to either — a caller passes its own `views`, its own `urls` keyed by the
 * same ids, and reads the id back out of `activeChange`. That is why the architecture folder
 * has fit / 100% / drag-to-pan / full screen at all: it is the same viewer, not a second one.
 *
 * What stays outside: fetching the PNGs (they need a Bearer header, so each caller fetches
 * blobs and passes object URLs in) and anything version- or ownership-specific. Everything
 * about *looking at* an image is in here.
 *
 * The PNGs are rendered at three times their layout size (see render.py's RENDER_SCALE), so
 * the pixel width the API reports is not the width to display at: `logicalWidth` below is the
 * size the diagram was laid out to be read at, and zoom is a multiple of that. 100% therefore
 * shows every label at its intended size with a 3× source behind it, which is what keeps it
 * sharp when zoomed further in.
 */
@Component({
  selector: 'ac-diagram-viewer',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './diagram-viewer.component.html',
  styleUrls: ['./diagram-viewer.component.css'],
})
export class DiagramViewerComponent implements OnChanges, OnDestroy {
  /** The views to offer. Defaults to the three the blueprint always has. */
  @Input() views: DiagramView[] = DIAGRAM_VIEWS;
  /** The view on screen. Owned by the parent: switching it can mean more than a tab change. */
  @Input() active: string = 'sipoc';
  @Output() activeChange = new EventEmitter<string>();
  /** Show the three-way switcher above the image. Off where the parent provides its own. */
  @Input() showTabs = false;
  /**
   * Object URLs per view. Blobs rather than URLs because auth is a Bearer header applied by
   * the HTTP interceptor: a browser-issued image request would arrive unauthenticated and 401.
   * The parent owns them and must revoke them.
   */
  @Input() urls: Partial<Record<string, string>> = {};
  /** Render metadata for the current version — width, height and the supersample `scale`. */
  @Input() images: DiagramImage[] = [];
  /**
   * Supersample factor to assume when `images` carries no entry for the view on screen.
   *
   * The blueprint endpoint reports the size and `scale` of every render; the architecture
   * endpoint returns bytes only. Without this the viewer would treat a 2526 px wide PNG as
   * 2526 px of layout, so "100%" would open at three times reading size and "Fit" would think
   * it had three times the pixels to spend. `0` keeps the metadata-only behaviour.
   */
  @Input() scale = 0;
  /** A fetch is in flight; show the placeholder instead of a stale or missing image. */
  @Input() loading = false;
  /** Process title, for the caption and the image's alt text. */
  @Input() title = '';
  /** Free text after the title in the caption — counts, or which version is on screen. */
  @Input() caption = '';
  /** Version for the downloaded filename, so a saved file says what it is. */
  @Input() version: number | null = null;
  /**
   * Filename stem for the download, when a version number is not what names the file.
   *
   * The architecture views have no version — they are redrawn from the current design — so
   * `deployment-v1.png` would claim a history they do not have.
   */
  @Input() downloadStem = '';
  /**
   * Whether the keyboard shortcuts respond. The parent switches them off while something
   * else owns the keyboard — a redraw in progress, or a second viewer on the same screen.
   *
   * The admin console is the second case: it shows the blueprint and the three architecture
   * views one above the other, and hands this to whichever the pointer is over. Without that,
   * `f` would ask both to go full screen.
   */
  @Input() live = true;

  /**
   * Zoom as a multiple of the diagram's layout size. `0` means "fit the pane width".
   *
   * Reset from `defaultZoom` on every view change — see the note there.
   */
  zoom = 0;
  /**
   * The viewer is filling the window. Same DOM node either way; set from the browser's
   * `fullscreenchange` event so it can never disagree with what is on screen.
   */
  fullscreen = false;
  readonly minZoom = 0.15;
  readonly maxZoom = 6;

  @ViewChild('viewer') private viewerRef?: ElementRef<HTMLElement>;

  /**
   * Measured pixel size of each image, keyed by its URL.
   *
   * `images` describes the current version only, so an archived one redrawn from the change
   * history has no metadata to size the zoom against. The image itself does — same renderer,
   * same `scale` — and measuring every image the same way means there is no second code path
   * to keep correct.
   */
  private natural: Record<string, { width: number; height: number }> = {};

  constructor(private cdr: ChangeDetectorRef) {}

  /**
   * A different view, or a fresh set of images, starts from the default zoom.
   *
   * The three views have very different dimensions, so a carried-over zoom would drop the
   * reader into the middle of a picture they have not seen yet.
   */
  ngOnChanges(changes: SimpleChanges): void {
    if (changes['active'] || changes['urls']) this.resetZoom();
  }

  /**
   * Leave full screen with the component.
   *
   * Dropping the class alone is not enough on the native path: the browser would stay in full
   * screen showing a viewer that is no longer on the page, with no visible way out except Esc.
   */
  ngOnDestroy(): void {
    if (document.fullscreenElement === this.viewerRef?.nativeElement) void document.exitFullscreen();
  }

  get activeView(): DiagramView | undefined {
    return this.views.find((v) => v.id === this.active);
  }

  get activeLabel(): string {
    return this.activeView?.label || 'Diagram';
  }

  get activeUrl(): string | undefined {
    return this.urls[this.active];
  }

  select(kind: string): void {
    if (kind === this.active) return;
    // Emit rather than assign: in the wizard's file browser the picture *is* a selected row,
    // so switching view has to move the tree selection too. The parent decides what that means
    // and sets `active` back through the binding.
    this.activeChange.emit(kind);
  }

  // ── Sizing ───────────────────────────────────────────────────────────────────

  private get activeMeta(): DiagramImage | undefined {
    return this.images?.find((i) => i.kind === this.active);
  }

  /** Supersampling factor. The same for every version — it is a property of the renderer. */
  private get activeScale(): number {
    return this.activeMeta?.scale || this.scale || 1;
  }

  /** Width in CSS pixels at 100% zoom. Falls back to a sane pane width if nothing is known. */
  get logicalWidth(): number {
    const url = this.activeUrl;
    const nat = url ? this.natural[url] : undefined;
    if (nat) return Math.round(nat.width / this.activeScale);
    const meta = this.activeMeta;
    if (!meta?.width) return 1100;
    return Math.round(meta.width / (meta.scale || 1));
  }

  get logicalHeight(): number {
    const url = this.activeUrl;
    const nat = url ? this.natural[url] : undefined;
    if (nat) return Math.round(nat.height / this.activeScale);
    const meta = this.activeMeta;
    if (!meta?.height) return 0;
    return Math.round(meta.height / (meta.scale || 1));
  }

  /** Measure an image once it has decoded — the only place an archived render's size exists. */
  onLoad(event: Event): void {
    const img = event.target as HTMLImageElement | null;
    const url = this.activeUrl;
    if (!img?.naturalWidth || !url || this.natural[url]) return;
    this.natural[url] = { width: img.naturalWidth, height: img.naturalHeight };
    this.cdr.detectChanges();
  }

  /** Width to render the `<img>` at, or `null` while fitting (CSS handles that case). */
  get displayWidth(): number | null {
    return this.zoom ? Math.round(this.logicalWidth * this.zoom) : null;
  }

  /**
   * Never let "fit" upscale past the supersample factor.
   *
   * A narrow diagram stretched to a wide pane is a good thing — bigger text — but only up to
   * the point where the source runs out of pixels and it starts to look soft. The PNG is drawn
   * at `scale`× its layout size, so that is exactly where the real pixels run out.
   */
  get fitCap(): number {
    return this.logicalWidth * this.activeScale;
  }

  // ── Zoom ─────────────────────────────────────────────────────────────────────

  get zoomLabel(): string {
    return this.zoom ? `${Math.round(this.zoom * 100)}%` : 'Fit';
  }

  /**
   * Where a view opens: fitted in the page, 100% in full screen.
   *
   * All three start fitted, whatever their shape — the first thing anyone needs from a diagram
   * they have not seen is its shape and size, and that is what tells them where to zoom. Full
   * screen is the explicit "now let me read it" gesture, so it goes to 100%: the size the
   * labels were laid out for, with the pane scrolling and panning to reach the rest.
   */
  private defaultZoom(): number {
    return this.fullscreen ? 1 : 0;
  }

  private resetZoom(): void {
    this.zoom = this.defaultZoom();
  }

  /**
   * Zoom currently on screen.
   *
   * While fitting, that is whatever the pane made it — measured from the rendered image so
   * zooming in continues from what the reader is looking at instead of jumping to 100%.
   */
  private currentZoom(): number {
    if (this.zoom) return this.zoom;
    const img = this.viewerRef?.nativeElement.querySelector<HTMLImageElement>('.bp-figure img');
    const logical = this.logicalWidth;
    if (!img || !img.clientWidth || !logical) return 1;
    return img.clientWidth / logical;
  }

  private setZoom(value: number): void {
    this.zoom = Math.min(this.maxZoom, Math.max(this.minZoom, value));
    this.cdr.detectChanges();
  }

  zoomIn(): void {
    this.setZoom(this.currentZoom() * 1.25);
  }

  zoomOut(): void {
    this.setZoom(this.currentZoom() / 1.25);
  }

  /** Whole diagram, pane width. */
  zoomFit(): void {
    this.zoom = 0;
    this.cdr.detectChanges();
  }

  /** 100% — the size the labels were laid out to be read at. */
  zoomActual(): void {
    this.setZoom(1);
  }

  /** Double-click swaps between the two useful extremes. */
  toggleZoom(): void {
    if (this.zoom) this.zoomFit();
    else this.zoomActual();
  }

  // ── Full screen ──────────────────────────────────────────────────────────────

  /**
   * Full screen through the browser's own Fullscreen API, not a CSS overlay.
   *
   * A CSS overlay cannot be trusted here: `position: fixed` resolves against the nearest
   * transformed ancestor, and the cards this viewer sits in carry `animation: rise … both`,
   * which leaves a transform on them permanently. The "full screen" viewer was therefore
   * pinned inside its card and clipped on the right. The Fullscreen API puts the element in
   * the browser's top layer, where no ancestor can contain it.
   *
   * The class is still applied — by `onFullscreenChange`, so it tracks reality rather than
   * intent — because it does the layout, and it is also the fallback if the request is
   * refused (an embedded frame without `allow="fullscreen"` will reject it).
   */
  toggleFullscreen(): void {
    const host = this.viewerRef?.nativeElement;
    if (this.fullscreen) {
      if (document.fullscreenElement) void document.exitFullscreen();
      else this.setFullscreen(false);
      return;
    }
    if (!host?.requestFullscreen) {
      this.setFullscreen(true);
      return;
    }
    host.requestFullscreen().catch(() => this.setFullscreen(true));
  }

  /** Track the browser's state: Esc, F11 and the window chrome all exit without asking us. */
  @HostListener('document:fullscreenchange')
  onFullscreenChange(): void {
    const el = document.fullscreenElement;
    // Some other element going full screen is not this viewer's business.
    if (el && el !== this.viewerRef?.nativeElement) return;
    this.setFullscreen(!!el);
  }

  private setFullscreen(on: boolean): void {
    if (this.fullscreen === on) return;
    this.fullscreen = on;
    // Entering full screen means "let me read this", so go to 100%; leaving it goes back to the
    // whole picture. Set here rather than in `toggleFullscreen` so Esc, F11 and the window
    // chrome — which exit without telling us — land on the same zoom as the button does.
    this.resetZoom();
    this.cdr.detectChanges();
  }

  @HostListener('document:keydown.escape')
  onEscape(): void {
    // Native fullscreen swallows Esc itself; this is only for the CSS fallback.
    if (!this.fullscreen || document.fullscreenElement) return;
    this.setFullscreen(false);
  }

  // ── Input ────────────────────────────────────────────────────────────────────

  /**
   * Viewer shortcuts.
   *
   * Bare keys, so anything that takes text has to be excluded — a follow-up textarea, a step
   * filter and a file editor all share these screens, and "f" would otherwise go full screen
   * instead of being typed. Modifiers are left alone too: Ctrl+0 and Ctrl+- belong to the
   * browser. Where two viewers share a screen — the admin console — `live` decides which one
   * these reach.
   */
  @HostListener('document:keydown', ['$event'])
  onKey(event: KeyboardEvent): void {
    if (!this.live) return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const el = event.target as HTMLElement | null;
    const tag = (el?.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || el?.isContentEditable) return;
    const shift = (by: number) => {
      const i = this.views.findIndex((v) => v.id === this.active);
      const next = this.views[(i + by + this.views.length) % this.views.length];
      if (next) this.select(next.id);
    };
    const actions: Record<string, () => void> = {
      '+': () => this.zoomIn(),
      '=': () => this.zoomIn(),
      '-': () => this.zoomOut(),
      _: () => this.zoomOut(),
      '0': () => this.zoomFit(),
      '1': () => this.zoomActual(),
      f: () => this.toggleFullscreen(),
      F: () => this.toggleFullscreen(),
      ArrowLeft: () => shift(-1),
      ArrowRight: () => shift(1),
    };
    const run = actions[event.key];
    if (!run) return;
    event.preventDefault();
    run();
  }

  /** Ctrl/⌘ + wheel zooms; a plain wheel keeps scrolling the pane, as everywhere else. */
  onWheel(event: WheelEvent): void {
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    if (event.deltaY < 0) this.zoomIn();
    else this.zoomOut();
  }

  // Drag to pan. The scroll offsets are read off the element rather than kept in state, so a
  // mouseup outside the pane simply ends the gesture.
  private panFrom: { x: number; y: number; left: number; top: number } | null = null;

  startPan(event: MouseEvent): void {
    const pane = event.currentTarget as HTMLElement;
    this.panFrom = { x: event.clientX, y: event.clientY, left: pane.scrollLeft, top: pane.scrollTop };
  }

  movePan(event: MouseEvent): void {
    if (!this.panFrom) return;
    event.preventDefault();
    const pane = event.currentTarget as HTMLElement;
    pane.scrollLeft = this.panFrom.left - (event.clientX - this.panFrom.x);
    pane.scrollTop = this.panFrom.top - (event.clientY - this.panFrom.y);
  }

  endPan(): void {
    this.panFrom = null;
  }

  get panning(): boolean {
    return !!this.panFrom;
  }

  // ── Getting the file out ─────────────────────────────────────────────────────

  /** Hand the PNG to the browser's own image viewer — its zoom works on the raw 3× file. */
  openInTab(): void {
    const url = this.activeUrl;
    if (url) window.open(url, '_blank');
  }

  /** Save the visible diagram. Same blob the `<img>` is showing — no second fetch. */
  download(): void {
    const url = this.activeUrl;
    if (!url) return;
    const a = document.createElement('a');
    a.href = url;
    a.download = `${this.downloadStem || `${this.active}-v${this.version ?? 1}`}.png`;
    a.click();
  }
}
