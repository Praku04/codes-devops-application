"""Ad-hoc: draw the three architectural views from a realistic payload, for eyeballing.

Not a test — no pytest in this project. Writes into `backend/.scratch/` so the output can be
opened, compared against the previous revision, and thrown away.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.services.diagrams.architecture import (  # noqa: E402
    ARCH_REVISION,
    render_architecture,
    views_digest,
)

#: Deliberately long — a real generated description runs several sentences, and the whole point
#: of `_prose` is that such a paragraph is shortened on a word boundary with a visible ellipsis
#: instead of being sliced mid-word. Keep these long so the eyeball pass sees the worst case.
LOGICAL_DESCRIPTION = (
    "Three layers, and every arrow is a call the design permits. The Presentation layer never "
    "reaches the Data layer directly: it speaks only to Application Services over HTTP, which "
    "keeps the browser bundle free of persistence concerns and lets the API evolve its storage "
    "without a front-end release. Application Services own every write, so the audit trail has "
    "exactly one place to hook into, and the Audit Writer is a cross-cutting concern invoked by "
    "Application Services and never by the Presentation layer."
)

DEVELOPMENT_DESCRIPTION = (
    "Packages grouped by the directory that owns them, ordered by import depth so an arrow "
    "always points at something that compiles on its own. Nothing in app/models imports from "
    "app/services or app/api, which is what lets the contracts be shared with the CLI without "
    "dragging the web stack along; the front-end tree mirrors the same rule, with shared "
    "components depending only on the typed HTTP client."
)

DEPLOYMENT_DESCRIPTION = (
    "One VPC, two private subnets and an internal ALB in front of the service. No task holds "
    "state that survives a deploy: uploaded documents live in S3, sessions in RDS, and the "
    "render cache is a disposable /tmp keyed by content digest, so a cold task is correct on "
    "its first request and merely slower. Outbound calls to Bedrock leave through a NAT gateway "
    "and are signed with the task role, so no long-lived key is ever mounted."
)

VIEWS = {
    "logical": {
        "description": LOGICAL_DESCRIPTION,
        "layers": [
            {
                "name": "Presentation",
                "responsibility": "Renders the wizard and streams generation progress to the browser",
                "components": [
                    {
                        "name": "Wizard",
                        "tech": "Angular 22 standalone",
                        "detail": "Six-step guided flow from problem statement to download",
                        "interface": "/#/wizard · SSE /projects/{id}/events",
                    },
                    {
                        "name": "Admin console",
                        "tech": "Angular 22 standalone",
                        "detail": "Read-only view of every user's sessions and diagrams",
                        "interface": "/#/admin · GET /admin/users",
                    },
                ],
            },
            {
                "name": "Application",
                "responsibility": "Deduces the solution design and renders the IDE-ready workspace",
                "components": [
                    {
                        "name": "Deduction engine",
                        "tech": "Python + LiteLLM → Bedrock",
                        "detail": "Turns a problem statement into agents, skills and rules",
                        "interface": "POST /projects/{id}/plan",
                    },
                    {
                        "name": "Diagram renderer",
                        "tech": "matplotlib Agg",
                        "detail": "Draws the blueprint and the three architectural views",
                        "interface": "GET /projects/{id}/plan/architecture/{kind}.png",
                    },
                    {
                        "name": "Exporter",
                        "tech": "Python zipfile",
                        "detail": "Writes SDD.md, README.md and both diagram folders",
                        "interface": "GET /projects/{id}/export.zip",
                    },
                ],
            },
            {
                "name": "Data",
                "responsibility": "Persists sessions and caches rendered PNGs by content digest",
                "components": [
                    {
                        "name": "Sessions",
                        "tech": "PostgreSQL 16",
                        "detail": "One row per project, plan and diagram payloads as JSONB",
                        "interface": "table projects(plan_json, diagram_json)",
                    },
                    {
                        "name": "Render cache",
                        "tech": "Local disk",
                        "detail": "Keyed by views_digest, so a redraw is free",
                        "interface": "/tmp/agentcraft/renders/{digest}.png",
                    },
                ],
            },
        ],
        "flows": [
            {"from": "Wizard", "to": "Deduction engine", "carries": "problem statement"},
            {"from": "Deduction engine", "to": "Sessions", "carries": "plan_json"},
            {"from": "Diagram renderer", "to": "Render cache", "carries": "PNG bytes"},
            {"from": "Admin console", "to": "Sessions", "carries": "read-only session list"},
        ],
    },
    "development": {
        "description": DEVELOPMENT_DESCRIPTION,
        "groups": [
            {
                "path": "app/api",
                "top": "app",
                "files": 9,
                "key_files": ["v1/projects.py", "v1/admin.py", "v1/auth.py", "deps.py", "router.py"],
                "depends_on": ["app/services", "app/models"],
                "responsibility": "HTTP surface: validates requests, resolves the caller, delegates to a service",
            },
            {
                "path": "app/services",
                "top": "app",
                "files": 21,
                "key_files": [
                    "projects.py",
                    "deduction/engine.py",
                    "diagrams/architecture.py",
                    "export/renderers.py",
                ],
                "depends_on": ["app/models"],
                "responsibility": "All business logic: deduction, diagram rendering, export and admin reads",
            },
            {
                "path": "app/models",
                "top": "app",
                "files": 4,
                "key_files": ["schemas.py", "db.py"],
                "depends_on": [],
                "responsibility": "Pydantic request/response contracts and the SQLAlchemy tables",
            },
            {
                "path": "src/app/wizard",
                "top": "src",
                "files": 7,
                "key_files": ["wizard.component.ts", "wizard.component.html", "wizard.component.css"],
                "depends_on": ["src/app/shared", "src/app/services"],
                "responsibility": "The six-step generation flow and the generated-file browser",
            },
            {
                "path": "src/app/shared",
                "top": "src",
                "files": 5,
                "key_files": ["diagram-viewer/diagram-viewer.component.ts"],
                "depends_on": ["src/app/services"],
                "responsibility": "The diagram viewer both the wizard and the admin console mount",
            },
            {
                "path": "src/app/services",
                "top": "src",
                "files": 3,
                "key_files": ["api.service.ts", "auth.service.ts"],
                "depends_on": [],
                "responsibility": "Typed HTTP client, Bearer token handling and blob fetches",
            },
        ],
    },
    "deployment": {
        "description": DEPLOYMENT_DESCRIPTION,
        "nodes": [
            {
                "name": "ECS Fargate service",
                "runtime": "Fargate 1.4 · 2 vCPU / 4 GB",
                "scaling": "2–8 tasks on CPU > 70%",
                "hosts": [
                    {
                        "name": "API container",
                        "tech": "uvicorn :8000",
                        "detail": "Serves the API and the built Angular bundle",
                        "interface": "stateless — no local disk",
                    },
                    {
                        "name": "Render worker",
                        "tech": "matplotlib Agg",
                        "detail": "Draws the blueprint and architecture PNGs in-process",
                        "interface": "writes the /tmp cache only",
                    },
                ],
            },
            {
                "name": "RDS PostgreSQL",
                "runtime": "db.t4g.medium",
                "scaling": "Multi-AZ standby",
                "hosts": [
                    {
                        "name": "agentcraft",
                        "tech": "PostgreSQL 16",
                        "detail": "Sessions, users, plans and diagram payloads",
                        "interface": "stateful — 7-day PITR",
                    }
                ],
            },
            {
                "name": "Amazon Bedrock",
                "runtime": "Managed · us-east-1",
                "scaling": "On-demand throughput",
                "hosts": [
                    {
                        "name": "Claude Sonnet 4.6",
                        "tech": "InvokeModel",
                        "detail": "Deduction and solution-design generation",
                        "interface": "stateless — no prompt retention",
                    }
                ],
            },
        ],
        "connections": [
            {
                "from": "API container",
                "to": "agentcraft",
                "protocol": "PostgreSQL",
                "port": "5432",
                "data": "session rows",
            },
            {
                "from": "API container",
                "to": "Claude Sonnet 4.6",
                "protocol": "HTTPS (SigV4)",
                "port": "443",
                "data": "problem statement",
            },
        ],
    },
}


def main() -> None:
    out = pathlib.Path(__file__).resolve().parents[1] / ".scratch"
    out.mkdir(parents=True, exist_ok=True)
    print(f"ARCH_REVISION={ARCH_REVISION} digest={views_digest(VIEWS)}")
    drawn = render_architecture(VIEWS, project_name="AgentCraft Studio")
    for kind, (data, w, h, scale) in drawn.items():
        target = out / f"{kind}-view.png"
        target.write_bytes(data)
        print(f"{kind:12} {len(data):>8} bytes  {w}x{h} @{scale}  -> {target}")
    missing = [k for k in ("logical", "development", "deployment") if k not in drawn]
    print("MISSING:", missing or "none")


if __name__ == "__main__":
    main()
