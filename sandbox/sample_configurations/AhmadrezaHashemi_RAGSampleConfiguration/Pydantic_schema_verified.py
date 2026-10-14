"""
Module: Pydantic_schema_verified.py
Description: Defines the strict data validation schema for incoming job advertisements.
This ensures all payloads sent to the Qdrant Vector Database conform to our standard structure.
"""

from typing import List, Optional
from pydantic import BaseModel, Field

class VectorStorePayload(BaseModel):
    """
    Schema for validating and structuring job advertisement data before vectorization and ingestion.
    """
    # Meta fields
    comment: Optional[str] = Field(None, alias="__comment__", description="Optional comment field")

    # Core Strings (Required for RAG context)
    text: str = Field(..., description="The raw text of the job description.")
    role_normalized: str = Field(..., description="The standardized job title.")
    organization: str = Field(..., description="Company name, can be an empty string.")

    # Categorical Arrays (Defaults to empty list if not provided)
    seniority: List[str] = Field(default_factory=list)
    employment_type: List[str] = Field(default_factory=list)
    work_mode: List[str] = Field(default_factory=list)
    skills_required: List[str] = Field(default_factory=list)
    tools: List[str] = Field(default_factory=list)
    skills_preferred: List[str] = Field(default_factory=list)

    # Optional Strings
    domain: Optional[str] = None
    location: Optional[str] = None
    salary_currency: Optional[str] = None
    collected_at: Optional[str] = Field(None, description="ISO format date string (e.g., '2026-09-02')")
    education: Optional[str] = None
    industry: Optional[str] = None

    # Optional Integers
    experience_years_min: Optional[int] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    internship_duration_months: Optional[int] = None

# Example usage block (Executes only if run directly for testing purposes)
if __name__ == "__main__":
    import json
    
    # Dummy test payload to verify schema compilation
    dummy_data = {
        "role_normalized": "Senior/Lead Machine Learning Engineer",
        "text": "We are looking for a Data Engineer...",
        "organization": "Lantern Sample Corp",
        "seniority": ["senior", "lead"]
    }
    
    parsed = VectorStorePayload.model_validate(dummy_data)
    print("Schema compiled and validated successfully.")