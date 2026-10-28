"""Pydantic domain schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core.state_machine import Platform, ProjectPath, ProjectState


class SkillSpec(BaseModel):
    name: str = Field(..., max_length=64)
    description: str = Field(..., max_length=1024)
    instructions: str
    disable_model_invocation: bool | None = None
    references: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def kebab_name(cls, v: str) -> str:
        cleaned = v.strip().lower().replace("_", "-").replace(" ", "-")
        if not cleaned or not all(c.isalnum() or c == "-" for c in cleaned):
            raise ValueError("name must be lowercase kebab-case")
        return cleaned[:64]


class AgentSpec(BaseModel):
    name: str = Field(..., max_length=64)
    description: str
    system_prompt: str
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def kebab_name(cls, v: str) -> str:
        cleaned = v.strip().lower().replace("_", "-").replace(" ", "-")
        return cleaned[:64]


class RuleSpec(BaseModel):
    name: str
    description: str
    body: str
    always_apply: bool = False
    globs: list[str] = Field(default_factory=list)


class McpServerSpec(BaseModel):
    name: str
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class SourceFileSpec(BaseModel):
    """One file in the AI-analyzed modular application scaffold."""

    path: str = Field(..., description="Repo-relative path, e.g. main.py or app/api/v1/router.py")
    purpose: str = Field(..., description="Detailed why this file exists and how agents should use it")
    content: str = Field(default="", description="Scaffold / stub source for the file")

    @field_validator("path")
    @classmethod
    def normalize_path(cls, v: str) -> str:
        cleaned = (v or "").replace("\\", "/").lstrip("/")
        if not cleaned or ".." in cleaned.split("/"):
            raise ValueError("path must be a safe relative path")
        return cleaned


class ProjectPlan(BaseModel):
    project_name: str
    summary: str
    agents: list[AgentSpec] = Field(default_factory=list)
    skills: list[SkillSpec] = Field(default_factory=list)
    rules: list[RuleSpec] = Field(default_factory=list)
    mcp_servers: list[McpServerSpec] = Field(default_factory=list)
    source_tree: list[SourceFileSpec] = Field(
        default_factory=list,
        description="Modular app layout starting at main.py (packages include __init__.py)",
    )
    file_overrides: dict[str, str] = Field(
        default_factory=dict,
        description="User-edited export file bodies keyed by repo-relative path (persist across reopen)",
    )
    work_breakdown: str = Field(
        default="",
        description="Detailed phased WORKBREAKDOWN.md content (editable in UI; exported beside README)",
    )
    work_breakdown_llm: bool = Field(
        default=False,
        description="True when work_breakdown was produced by Bedrock (not fallback stub)",
    )
    work_breakdown_complete: bool = Field(
        default=False,
        description="True when every phase in work_breakdown is fully written (not truncated)",
    )
    solution_design: str = Field(
        default="",
        description="Solution Design Document SDD.md content (editable in UI; exported beside README)",
    )
    solution_design_llm: bool = Field(
        default=False,
        description="True when solution_design was produced by Bedrock (not the deterministic fallback)",
    )
    solution_design_complete: bool = Field(
        default=False,
        description="True when every numbered section of solution_design is present and in order",
    )
    architecture_views: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Normalised logical/development/deployment structure behind SDD §3.2, kept so the "
            "architecture PNGs can be re-drawn without another model call"
        ),
    )


class UploadedDocument(BaseModel):
    """A document the user uploaded, persisted so the UI/CLI can list it later."""

    filename: str
    size_bytes: int = 0
    content_type: str = ""
    chars_extracted: int = 0
    uploaded_at: str | None = None
    # Extracted text is kept in ProjectBrief.document_text (used for generation);
    # this record exists so the file itself is attributable and re-listable.


class ProjectBrief(BaseModel):
    problem_statement: str = ""
    document_text: str = ""
    documents: list[UploadedDocument] = Field(
        default_factory=list,
        description="Uploaded source documents (metadata). Empty for pre-upgrade projects.",
    )
    interview_answers: dict[str, str] = Field(default_factory=dict)
    tech_stack: str = ""
    domains: list[str] = Field(default_factory=list)
    constraints: str = ""
    integrations: list[str] = Field(default_factory=list)

    def as_prompt_context(self) -> str:
        parts = []
        if self.problem_statement:
            parts.append(f"## Problem statement\n{self.problem_statement}")
        if self.document_text:
            # Keep as much document text as practical for generation prompts
            parts.append(f"## Documents\n{self.document_text[:200000]}")
        if self.interview_answers:
            qa = "\n".join(f"- {k}: {v}" for k, v in self.interview_answers.items())
            parts.append(f"## Interview answers\n{qa}")
        if self.tech_stack:
            parts.append(f"## Tech stack\n{self.tech_stack}")
        if self.domains:
            parts.append(f"## Domains\n{', '.join(self.domains)}")
        if self.constraints:
            parts.append(f"## Constraints\n{self.constraints}")
        if self.integrations:
            parts.append(f"## Integrations\n{', '.join(self.integrations)}")
        return "\n\n".join(parts) if parts else "No context provided."


class InterviewQuestion(BaseModel):
    id: str
    prompt: str
    required: bool = True
    answer: str | None = None


class ProjectStatus(BaseModel):
    id: str
    name: str
    state: ProjectState
    path: ProjectPath | None = None
    platform: Platform | None = None
    blockers: list[str] = Field(default_factory=list)
    brief: ProjectBrief | None = None
    plan: ProjectPlan | None = None
    interview: list[InterviewQuestion] = Field(default_factory=list)
    error: str | None = None
    progress: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    user_id: str | None = None
    # Blueprint flags rather than the diagrams themselves: the UI polls this object during
    # generation, and the process model is far too large to ship on every poll. The full set
    # comes from GET /projects/{id}/diagrams.
    has_diagrams: bool = False
    diagrams_frozen: bool = False
    diagram_version: int = 0


class UserOut(BaseModel):
    id: str
    email: str
    name: str
    role: str = "user"  # user | admin
    status: str = "pending"  # pending | approved | rejected


class SignupRequest(BaseModel):
    email: str
    password: str = Field(..., min_length=6)
    name: str = "AgentCraft User"


class LoginRequest(BaseModel):
    email: str
    password: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class TokenResponse(BaseModel):
    """The OAuth2 password-flow response — exactly the two fields the spec requires.

    Deliberately *not* AuthResponse: Swagger UI reads `access_token` out of the token
    endpoint's body itself, and anything extra (our `user` object) is noise it ignores.
    The UI and CLI keep using `/auth/login`, which does return the user.
    """

    access_token: str
    token_type: str = "bearer"


class SignupPendingResponse(BaseModel):
    message: str
    status: str = "pending"
    email: str


class AdminSessionInfo(BaseModel):
    id: str
    name: str
    state: str
    platform: str | None = None
    updated_at: str | None = None
    agent_count: int = 0
    skill_count: int = 0
    rule_count: int = 0
    agent_names: list[str] = Field(default_factory=list)
    skill_names: list[str] = Field(default_factory=list)
    rule_names: list[str] = Field(default_factory=list)
    document_names: list[str] = Field(
        default_factory=list,
        description="Uploaded source documents for this session — same list the owner sees",
    )
    # ── Which context pipeline the session took ──────────────────────────────────
    #
    # The two paths produce very different briefs — uploaded documents versus answers
    # typed into the guided interview — so "did this follow the document pipeline?" is
    # the first thing an admin looking at someone else's session needs to know, and it
    # is not deducible from the state name.
    path: str | None = Field(
        default=None,
        description="docs | interview — the context pipeline this session followed. "
        "None when the choice was never made and nothing was handed in.",
    )
    path_inferred: bool = Field(
        default=False,
        description="True when the pipeline was deduced from what the session holds rather "
        "than read from the row. Sessions created before the column existed store no path, "
        "and reporting them as 'not chosen' would be wrong — they did take one.",
    )
    interview_answer_count: int = Field(
        default=0,
        description="Answered interview questions. The interview path's counterpart to "
        "document_names, and what distinguishes a started interview from an abandoned one.",
    )
    # ── Blueprint ────────────────────────────────────────────────────────────────
    #
    # Metadata only. The PNGs are several megabytes at 3× supersampling, so the diagrams
    # themselves come from /admin/projects/{id}/diagrams and its PNG endpoint, fetched
    # when the admin actually opens a session rather than with every user card.
    has_diagrams: bool = False
    diagrams_frozen: bool = Field(
        default=False,
        description="The user approved the blueprint — generation reads it as authoritative",
    )
    diagram_version: int = Field(
        default=0, description="Current blueprint version; 0 when none was drawn"
    )
    diagram_title: str | None = Field(
        default=None, description="Process title from the extracted model"
    )
    diagram_step_count: int = 0
    diagram_actor_count: int = 0
    diagram_revision_count: int = Field(
        default=0,
        description="Entries in the change history, including the first draft. One means the "
        "user accepted the first draw; six means they worked on it.",
    )
    diagram_last_scope: str | None = Field(
        default=None,
        description="Scope of the most recent follow-up — all | sipoc | flow | swimlane. None "
        "when the blueprint has never been revised. Lets the collapsed session row say whether "
        "the last change moved the process or only re-drew one view.",
    )
    # ── Solution design ─────────────────────────────────────────────────────────
    has_plan: bool = Field(
        default=False,
        description="The session has a generated plan, so SDD.md and its three architectural "
        "views exist. Metadata for the same reason as has_diagrams: the panel decides whether "
        "to ask for /admin/projects/{id}/architecture/{kind}.png rather than requesting three "
        "matplotlib renders speculatively for every session in the list.",
    )


class AdminUserView(BaseModel):
    """Read-only admin view of a user and their sessions."""

    id: str
    email: str
    name: str
    role: str
    # pending | approved | rejected | deleted — "deleted" rows come from the archive,
    # not the users table, so the admin can see and restore them.
    status: str
    created_at: str | None = None
    session_count: int = 0
    sessions: list[AdminSessionInfo] = Field(default_factory=list)
    deleted_at: str | None = None
    deleted_by: str | None = Field(
        default=None, description="Email of the admin who deleted this account"
    )
    previous_status: str | None = Field(
        default=None, description="Status the account held when it was deleted"
    )
    restorable: bool = Field(
        default=False,
        description="False for archives written before restore was supported",
    )


class DeleteUserResponse(BaseModel):
    """Result of a super-admin delete — the account is archived, not yet erased."""

    id: str
    email: str
    name: str
    deleted_sessions: int = 0
    message: str
    restorable: bool = True


class RestoreUserResponse(BaseModel):
    """Result of a super-admin restore — the account and its workspaces are back."""

    user: AdminUserView
    restored_sessions: int = 0
    message: str


class PurgeUserResponse(BaseModel):
    """Result of a permanent erase — archive and workspaces are gone for good."""

    id: str
    email: str
    purged_sessions: int = 0
    message: str


class SessionSummary(BaseModel):
    """Lightweight session card for the landing page / CLI."""

    id: str
    name: str
    state: ProjectState
    platform: Platform | None = None
    summary: str | None = None
    agent_count: int = 0
    skill_count: int = 0
    rule_count: int = 0
    agent_names: list[str] = Field(default_factory=list)
    skill_names: list[str] = Field(default_factory=list)
    rule_names: list[str] = Field(default_factory=list)
    document_names: list[str] = Field(
        default_factory=list,
        description="Uploaded source documents for this session (docs path only)",
    )
    created_at: str | None = None
    updated_at: str | None = None


class ExpandBriefRequest(BaseModel):
    seed: str


class ExpandBriefResponse(BaseModel):
    expanded: str
    was_short: bool = True


class CreateProjectRequest(BaseModel):
    name: str = "Untitled Project"


class SetPathRequest(BaseModel):
    path: ProjectPath


class DocumentsRequest(BaseModel):
    problem_statement: str = ""


class InterviewAnswerRequest(BaseModel):
    answers: dict[str, str]
    #: Optional project title supplied with the answer (interview path).
    name: str | None = None


class SetPlatformRequest(BaseModel):
    platform: Platform


class ExportRequest(BaseModel):
    mode: str = "zip"  # zip | directory
    output_path: str | None = None


class LlmSettingsView(BaseModel):
    model: str
    temperature: float
    max_tokens: int
    region: str


class LlmSettingsUpdate(BaseModel):
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


class FileTreeNode(BaseModel):
    path: str
    content_preview: str | None = None


class ExportResult(BaseModel):
    mode: str
    platform: Platform
    files: list[str]
    download_path: str | None = None
    output_path: str | None = None
