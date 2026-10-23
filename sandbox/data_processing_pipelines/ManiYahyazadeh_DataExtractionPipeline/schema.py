from datetime import date
from enum import Enum

from pydantic import BaseModel, ConfigDict


class SourceType(str, Enum):
    JOB_POSTING = "job_posting"


class EmploymentType(str, Enum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    PROJECT_BASED = "project_based"


class WorkMode(str, Enum):
    ONSITE = "onsite"
    REMOTE = "remote"
    HYBRID = "hybrid"
    UNKNOWN = "unknown"


class GenderRequirement(str, Enum):
    ANY = "any"
    MALE = "male"
    FEMALE = "female"


class MilitaryServiceRequirement(str, Enum):
    ANY = "any"
    COMPLETED_OR_EXEMPT = "completed_or_exempt"
    COMPLETED_ONLY = "completed_only"
    EXEMPT_ONLY = "exempt_only"
    DEFERMENT_ACCEPTED = "deferment_accepted"
    PENDING_ACCEPTED = "pending_accepted"
    AMRIYEH_OFFERED = "amriyeh_offered"


class EducationDegree(str, Enum):
    ANY = "any"
    SECONDARY = "secondary"
    POST_SECONDARY = "post_secondary"
    ASSOCIATE = "associate"
    BACHELOR = "bachelor"
    MASTER = "master"
    DOCTORATE = "doctorate"
    PROFESSIONAL_DOCTORATE = "professional_doctorate"


class JobAd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    collector: str
    source_type: SourceType

    organization: str | None
    role_title: str
    role_normalized: str
    domain: str
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