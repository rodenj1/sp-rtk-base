"""Shared UI form validation helpers for NiceGUI pages.

Provides reusable validation functions and factory helpers
for NiceGUI input field ``validation`` dictionaries.

NiceGUI validation format: ``{"Error message": callable}``
where the callable returns True if the value is valid.

Note:
    Validation functions are named functions (not lambdas) because
    pyright strict mode cannot infer parameter types for lambdas.
"""

from __future__ import annotations

from typing import Any

# Type alias for NiceGUI validation dictionaries
ValidationDict = dict[str, Any]

# Type alias for source/destination field definitions:
# (field_name, label, default_value, validation_dict)
FieldDef = tuple[str, str, str, ValidationDict]


# ---------------------------------------------------------------------------
# Core validators
# ---------------------------------------------------------------------------


def is_non_empty(v: str) -> bool:
    """Validate that a string value is non-empty after stripping.

    Args:
        v: The value to validate.

    Returns:
        True if the stripped string is non-empty.
    """
    return bool(str(v).strip())


def is_valid_port(v: str) -> bool:
    """Validate that a value is a valid TCP/UDP port number (1-65535).

    Args:
        v: The value to validate.

    Returns:
        True if the value is an integer between 1 and 65535.
    """
    s = str(v)
    return s.isdigit() and 1 <= int(s) <= 65535


def is_numeric(v: str) -> bool:
    """Validate that a value is a numeric (integer) string.

    Args:
        v: The value to validate.

    Returns:
        True if the value contains only digits.
    """
    return str(v).isdigit()


# ---------------------------------------------------------------------------
# Validation rule factories
# ---------------------------------------------------------------------------


def required(label: str) -> ValidationDict:
    """Create a 'required field' validation rule.

    Args:
        label: The field label for the error message.

    Returns:
        A validation dict with a single required rule.
    """
    return {f"{label} is required": is_non_empty}


def port_validation() -> ValidationDict:
    """Create port number validation rules (required + range 1-65535).

    Returns:
        A validation dict with required and port-range rules.
    """
    return {
        "Port is required": is_non_empty,
        "Port must be 1-65535": is_valid_port,
    }


def numeric_validation(label: str) -> ValidationDict:
    """Create numeric-only validation rules (required + digits only).

    Args:
        label: The field label for error messages.

    Returns:
        A validation dict with required and numeric rules.
    """
    return {
        f"{label} is required": is_non_empty,
        f"{label} must be a number": is_numeric,
    }
