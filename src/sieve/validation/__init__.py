from .engine import (
    DETECTORS, DISQUALIFYING_CODES, FREEMAIL_DOMAINS, GENERIC_LOCAL_PARTS, SEVERITY_PENALTY,
    ContactUnderTest, EmailPatternIndex, Flag, Severity, ValidationResult,
    validate_contact,
)

__all__ = [
    "DETECTORS", "DISQUALIFYING_CODES", "GENERIC_LOCAL_PARTS", "FREEMAIL_DOMAINS",
    "SEVERITY_PENALTY", "ContactUnderTest", "EmailPatternIndex", "Flag",
    "Severity", "ValidationResult", "validate_contact",
]
