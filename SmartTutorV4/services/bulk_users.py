"""
Admin account creation: one shared create path, and bulk upload from an Excel template.

Single (Create User form) and bulk accounts are created by create_admin_account(), so the two
cannot drift apart: same duplicate check, same activity log, same welcome notification, and
both start on the St@<EmployeeID>#2026 login with must_change_password set.

The template is generated on each download, so its Process dropdown always lists the processes
that exist now. Uploads are parsed with openpyxl, which uses defusedxml automatically (pinned in
requirements.txt), so a crafted workbook cannot expand external XML entities.
"""

import io
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from core.runtime import SessionLocal
from models.domain import Notification, Process, User
from services.auth_platform import get_password_hash, get_user_from_db, log_activity
from services.jobs import update_job_status
from utils.initial_login import (
    EMPLOYEE_LOGIN_PREFIX,
    EMPLOYEE_LOGIN_SUFFIX,
    MAX_BULK_USERS,
    initial_login_for,
    safe_employee_id,
)
from utils.input_validation import normalize_email, safe_registration_email
from utils.security_helpers import log_safe

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
# A sheet can carry formatting far below its last real row; stop reading well before that.
_MAX_SCANNED_ROWS = 5000
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TEMPLATE_FILENAME = "smart_tutor_bulk_users_template.xlsx"

USERS_SHEET = "Users"
LISTS_SHEET = "Lists"
COLUMNS = ("Email", "Employee ID", "Role", "Process")

# Dropdown label -> stored role. The same labels the Create User form shows.
ROLE_LABELS: Dict[str, str] = {"Trainee": "trainee", "Trainer": "trainer", "Super Admin": "super_admin"}
_ROLE_LOOKUP: Dict[str, str] = {
    **{label.lower(): role for label, role in ROLE_LABELS.items()},
    **{role: role for role in ROLE_LABELS.values()},
    "admin": "super_admin",
}
ROLE_DISPLAY: Dict[str, str] = {role: label for label, role in ROLE_LABELS.items()}

_HEADER_FONT = Font(bold=True, color="FFFFFF")
_HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
# Text that a spreadsheet would run as a formula if a cell started with it.
_FORMULA_PREFIXES = ("=", "+", "-", "@")


class AccountExistsError(Exception):
    """The email already belongs to an account."""


@dataclass
class BulkRow:
    row: int
    email: str
    employee_id: str
    role_label: str
    process_label: str
    role: Optional[str] = None
    process_id: Optional[str] = None
    errors: List[str] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        return {
            "row": self.row,
            "email": self.email,
            "employee_id": self.employee_id,
            "role": ROLE_DISPLAY.get(self.role or "", self.role_label),
            "process": self.process_label,
            "errors": list(self.errors),
        }


def create_admin_account(
    db, actor: User, *, email: str, role: str, process_id: str, is_approved: bool, password: str
) -> User:
    """Create one admin-provisioned account. It must change its password at first login."""
    if get_user_from_db(db, email=email):
        raise AccountExistsError(email)
    new_user = User(
        email=email,
        hashed_password=get_password_hash(password),
        role=role,
        process_id=process_id,
        is_approved=is_approved,
        must_change_password=True,
    )
    db.add(new_user)
    try:
        db.commit()
    except IntegrityError as exc:
        # Another admin created the same address between the check above and this commit.
        db.rollback()
        raise AccountExistsError(email) from exc
    db.refresh(new_user)
    log_activity(db, actor.id, "CREATE_USER", f"Created {role} user '{email}' (ID: {new_user.id})")
    log_activity(db, new_user.id, "USER_SIGNUP", f"Role: {role} (created by admin)")
    state = "approved" if is_approved else "is pending approval"
    db.add(Notification(
        user_id=new_user.id,
        message=f"Welcome to Smart Tutor! Your {role} account has been created and {state}.",
    ))
    db.commit()
    return new_user


def _write_text(ws, ref: str, value: str) -> None:
    """Write a cell as text, never as a formula, whatever the value starts with."""
    cell = ws[ref]
    cell.value = value
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        cell.data_type = "s"


def _header(ws, labels: Sequence[str], widths: Sequence[int]) -> None:
    for index, (label, width) in enumerate(zip(labels, widths), start=1):
        cell = ws.cell(row=1, column=index, value=label)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(vertical="center")
        ws.column_dimensions[cell.column_letter].width = width
    ws.freeze_panes = "A2"


def build_template(process_names: Sequence[str]) -> bytes:
    """The bulk-user workbook: Instructions, Users (with dropdowns), Example, and hidden Lists."""
    wb = Workbook()
    guide = wb.active
    guide.title = "Instructions"
    guide.column_dimensions["A"].width = 110
    sample = f"{EMPLOYEE_LOGIN_PREFIX}2000148019{EMPLOYEE_LOGIN_SUFFIX}"
    lines = [
        "Smart Tutor - bulk user creation",
        "",
        f"1. Fill in the '{USERS_SHEET}' sheet, one user per row (up to {MAX_BULK_USERS} rows). See 'Example' for a filled-in sample.",
        "2. Email: the user's company address (hexaware.com or testenvhexa.com).",
        "3. Employee ID: letters, digits, '-' or '_' only.",
        "4. Role and Process: choose from the dropdowns.",
        "5. Upload the file in Super Admin > Create Users > Bulk create, check the preview, then create.",
        "",
        f"Each user's first password is {EMPLOYEE_LOGIN_PREFIX}<Employee ID>{EMPLOYEE_LOGIN_SUFFIX}, e.g. {sample}.",
        "At first login the user must set a new password before they can use Smart Tutor.",
        "Only the 'Users' sheet is read. Rows on the 'Example' sheet are never created.",
    ]
    for row, text in enumerate(lines, start=1):
        _write_text(guide, f"A{row}", text)
    guide["A1"].font = Font(bold=True, size=14)

    users = wb.create_sheet(USERS_SHEET)
    _header(users, COLUMNS, (38, 18, 16, 32))

    example = wb.create_sheet("Example")
    _header(example, COLUMNS, (38, 18, 16, 32))
    example_process = process_names[0] if process_names else "<a process>"
    for row, values in enumerate((
        ("first.trainee@hexaware.com", "2000148019", "Trainee", example_process),
        ("first.trainer@hexaware.com", "2000148020", "Trainer", example_process),
        ("first.admin@hexaware.com", "2000148021", "Super Admin", example_process),
    ), start=2):
        for column, value in zip("ABCD", values):
            _write_text(example, f"{column}{row}", value)

    lists = wb.create_sheet(LISTS_SHEET)
    lists["A1"], lists["B1"] = "Roles", "Processes"
    for row, label in enumerate(ROLE_LABELS, start=2):
        _write_text(lists, f"A{row}", label)
    for row, name in enumerate(process_names, start=2):
        _write_text(lists, f"B{row}", name)
    lists.sheet_state = "hidden"

    last_row = MAX_BULK_USERS + 1
    role_choice = DataValidation(
        type="list", formula1=f"={LISTS_SHEET}!$A$2:$A${len(ROLE_LABELS) + 1}", allow_blank=True,
        showErrorMessage=True, errorTitle="Role", error="Choose a role from the list.",
    )
    users.add_data_validation(role_choice)
    role_choice.add(f"C2:C{last_row}")
    if process_names:
        process_choice = DataValidation(
            type="list", formula1=f"={LISTS_SHEET}!$B$2:$B${len(process_names) + 1}", allow_blank=True,
            showErrorMessage=True, errorTitle="Process", error="Choose a process from the list.",
        )
        users.add_data_validation(process_choice)
        process_choice.add(f"D2:D{last_row}")

    wb.active = wb.sheetnames.index(USERS_SHEET)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def read_upload(content: bytes) -> List[Tuple[int, Tuple[Any, Any, Any, Any]]]:
    """Rows of the Users sheet as (row number, raw cells). Raises ValueError for a bad file."""
    if not content.startswith(b"PK"):
        raise ValueError("The file is not an .xlsx workbook. Download the template and use that.")
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        # Not a zip, not a workbook, or a tampered part (e.g. an injected DOCTYPE), which fails
        # inside openpyxl with TypeError, AttributeError and the like. External entities are never
        # expanded - openpyxl parses with lxml in safe mode, and defusedxml - so this is only
        # about answering 400 rather than 500.
        raise ValueError(_UNREADABLE) from exc
    try:
        return _users_sheet_rows(wb)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(_UNREADABLE) from exc
    finally:
        wb.close()


_UNREADABLE = "The file could not be read as an .xlsx workbook. Download the template and use that."


def _users_sheet_rows(wb) -> List[Tuple[int, Tuple[Any, Any, Any, Any]]]:
    """The Users sheet rows as (row number, raw cells). The caller owns and closes the workbook."""
    if USERS_SHEET not in wb.sheetnames:
        raise ValueError(f"The workbook has no '{USERS_SHEET}' sheet. Download the template and use that.")
    rows = wb[USERS_SHEET].iter_rows(min_row=1, max_col=len(COLUMNS), values_only=True)
    header = next(rows, None)
    if not header or tuple(_cell_text(c).lower() for c in header) != tuple(c.lower() for c in COLUMNS):
        raise ValueError(f"The '{USERS_SHEET}' sheet must keep the template's columns: {', '.join(COLUMNS)}.")
    collected: List[Tuple[int, Tuple[Any, Any, Any, Any]]] = []
    for scanned, cells in enumerate(rows, start=2):
        if scanned > _MAX_SCANNED_ROWS:
            break
        cells = tuple(cells) + (None,) * (len(COLUMNS) - len(cells))
        if all(_cell_text(c) == "" for c in cells):
            continue
        collected.append((scanned, cells[: len(COLUMNS)]))
        if len(collected) > MAX_BULK_USERS:
            raise ValueError(f"The file has more than {MAX_BULK_USERS} users. Split it into smaller files.")
    if not collected:
        raise ValueError(f"The '{USERS_SHEET}' sheet has no users in it.")
    return collected


def _first_error(exc: Exception) -> str:
    return str(exc).strip() or "Invalid value."


def validate_rows(
    raw_rows: Iterable[Tuple[int, Tuple[Any, Any, Any, Any]]],
    processes_by_name: Dict[str, Tuple[str, str]],
    existing_emails: Set[str],
) -> List[BulkRow]:
    """Check every row. Nothing is created here; each row carries its own errors."""
    seen: Dict[str, int] = {}
    checked: List[BulkRow] = []
    for row_number, (email_cell, employee_cell, role_cell, process_cell) in raw_rows:
        email = normalize_email(_cell_text(email_cell))
        email_errors: List[str] = []
        try:
            email = safe_registration_email(email)
        except ValueError as exc:
            email_errors.append(_first_error(exc))
        else:
            if email in existing_emails:
                email_errors.append("This email is already registered.")
            elif email in seen:
                email_errors.append(f"Duplicate of row {seen[email]} in this file.")
            seen.setdefault(email, row_number)
        item = BulkRow(
            row=row_number,
            email=email,
            employee_id=_cell_text(employee_cell),
            role_label=_cell_text(role_cell),
            process_label=_cell_text(process_cell),
            errors=email_errors,
        )
        try:
            item.employee_id = safe_employee_id(employee_cell if employee_cell is not None else "")
            initial_login_for(item.employee_id)
        except ValueError as exc:
            item.errors.append(_first_error(exc))
        item.role = _ROLE_LOOKUP.get(item.role_label.lower())
        if not item.role:
            item.errors.append("Choose a Role from the list (Trainee, Trainer or Super Admin).")
        match = processes_by_name.get(item.process_label.lower())
        if match:
            item.process_id, item.process_label = match
        else:
            item.errors.append("Choose a Process from the list.")
        checked.append(item)
    return checked


def process_choices(db) -> Dict[str, Tuple[str, str]]:
    return {p.name.lower(): (p.id, p.name) for p in db.query(Process).all() if p.name}


def registered_emails(db, emails: Iterable[str]) -> Set[str]:
    wanted = sorted({e.lower() for e in emails if e})
    if not wanted:
        return set()
    rows = db.query(User.email).filter(func.lower(User.email).in_(wanted)).all()
    return {email.lower() for (email,) in rows}


def check_rows(db, raw_rows: List[Tuple[int, Tuple[Any, Any, Any, Any]]]) -> List[BulkRow]:
    """Validate rows against the current processes and accounts. Used for files and edited rows."""
    emails = [normalize_email(_cell_text(cells[0])) for _, cells in raw_rows]
    return validate_rows(raw_rows, process_choices(db), registered_emails(db, emails))


def check_upload(db, content: bytes) -> List[BulkRow]:
    """Read and validate an uploaded template."""
    return check_rows(db, read_upload(content))


def rows_from_payload(rows: Iterable[Any]) -> List[Tuple[int, Tuple[Any, Any, Any, Any]]]:
    """Rows the admin reviewed (and may have edited) in the browser, in the same shape as a file's.

    They go through exactly the same validate_rows() as an upload: the browser's copy is never
    trusted, so editing a row there cannot slip past a rule.
    """
    return [(r.row, (r.email, r.employee_id, r.role, r.process)) for r in rows]


def summarize(rows: List[BulkRow]) -> Dict[str, Any]:
    """Counts for the review screen, including how many of each role are ready to create."""
    ready = [row for row in rows if not row.errors]
    by_role = {role: 0 for role in ROLE_LABELS.values()}
    for row in ready:
        if row.role in by_role:
            by_role[row.role] += 1
    return {
        "total": len(rows),
        "valid": len(ready),
        "invalid": len(rows) - len(ready),
        "by_role": by_role,
        "rows": [row.summary() for row in rows],
    }


def run_bulk_creation(job_id: str, actor_id: str, rows: List[Dict[str, Any]], is_approved: bool) -> None:
    """Background job: create each validated row, recording a per-row result. Never logs passwords."""
    db = SessionLocal()
    results: List[Dict[str, Any]] = []
    created = 0
    try:
        actor = db.query(User).filter(User.id == actor_id).first()
        if actor is None:
            update_job_status(job_id, status="failed", error="The admin who started this upload no longer exists.")
            return
        total = len(rows)
        for index, row in enumerate(rows, start=1):
            update_job_status(job_id, status="running", progress_message=f"Creating user {index} of {total}...")
            outcome = {"row": row["row"], "email": row["email"], "role": ROLE_DISPLAY.get(row["role"], row["role"])}
            try:
                create_admin_account(
                    db, actor, email=row["email"], role=row["role"], process_id=row["process_id"],
                    is_approved=is_approved, password=initial_login_for(row["employee_id"]),
                )
                outcome["status"] = "created"
                created += 1
            except AccountExistsError:
                outcome.update(status="failed", error="This email is already registered.")
            except Exception as exc:
                db.rollback()
                logger.error("Bulk user creation failed for row %s: %s", row["row"], log_safe(exc))
                outcome.update(status="failed", error="Could not create this user.")
            results.append(outcome)
        log_activity(db, actor.id, "BULK_CREATE_USERS", f"Created {created} of {total} user(s) from a bulk upload")
        update_job_status(
            job_id,
            status="completed",
            progress_message=f"Created {created} of {total} user(s).",
            result={"created": created, "failed": total - created, "rows": results},
        )
    except Exception as exc:
        logger.error("Bulk user creation job failed: %s", log_safe(exc))
        update_job_status(job_id, status="failed", error="Bulk user creation stopped unexpectedly.")
    finally:
        db.close()
