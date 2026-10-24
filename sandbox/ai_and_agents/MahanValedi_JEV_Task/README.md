# MahanValedi_JEV_Task: Smart Gatekeeper for RAG

This folder contains a gatekeeper layer for the Fanos RAG pipeline. It sits between Qdrant and the generator LLM and uses a System One decision model (TypeSafe Jev API, or the open-source OpenDecider model running locally) to answer one question: "does this retrieved passage contain the answer to the user's question?" Passages with probability strictly greater than 0.60 pass. If none pass, the result is `Not Found in Documents`.

## Files

| File | Purpose |
|---|---|
| `jev_rag_filter.py` | Main script: Qdrant retrieval, Jev API judge, local OpenDecider judge, 60% threshold logic, latency timing, CLI. |
| `pipeline_explanation.md` | Technical explanation of the code, how to run the API and the local model, latency analysis, risks. |
| `jev_creative_pipelines.md` | Brainstorming document with other uses of System One models in the project. |
| `prompts/judge_instructions.txt` | The judge prompt, kept outside the code on purpose. |
| `.env.example` | Template listing every setting. Copy to `.env` and fill in. |
| `.gitignore` | Keeps `.env` and caches out of the repository. |
| `requirements.txt` | Python dependencies. |

## Quick start

```
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then edit .env
python jev_rag_filter.py --prompt "your test question"
```

- Local open-source model: set `JEV_BACKEND=local` (default).
- TypeSafe Jev API: set `JEV_BACKEND=api` and `MODEL_ACCESS_KEY` in `.env`.
- Measure latency: add `--repeat 10`.

## Rules followed

- All comments and documentation are in English.
- No API key and no prompt text is hardcoded; keys live only in `.env`, which is git-ignored.
- The module has a descriptive docstring and inline comments.

## Opening the Pull Request

Place this folder at `sandbox/rag_gatekeeper/MahanValedi_JEV_Task`, then:

```
git checkout -b feature/mahan-jev-gatekeeper
git add sandbox/rag_gatekeeper/MahanValedi_JEV_Task
git status                         # confirm .env is NOT listed
git commit -m "Add Smart Gatekeeper RAG filter with Jev API and local OpenDecider"
git push -u origin feature/mahan-jev-gatekeeper
```

Then open a Pull Request into `main`.
