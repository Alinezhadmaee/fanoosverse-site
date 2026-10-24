# Test data for "data extractor agent supervisor"
# NOTE: this is just a test code, Mani Yahyazadeh's data extractor agent must be completed, be matched with this module, and finally be put in a real pipeline 
import os
import json
from datetime import datetime
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError
from openai import OpenAI

# ==========================================
# 1. PYDANTIC SCHEMA (STRUCTURED OUTPUT)
# ==========================================
class SupervisorDecision(BaseModel):
    is_approved: bool = Field(
        description="True if the extracted fields perfectly match the initial text without errors, omissions, or hallucinations. False otherwise."
    )
    rejection_reason: str = Field(
        description="If rejected, provide a highly specific explanation of exactly which fields are wrong or missing. If approved, return an empty string."
    )

# ==========================================
# 2. AVALAI CLIENT SETUP
# ==========================================
load_dotenv(dotenv_path=".env")
client = OpenAI(
    api_key=os.environ.get("AVALAI_API_KEY"),
    base_url="https://api.avalai.ir/v1",
    default_headers={"Accept-Encoding": "gzip, deflate"}
)

# ==========================================
# 3. HUMAN SUPERVISION MODULE
# ==========================================
def requires_human_supervision(completed_fields: dict) -> dict:
    print("\n" + "="*55)
    print(" 🛑 PENDING HUMAN APPROVAL 🛑 ")
    print("="*55)
    print("The AI Supervisor approved this data. Human verification required.")
    print(f"Role:    {completed_fields.get('role_title', 'N/A')}")
    print(f"Company: {completed_fields.get('organization', 'N/A')}")
    print("-" * 55)
    
    user_input = input("Type 'Y' to approve, or type your reason for rejection: ").strip()
    
    if user_input.upper() == 'Y':
        return {"status": "approved", "reason": None}
    else:
        return {"status": "rejected", "reason": user_input}

# ==========================================
# 4. MAIN SUPERVISOR AGENT PIPELINE
# ==========================================
def run_supervisor_pipeline(completed_fields: dict, initial_data: str) -> dict:
    """
    Evaluates extracted data against raw text. Routes to a human if the AI approves.
    Returns a strict dictionary payload formatted for the extraction agent.
    """
    
    # 4a. Elaborated Prompt Engineering
    system_prompt = """You are a strict, highly accurate Data Supervisor Agent. 
    Your objective is to validate 'Completed Fields' (a JSON object) against 'Initial Data' (raw text) to guarantee 100% fidelity.

    Evaluation Criteria:
    1. Anti-Hallucination: Every value in the Completed Fields MUST exist in or be strictly logically inferred from the Initial Data.
    2. Numerical Accuracy: Verify salaries, years of experience, and ages. If a salary is converted (e.g., millions to absolute Toman), verify the math (e.g., 130 million = 130000000).
    3. Categorical Verification: Validate seniority (e.g., senior/lead), employment type, and work mode against the text context.
    4. Completeness: Ensure no critical skills or tools mentioned in the text were omitted from the extracted lists.

    You must output your decision strictly in JSON format matching EXACTLY this schema:
    {
    "is_approved": boolean (true if perfectly extracted, false otherwise),
    "rejection_reason": string (highly specific explanation if rejected, or empty string if approved)
    }"""

    user_message = f"""
Initial Data (Raw Text):
{initial_data}

Completed Fields (Parsed JSON):
{json.dumps(completed_fields, ensure_ascii=False, indent=2)}
"""

    # 4b. AI Evaluation Execution
    try:
        response = client.chat.completions.create(
            model="gemini-3.8-flash",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            response_format={"type": "json_object"},
            temperature=0.0 # Zero temperature for strict analytical evaluation
        )
        
        # Enforce strict schema validation using Pydantic
        raw_response = response.choices[0].message.content
        ai_decision = SupervisorDecision.model_validate_json(raw_response)
        
    except ValidationError as e:
        # Failsafe if the LLM breaks the JSON schema
        return _build_rejection_payload(
            completed_fields, initial_data, 
            status="rejected by agent mode", 
            reason=f"Agent returned malformed evaluation schema: {str(e)}", 
            rejecter="system_supervisor"
        )

    # 4c. Branch 1: AI Rejects the Data
    if not ai_decision.is_approved:
        return _build_rejection_payload(
            completed_fields, initial_data,
            status="rejected by agent mode",
            reason=ai_decision.rejection_reason,
            rejecter="ai_supervisor_agent"
        )
    
    # 4d. Branch 2: AI Approves -> Pass to Human
    human_decision = requires_human_supervision(completed_fields)
    
    if human_decision["status"] == "approved":
        return {
            "status": "approved",
            "final_data": completed_fields,
            "timestamp": datetime.now().isoformat()
        }
    else:
        # Branch 3: Human Rejects the Data
        return _build_rejection_payload(
            completed_fields, initial_data,
            status="rejected by human mode",
            reason=human_decision["reason"],
            rejecter="human_supervisor"
        )

# ==========================================
# 5. PAYLOAD FORMATTER UTILITY
# ==========================================
def _build_rejection_payload(completed_fields: dict, initial_data: str, status: str, reason: str, rejecter: str) -> dict:
    """Helper function to guarantee the 4 required outputs are formatted identically for the extraction agent."""
    return {
        # 1. The inputs returned exactly as they were provided
        "input": {
            "completed_fields": completed_fields,
            "initial_data": initial_data
        },
        # 2. Inform the extraction agent of the rejection status
        "status": status,
        # 3. Show the extraction agent exactly why it was rejected
        "denial_reason": reason,
        # 4. Actionable message requiring correction
        "message": "Your extraction was rejected. Please review the denial_reason and correct the data accordingly before resubmitting.",
        # Meta: Track who rejected it for the logs
        "rejecter": rejecter,
        "timestamp": datetime.now().isoformat()
    }

# # --- Example Execution Context ---
# if __name__ == "__main__":
#     # Simulate passing the test JSON row
#     test_completed_fields = {
#         "role_title": "مهندس ارشد یادگیری ماشین",
#         "organization": "ونچر استودیوی آرگومان",
#         "salary_min": 130000000,
#         "salary_max": 180000000
#         # ... (other parsed fields)
#     }
#     test_initial_data = "مهندس ارشد یادگیری ماشین... حقوق 130 - 180 میلیون تومان... استخدام کننده: ونچر استودیوی آرگومان"
    
#     # Execute the pipeline
#     result = run_supervisor_pipeline(test_completed_fields, test_initial_data)
#     print("\n--- FINAL PIPELINE RESULT ---")
#     print(json.dumps(result, ensure_ascii=False, indent=2))

