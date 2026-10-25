# Data Engineering

This folder groups the Fanos data-acquisition and processing pipeline. Each
subfolder is owned by one contributor and contains a self-contained task,
its documentation, and its tests.

## Components

| Folder | Responsibility |
|---|---|
| `AhmadrezaHashemi_WebScraper/` | Web scraping and raw job-ad collection |
| `AhmadrezaHashemi_SupervisorAgent/` | Extractor/supervisor review loop and schema checks |
| `ManiYahyazadeh_DataExtractionPipeline/` | LLM-based extraction of raw ads into the canonical `JobAd` schema |
| `ZahraKamali_Validation_Task/` | Database access and Pydantic validation of extracted records |
| `ImanAbedini_NormalizerAgent/` | Deterministic-first normalization, LLM fallback, audit trail, and the file-based handoff to vectorization |

## Conventions

- All code comments and Markdown documentation are in English.
- API keys and other secrets live only in `.env`; `.env.example` documents the
  required variable names.
- Prompts are stored in versioned files, not inside Python modules.
- Every task folder has a `README.md` that explains how to run it and how it
  connects to the rest of the pipeline.
