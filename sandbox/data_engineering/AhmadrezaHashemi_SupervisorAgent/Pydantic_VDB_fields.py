# The fields selected from database for storing in VDB and RAG works: -> based on pydantic schemas
from typing import List, Optional
from pydantic import BaseModel, Field

class VectorStorePayload(BaseModel):
    # Meta fields
    comment: Optional[str] = Field(None, alias="__comment__", description="Optional comment field, mapped from __comment__")
    
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
    
    # Optional Strings (Can be null based on your JSONL example)
    domain: Optional[str] = None
    location: Optional[str] = None
    salary_currency: Optional[str] = None
    collected_at: Optional[str] = Field(None, description="ISO format date string (e.g., '2026-09-02')")
    education: Optional[str] = None
    industry: Optional[str] = None
    
    # Optional Integers (Can be null based on your JSONL example)
    experience_years_min: Optional[int] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    internship_duration_months: Optional[int] = None

# --- Example Usage ---
if __name__ == "__main__":
    raw_data = {
      "__comment__" : "fields below are what we consider for vector databases and RAG works",
      "role_normalized": "Senior/Lead Machine Learning Engineer",
      "domain": "AI/ML",
      "seniority": ["senior", "lead"],
      "employment_type": ["full_time"],
      "work_mode": ["onsite"],
      "location": "تهران",
      "salary_currency": None,
      "experience_years_min": 5,
      "salary_min": 130000000,
      "salary_max": 180000000,
      "collected_at": "2026-09-02",
      "education": "کارشناسی - کامپیوتر/فناوری اطلاعات",
      "industry": "fintech",
      "internship_duration_months" : 8,
      "text": "Data Engineer (AI & Data Platform)...", # Truncated for example
      "organization" : "",
      "skills_required": ["Machine Learning", "Logistic Regression", "CI/CD"],
      "tools": ["Python", "SQL Server", "Git", "Docker", "Pandas", "Scikit-learn", "SHAP", "LIME", "XGBoost", "LightGBM", "CatBoost", "FastAPI", "MLflow"],
      "skills_preferred": ["Marketing Analytics", "Consumer Behavior Analysis", "Retail Analytics", "FMCG", "Forecasting", "Marketing Mix Modeling", "Attribution Modeling", "Causal Inference"]
    }
    
    # Validate and parse the data
    parsed_payload = VectorStorePayload.model_validate(raw_data)
    
    # Exporting back to JSON drops nulls automatically if exclude_none is True (useful for Qdrant payloads)
    clean_json = parsed_payload.model_dump_json(exclude_none=True, by_alias=True)
    print(clean_json)