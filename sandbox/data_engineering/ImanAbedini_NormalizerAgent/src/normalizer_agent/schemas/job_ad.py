"""Pydantic models for the (provisional) Fanos job-ad schema.

``JobAd`` is the strict output model (Appendix A of the task).
``JobAdRaw`` is the permissive intake model: same keys, but values are raw
strings/lists as they appeared in the advertisement. Unmapped enum values and
dirty strings are accepted at intake; they are normalized by the pipeline and
only the final record must validate against the strict model.
"""

from __future__ import annotations

import types
from datetime import date
from enum import Enum, StrEnum

from pydantic import BaseModel, ConfigDict


class SourceType(StrEnum):
    JOB_POSTING = "job_posting"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    PROJECT_BASED = "project_based"


class WorkMode(StrEnum):
    ONSITE = "onsite"
    REMOTE = "remote"
    HYBRID = "hybrid"
    # Schema-compatibility placeholder member; see docs/ASSUMPTIONS.md.
    UNKNOWN = "unknown"


class GenderRequirement(StrEnum):
    ANY = "any"
    MALE = "male"
    FEMALE = "female"


class MilitaryServiceRequirement(StrEnum):
    ANY = "any"
    COMPLETED_OR_EXEMPT = "completed_or_exempt"
    COMPLETED_ONLY = "completed_only"
    EXEMPT_ONLY = "exempt_only"
    DEFERMENT_ACCEPTED = "deferment_accepted"
    PENDING_ACCEPTED = "pending_accepted"
    AMRIYEH_OFFERED = "amriyeh_offered"


class EducationDegree(StrEnum):
    ANY = "any"
    SECONDARY = "secondary"
    POST_SECONDARY = "post_secondary"
    ASSOCIATE = "associate"
    BACHELOR = "bachelor"
    MASTER = "master"
    DOCTORATE = "doctorate"
    PROFESSIONAL_DOCTORATE = "professional_doctorate"


# Fields whose value must come from a Python enum, mapped to that enum.
# (Every member class is a ``str`` subclass enum, so lookup by value works.)
ENUM_FIELDS: dict[str, type[Enum]] = {
    "source_type": SourceType,
    "employment_type": EmploymentType,
    "work_mode": WorkMode,
    "education_degree": EducationDegree,
    "military_service_requirement": MilitaryServiceRequirement,
    "gender_requirement": GenderRequirement,
}

# Fields that must never be modified by the normalizer (R-provenance).
PROVENANCE_FIELDS: frozenset[str] = frozenset(
    {
        "document_id",
        "collector",
        "source_type",
        "text",
        "content_hash",
        "source_date",
        "collected_at",
        "version",
    }
)


class JobAd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    collector: str
    source_type: SourceType

    organization: str | None
    role_title: str
    role_normalized: str | None
    domain: str | None
    seniority: list[str]

    skills_required: list[str]
    skills_preferred: list[str]
    ai_skills: list[str]
    tools: list[str]
    soft_skills: list[str]

    employment_type: list[EmploymentType] | None
    work_mode: list[WorkMode]

    location: str | None

    experience_years_min: int | None

    language: list[str]

    salary_min: int | None
    salary_max: int | None
    salary_currency: str | None

    benefits: list[str]

    education: list[str]
    education_degree: list[EducationDegree]
    education_field: list[str]

    military_service_requirement: list[MilitaryServiceRequirement]
    gender_requirement: list[GenderRequirement]

    age_min: int | None
    age_max: int | None

    company_size_min: int | None
    company_size_max: int | None

    industry: str | None
    compensation_type: str | None

    internship_duration_months: int | None

    source_date: date | None
    collected_at: date

    text: str
    content_hash: str

    evidence_notes: list[str]
    candidate_new_skill: list[str]

    version: str


# Raw intake: every non-essential field accepts whatever the Extractor produced.
RawValue = str | int | float | None | list[str | int | float]
RawDateValue = str | date | None


class JobAdRaw(BaseModel):
    """Permissive intake model: raw string values, no enum coercion yet."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    collector: str
    source_type: str | None = None
    organization: RawValue = None
    role_title: RawValue = None
    role_normalized: RawValue = None
    domain: RawValue = None
    seniority: RawValue = None
    skills_required: RawValue = None
    skills_preferred: RawValue = None
    ai_skills: RawValue = None
    tools: RawValue = None
    soft_skills: RawValue = None
    employment_type: RawValue = None
    work_mode: RawValue = None
    location: RawValue = None
    experience_years_min: RawValue = None
    language: RawValue = None
    salary_min: RawValue = None
    salary_max: RawValue = None
    salary_currency: RawValue = None
    benefits: RawValue = None
    education: RawValue = None
    education_degree: RawValue = None
    education_field: RawValue = None
    military_service_requirement: RawValue = None
    gender_requirement: RawValue = None
    age_min: RawValue = None
    age_max: RawValue = None
    company_size_min: RawValue = None
    company_size_max: RawValue = None
    industry: RawValue = None
    compensation_type: RawValue = None
    internship_duration_months: RawValue = None
    source_date: RawDateValue = None
    collected_at: str | date = None  # type: ignore[assignment]
    text: str
    content_hash: str
    evidence_notes: RawValue = None
    candidate_new_skill: RawValue = None
    version: str = "0"


def classify_raw_validation_error(exc: Exception) -> str:
    """Map a JobAdRaw validation failure to an envelope error code.

    Returns ``UNKNOWN_KEY`` when the input has extra keys, ``SCHEMA_INVALID``
    for every other structural problem (missing/incorrectly typed essentials).
    """

    errors = getattr(exc, "errors", None)
    if callable(errors):
        details = errors()
    else:  # pragma: no cover - defensive
        return "SCHEMA_INVALID"
    for err in details:
        if err.get("type") in {"extra_forbidden", "extra"}:
            ".".join(str(part) for part in err.get("loc", ()))
            return "UNKNOWN_KEY"
    return "SCHEMA_INVALID"


__all__ = [
    "ENUM_FIELDS",
    "PROVENANCE_FIELDS",
    "EducationDegree",
    "EmploymentType",
    "GenderRequirement",
    "JobAd",
    "JobAdRaw",
    "MilitaryServiceRequirement",
    "SourceType",
    "WorkMode",
    "classify_raw_validation_error",
    "types",
]
