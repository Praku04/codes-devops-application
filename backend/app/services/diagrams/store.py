"""
On-disk cache for rendered diagram PNGs.

The PNGs are a pure function of the stored `DiagramSet`, so this is a cache and not a source
of truth: a missing or deleted file is re-rendered on demand. That is deliberate — it means a
deployment can wipe `diagrams/` without breaking any project, and a code change to the
renderer takes effect for old projects as soon as their version folder is pruned.

Files live at `diagrams/<project_id>/<kind>-v<version>-r<render revision>.png`, alongside the
existing `uploads/` and `exports/` folders at the process working directory. The renderer's
revision is in the name on purpose: improving the drawing then invalidates every cached PNG
automatically, instead of leaving existing projects showing the old render forever.
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path
from typing import Iterable

from app.models.diagrams import DIAGRAM_KINDS, DiagramImage, DiagramSet
from app.services.diagrams.architecture import (
    ARCH_KINDS,
    render_architecture,
    views_digest,
)
from app.services.diagrams.render import RENDER_REVISION, RENDER_SCALE, render_all

logger = logging.getLogger("agentcraft.diagrams")

DIAGRAM_ROOT = Path("diagrams")

# The SDD's architectural views live in a subfolder, not beside the process blueprint. Two
# reasons, both load-bearing: the blueprint and the architecture are separate artefacts with
# separate lifecycles, and `prune()` below deletes any `*.png` in the project folder that does
# not carry a live `-v…-r…-s` marker — an architecture PNG sitting there would be deleted the
# next time a blueprint re-rendered.
ARCH_SUBDIR = "architecture"


def _safe_id(project_id: str) -> str:
    """Project ids are uuids, but this path is built from a URL segment — keep it inert."""
    return "".join(ch for ch in str(project_id) if ch.isalnum() or ch in "-_") or "unknown"


def diagram_dir(project_id: str) -> Path:
    return DIAGRAM_ROOT / _safe_id(project_id)


def png_path(project_id: str, kind: str, version: int, scale: int = RENDER_SCALE) -> Path:
    """`<kind>-v<version>-r<renderer revision>-s<scale>.png`.

    The pixel scale is in the name because the viewer needs it to know what 100% means, and
    reading it back off the filename beats storing it in a sidecar or re-deriving it from the
    model. Reads go through `find_png`, which does not need to know the scale up front.
    """
    return diagram_dir(project_id) / f"{kind}-v{int(version)}-r{RENDER_REVISION}-s{int(scale)}.png"


def find_png(project_id: str, kind: str, version: int) -> Path | None:
    """The cached file for this kind and version, whatever scale it was drawn at."""
    pattern = f"{kind}-v{int(version)}-r{RENDER_REVISION}-s*.png"
    return next(iter(sorted(diagram_dir(project_id).glob(pattern))), None)


def _scale_of(path: Path) -> int:
    """Read the trailing `-s<n>` back off a cached filename."""
    try:
        return max(1, int(path.stem.rsplit("-s", 1)[1]))
    except (IndexError, ValueError):
        return 1


def _png_size(data: bytes) -> tuple[int, int]:
    """Width/height straight from the IHDR chunk — cheaper than decoding the image."""
    try:
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    except Exception:  # noqa: BLE001
        return 0, 0


def _write(
    project_id: str, version: int, rendered: dict[str, tuple[bytes, int, int, int]]
) -> list[DiagramImage]:
    diagram_dir(project_id).mkdir(parents=True, exist_ok=True)
    images: list[DiagramImage] = []
    for kind in DIAGRAM_KINDS:
        data, width, height, scale = rendered[kind]
        png_path(project_id, kind, version, scale).write_bytes(data)
        images.append(
            DiagramImage(kind=kind, width=width, height=height, bytes=len(data), scale=scale)
        )
    return images


def render_set(
    project_id: str,
    dset: DiagramSet,
    *,
    redraw: Iterable[str] | None = None,
    reuse_from: int | None = None,
) -> list[DiagramImage]:
    """Render the three views for this version and write them to the cache.

    `redraw` plus `reuse_from` is the single-view follow-up: only the changed view is drawn and
    the other two are copied byte-for-byte from the version they were drawn at. Safe because a
    PNG is a pure function of (renderer revision, model, view) — the caller only passes a kind
    as unchanged when both the model and that view compare equal — and because a missing source
    file falls back to drawing it. Without this, changing the SIPOC's wording would redraw a
    25-step swimlane to produce the identical image.
    """
    to_draw = set(DIAGRAM_KINDS) if redraw is None else {k for k in redraw if k in DIAGRAM_KINDS}
    copied: dict[str, tuple[bytes, int, int, int]] = {}
    if reuse_from is not None and reuse_from != dset.version:
        for kind in DIAGRAM_KINDS:
            if kind in to_draw:
                continue
            source = find_png(project_id, kind, reuse_from)
            if source is None:
                to_draw.add(kind)  # nothing cached to copy — draw it after all
                continue
            try:
                data = source.read_bytes()
            except OSError:
                to_draw.add(kind)
                continue
            width, height = _png_size(data)
            copied[kind] = (data, width, height, _scale_of(source))
    else:
        to_draw = set(DIAGRAM_KINDS)

    rendered = (
        render_all(dset.model, dset.sipoc, dset.flow, dset.swimlane, only=to_draw)
        if to_draw
        else {}
    )
    images = _write(project_id, dset.version, {**copied, **rendered})
    prune(project_id, keep_versions=dset.viewable_versions())
    return images


def render_version(project_id: str, dset: DiagramSet, version: int) -> list[DiagramImage]:
    """Render an archived version from the views kept in `dset.history`.

    Drawn by the *current* renderer, not by whatever the renderer looked like when that
    version was generated — the point of keeping views instead of PNGs. Raises `ValueError`
    when the version has aged out of the history, which is a 404 and not a server error.
    """
    if version == dset.version:
        return render_set(project_id, dset)
    snap = dset.views_for(version)
    if snap is None:
        raise ValueError(f"No stored views for version {version}")
    images = _write(
        project_id, version, render_all(snap.model, snap.sipoc, snap.flow, snap.swimlane)
    )
    prune(project_id, keep_versions=dset.viewable_versions())
    return images


def get_png(project_id: str, dset: DiagramSet, kind: str, version: int | None = None) -> bytes:
    """Cached PNG for one view, rendering it first if the file is not there.

    `version` defaults to the current one; an earlier version still in the history is drawn
    on demand, which is how the change-history entries show what they actually looked like.
    """
    if kind not in DIAGRAM_KINDS:
        raise ValueError(f"Unknown diagram kind: {kind}")
    wanted = dset.version if version is None else int(version)
    path = find_png(project_id, kind, wanted)
    if path:
        try:
            return path.read_bytes()
        except OSError as exc:  # partially written or locked — fall through to a re-render
            logger.warning("Re-rendering %s for %s: %s", kind, project_id, exc)
    render_version(project_id, dset, wanted)
    found = find_png(project_id, kind, wanted)
    if not found:
        raise OSError(f"Render produced no file for {kind} v{wanted}")
    return found.read_bytes()


def images_for(project_id: str, dset: DiagramSet) -> list[DiagramImage]:
    """Metadata for the API response, rendering anything not already cached."""
    found = {k: find_png(project_id, k, dset.version) for k in DIAGRAM_KINDS}
    if not all(found.values()):
        return render_set(project_id, dset)
    images: list[DiagramImage] = []
    for kind in DIAGRAM_KINDS:
        path = found[kind]
        assert path is not None  # guarded above; keeps the type checker honest
        data = path.read_bytes()
        width, height = _png_size(data)
        images.append(
            DiagramImage(
                kind=kind,
                width=width,
                height=height,
                bytes=len(data),
                scale=_scale_of(path),
            )
        )
    return images


# ---------------------------------------------------------------------------
# Architecture views (SDD §3.2)
#
# Same cache-not-truth contract as above, keyed by a digest of the stored views instead of a
# version number: the architectural views are a rendering of the *current* design, so there is
# no numbered revision to preserve. Change the design and the digest changes, the old files are
# pruned, and the next read draws the new ones.
# ---------------------------------------------------------------------------


def arch_dir(project_id: str) -> Path:
    return diagram_dir(project_id) / ARCH_SUBDIR


def arch_png_path(project_id: str, kind: str, digest: str, scale: int = RENDER_SCALE) -> Path:
    return arch_dir(project_id) / f"{kind}-{digest}-s{int(scale)}.png"


def find_arch_png(project_id: str, kind: str, digest: str) -> Path | None:
    return next(iter(sorted(arch_dir(project_id).glob(f"{kind}-{digest}-s*.png"))), None)


def render_arch_set(
    project_id: str,
    views: dict,
    *,
    project_name: str = "",
    only: Iterable[str] | None = None,
) -> dict[str, Path]:
    """Draw the architecture views for `views` and write them to the cache.

    Returns the path of every view that is now on disk, including ones that were already
    cached. A view the renderer could not draw is simply absent — `render_architecture`
    swallows its own failures, because a missing picture must not fail an export.
    """
    digest = views_digest(views)
    wanted = tuple(ARCH_KINDS if only is None else (k for k in ARCH_KINDS if k in set(only)))
    paths: dict[str, Path] = {}
    to_draw: list[str] = []
    for kind in wanted:
        found = find_arch_png(project_id, kind, digest)
        if found:
            paths[kind] = found
        else:
            to_draw.append(kind)
    if to_draw:
        arch_dir(project_id).mkdir(parents=True, exist_ok=True)
        rendered = render_architecture(views, project_name=project_name, only=to_draw)
        for kind, (data, _w, _h, scale) in rendered.items():
            path = arch_png_path(project_id, kind, digest, scale)
            path.write_bytes(data)
            paths[kind] = path
        prune_arch(project_id, keep_digest=digest)
    return paths


def get_arch_png(project_id: str, views: dict, kind: str, *, project_name: str = "") -> bytes:
    """Cached architecture PNG for one view, drawing it first if it is not on disk."""
    if kind not in ARCH_KINDS:
        raise ValueError(f"Unknown architecture view: {kind}")
    digest = views_digest(views)
    path = find_arch_png(project_id, kind, digest)
    if path:
        try:
            return path.read_bytes()
        except OSError as exc:  # partially written or locked — fall through to a re-render
            logger.warning("Re-rendering architecture %s for %s: %s", kind, project_id, exc)
    drawn = render_arch_set(project_id, views, project_name=project_name, only=[kind]).get(kind)
    if not drawn:
        raise OSError(f"Render produced no file for architecture view {kind}")
    return drawn.read_bytes()


def arch_png_bytes(
    project_id: str, views: dict, *, project_name: str = ""
) -> dict[str, bytes]:
    """Every drawable architecture view as bytes — what the exporter attaches to a zip."""
    out: dict[str, bytes] = {}
    for kind, path in render_arch_set(project_id, views, project_name=project_name).items():
        try:
            out[kind] = path.read_bytes()
        except OSError:
            logger.warning("Architecture view %s unreadable for %s", kind, project_id)
    return out


def prune_arch(project_id: str, *, keep_digest: str) -> None:
    """Drop architecture PNGs drawn for a design that is no longer the current one."""
    folder = arch_dir(project_id)
    if not folder.is_dir():
        return
    for path in folder.glob("*.png"):
        if f"-{keep_digest}-s" not in path.name:
            try:
                path.unlink()
            except OSError:
                pass


def prune(project_id: str, *, keep_versions: Iterable[int]) -> None:
    """Drop PNGs no view can ask for — versions off the end of the history, and anything an
    older renderer left behind.

    Every version still in `DiagramSet.history` is kept: the change history can ask for its
    image at any time, and re-rendering a swimlane is far slower than a few hundred KB on disk.
    """
    folder = diagram_dir(project_id)
    if not folder.is_dir():
        return
    keep = [f"-v{int(v)}-r{RENDER_REVISION}-s" for v in keep_versions]
    for path in folder.glob("*.png"):
        if not any(marker in path.name for marker in keep):
            try:
                path.unlink()
            except OSError:
                pass


def clear(project_id: str) -> None:
    """Remove every cached PNG for a project — used when its diagrams are discarded."""
    folder = diagram_dir(project_id)
    if not folder.is_dir():
        return
    for path in folder.glob(f"{ARCH_SUBDIR}/*.png"):
        try:
            path.unlink()
        except OSError:
            pass
    for path in folder.glob("*.png"):
        try:
            path.unlink()
        except OSError:
            pass
    # The architecture subfolder first, or the parent `rmdir` below would always fail.
    for target in (folder / ARCH_SUBDIR, folder):
        try:
            target.rmdir()
        except OSError:
            pass
