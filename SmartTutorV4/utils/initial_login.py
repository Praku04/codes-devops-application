"""
The login every admin-created account starts with: St@<EmployeeID>#2026.

Used by the single Create User form and by bulk upload, so both build it the same way. The
pattern was chosen by the product owner. Anyone who knows it and a colleague's employee ID can
derive that colleague's starting login, which is why such accounts are flagged
must_change_password and the server refuses every other action until the user replaces it.

Kept free of application imports so schemas and services can both use it without a cycle.
"""

from typing import Any

from utils.input_validation import safe_identifier, safe_password

EMPLOYEE_LOGIN_PREFIX = "St@"
EMPLOYEE_LOGIN_SUFFIX = "#2026"
EMPLOYEE_ID_MAX_LENGTH = 32
# Users per bulk upload (file or edited rows). Each password hash takes ~0.5 s, so this bounds the
# background job at a few minutes.
MAX_BULK_USERS = 500


def safe_employee_id(value: Any) -> str:
    """Letters, digits, '_' and '-' only, so the derived login can never contain anything odd."""
    # Excel stores a numeric employee ID as a number, so 2000148019 can arrive as 2000148019.0.
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return safe_identifier(value, field="Employee ID", max_length=EMPLOYEE_ID_MAX_LENGTH)


def initial_login_for(employee_id: Any) -> str:
    """The starting login for an employee ID, checked against the normal password policy."""
    employee = safe_employee_id(employee_id)
    return safe_password(f"{EMPLOYEE_LOGIN_PREFIX}{employee}{EMPLOYEE_LOGIN_SUFFIX}", field="Initial password")
