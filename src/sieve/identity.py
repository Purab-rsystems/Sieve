"""Deterministic identifiers (PRD module M0).

Both IDs are UUID5 hashes, so the same input always produces the same ID on
every machine and every run. That is what satisfies the PRD's bitwise
reproducibility requirement: nothing here consults a clock or a random source.

    sv_account_id = UUID5(DNS namespace, normalized_domain)
    sv_contact_id = UUID5(account_id as namespace, normalized_email)

Falling back when a key is missing matters, because real files have gaps. An
account with no website hashes its normalized company name instead. A contact
with no email hashes its LinkedIn URL, then its name. The fallback used is
recorded so a reader can tell a domain-derived ID from a name-derived one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from .normalize.values import (
    normalize_company_name, normalize_domain, normalize_email,
    normalize_linkedin_url, normalize_person_name,
)

__all__ = ["IdentityResult", "account_id", "contact_id", "NAMESPACE_NO_DOMAIN"]

#: Namespace for accounts that have no usable domain. Fixed constant, so
#: name-derived IDs stay stable across runs too.
NAMESPACE_NO_DOMAIN = uuid.UUID("6f6b1d2e-0c3a-5f7b-9e21-4a8c5d3b7e11")


@dataclass(frozen=True)
class IdentityResult:
    """An assigned ID plus which key produced it."""

    value: str
    basis: str          # 'domain' | 'name' | 'email' | 'linkedin' | 'name' | 'row'

    def __str__(self) -> str:
        return self.value


def account_id(domain: str | None, company_name: str | None,
               fallback: str | None = None) -> IdentityResult:
    """Hash an account to a stable UUID5 string.

    Prefers the normalized domain, since two records sharing a domain are the
    same company. Falls back to the normalized legal name, which is why
    '33Across' and '33Across Inc.' collapse onto one ID.
    """
    resolved_domain = normalize_domain(domain)
    if resolved_domain:
        return IdentityResult(str(uuid.uuid5(uuid.NAMESPACE_DNS, resolved_domain)),
                              "domain")

    resolved_name = normalize_company_name(company_name)
    if resolved_name:
        return IdentityResult(str(uuid.uuid5(NAMESPACE_NO_DOMAIN, resolved_name)),
                              "name")

    marker = (fallback or "unknown").strip().lower()
    return IdentityResult(str(uuid.uuid5(NAMESPACE_NO_DOMAIN, marker)), "row")


def contact_id(parent_account_id: str, email: str | None,
               linkedin_url: str | None = None, full_name: str | None = None,
               fallback: str | None = None) -> IdentityResult:
    """Hash a contact to a stable UUID5 string within its account.

    The account ID is the namespace, so the same person at two companies gets
    two IDs, which is correct: they are two prospects.
    """
    namespace = uuid.UUID(parent_account_id)

    resolved_email = normalize_email(email)
    if resolved_email:
        return IdentityResult(str(uuid.uuid5(namespace, resolved_email)), "email")

    resolved_linkedin = normalize_linkedin_url(linkedin_url)
    if resolved_linkedin:
        return IdentityResult(str(uuid.uuid5(namespace, resolved_linkedin)), "linkedin")

    resolved_name = normalize_person_name(full_name)
    if resolved_name:
        return IdentityResult(str(uuid.uuid5(namespace, resolved_name)), "name")

    marker = (fallback or "unknown").strip().lower()
    return IdentityResult(str(uuid.uuid5(namespace, marker)), "row")
