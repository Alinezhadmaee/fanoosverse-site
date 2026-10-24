# 🛠️ Sandbox Directory Guide

This directory serves as an isolated "sandbox" environment to test our approaches and see the big picture before integrating anything into the main Fanoos Lantern Project environment. 

To prevent clutter and maintain a clean testing laboratory, this directory is governed by a strict organizational structure and automated Git enforcement.

## 🏗️ Sandbox Architecture
The root `/sandbox/` directory is **immutable**. You cannot create new folders directly in the root. All test scripts and Proof of Concept (PoC) files must be placed inside one of these four designated domain folders:

*   **`/data_engineering/`**: Web scraping, data processing pipelines, Pydantic scripts, and data validation testing.
*   **`/ai_and_agents/`**: LLM API testing, prompt evaluations, entity extraction, RAG testing, and agent workflows.
*   **`/infrastructure_and_db/`**: Database connections, Qdrant (Vector DB) tuning, PostgreSQL tests, and VPS setup scripts.
*   **`/misc_experiments/`**: One-off sample configurations and temporary isolation tests that don't fit above.

## 📝 Strict Naming Convention (Enforced)
Inside the four domain folders, you must create a specific directory for your task. 
**Our GitHub Actions gatekeeper will automatically block your Pull Request if your folder does not exactly match this format:**

`[AssigneeName]_[ShortTaskName]`

**Examples of valid folders:**
*   `/sandbox/data_engineering/20261024_ZahraKamali_PydanticValidation/`
*   `/sandbox/ai_and_agents/20261024_MahanValadi_JevModelTest/`

## ⚠ Sandbox Rules
1. **No Production Code:** Do not push final, production-ready code directly here. This space is strictly for Trial & Error and PoC scripts.
2. **The Root is Locked:** Do not leave your test scripts scattered in the root of the sandbox directory. The `.gitignore` is configured to reject rogue files.
3. **Absolute Freedom Inside Your Folder:** Once inside your properly named task folder, you have complete freedom to structure your code, environments, and tests however you like.
4. **Document Your Code:** Inside your specific task folder, include a short `Readme.md` or use clear code comments to explain exactly what the test does, its dependencies, and what task it belongs to.

If you have any questions about how to push your files to this directory or format your folder names, don't hesitate to contact the technical leads.