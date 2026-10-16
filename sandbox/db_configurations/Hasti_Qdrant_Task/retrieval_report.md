# Retrieval Report

- Date: 2026-10-16
- Collection: `jobs`
- Model: `google/embeddinggemma-300m`
- Query prefix: `task: search result | query: {prompt}`
- Result: **6/6 tests passed**

## Notes

**Embedding method.** Search prompts are embedded with `google/embeddinggemma-300m` using the mandatory prefix `task: search result | query: `. Documents are embedded from `payload["text"]` with the model's document prompt (`title: none | text: ...`). All vectors are 768-dimensional, L2-normalised, and compared with cosine distance.

**Why the vectors in the JSONL file were not used.** The `vector` field of `Vectorized_raw_data.jsonl` is not compatible with EmbeddingGemma. For three sampled records, the cosine similarity between the stored vector and a fresh EmbeddingGemma embedding of the same text (title only, full text, with and without the document prefix) was between about -0.02 and 0.07, i.e. unrelated. The stored vectors are normalised and very similar to each other (pairwise cosine: mean 0.84, min 0.75, max 0.95 over the 10 records), which suggests they were produced by a different model that also outputs 768 dimensions. With them, semantic search scored about 0.0 for every prompt and the ranking was not meaningful. `ingest_qdrant.py` therefore re-embeds `payload["text"]` with EmbeddingGemma by default; pass `--use-file-vectors` to ingest the original vectors unchanged.

**Dataset size.** Only 10 records are available. Each filter test matches just 2 points, so the filters are verified to be applied correctly, but the filtered rankings are not a strong test of relevance.

**Pass criteria.** Semantic tests pass when 3 results are returned and at least one expected whole-word keyword appears in the top-3 results (for test 3, in the job title). Filter tests pass when every returned hit satisfies the filter and the number of hits equals `min(3, number of points matching the filter)`.

## Test 1: Pure semantic 1

- Prompt: `python backend developer`
- Filter: none

| Rank | Point ID | Score | Role |
|---|---|---|---|
| 1 | `5d8668dc-4243-48ef-9080-d53f89587490` | 0.4319 | AI Software Engineer |
| 2 | `010e2bd5-e92f-4b53-96f6-fe270c69be74` | 0.4283 | AI Engineer |
| 3 | `09085908-770b-4fb2-86c8-56b879e90157` | 0.3884 | AI Automation Specialist |

- Verdict: **PASS**: relevant keywords found in top-3: ['python', 'backend']

## Test 2: Pure semantic 2

- Prompt: `data scientist with machine learning experience`
- Filter: none

| Rank | Point ID | Score | Role |
|---|---|---|---|
| 1 | `04a5ea7d-340e-49bb-90a7-7e33aaf01211` | 0.3980 | AI Engineer |
| 2 | `b55ae178-b5fa-4d67-bc45-c1e17169ba7c` | 0.3592 | AI Research Engineer |
| 3 | `09085908-770b-4fb2-86c8-56b879e90157` | 0.3554 | AI Automation Specialist |

- Verdict: **PASS**: relevant keywords found in top-3: ['data', 'machine learning']

## Test 3: Pure semantic 3

- Prompt: `speech recognition engineer`
- Filter: none

| Rank | Point ID | Score | Role |
|---|---|---|---|
| 1 | `e3b85605-6530-495c-822e-6556bada0fe2` | 0.5034 | Speech AI Engineer |
| 2 | `010e2bd5-e92f-4b53-96f6-fe270c69be74` | 0.4126 | AI Engineer |
| 3 | `b55ae178-b5fa-4d67-bc45-c1e17169ba7c` | 0.3761 | AI Research Engineer |

- Verdict: **PASS**: relevant keywords found in top-3: ['speech']

## Test 4: Hard filter 1

- Prompt: `software developer`
- Filter: salary_min >= 40000000

| Rank | Point ID | Score | Role |
|---|---|---|---|
| 1 | `010e2bd5-e92f-4b53-96f6-fe270c69be74` | 0.3717 | AI Engineer |
| 2 | `e3b85605-6530-495c-822e-6556bada0fe2` | 0.2820 | Speech AI Engineer |

- Verdict: **PASS**: all 2 hits satisfy the filter (2 points match overall)

## Test 5: Hard filter 2

- Prompt: `software developer`
- Filter: seniority = mid

| Rank | Point ID | Score | Role |
|---|---|---|---|
| 1 | `010e2bd5-e92f-4b53-96f6-fe270c69be74` | 0.3717 | AI Engineer |
| 2 | `04a5ea7d-340e-49bb-90a7-7e33aaf01211` | 0.2829 | AI Engineer |

- Verdict: **PASS**: all 2 hits satisfy the filter (2 points match overall)

## Test 6: Hard filter 3 (combined)

- Prompt: `software developer`
- Filter: work_mode = onsite AND salary_min >= 40000000

| Rank | Point ID | Score | Role |
|---|---|---|---|
| 1 | `010e2bd5-e92f-4b53-96f6-fe270c69be74` | 0.3717 | AI Engineer |
| 2 | `e3b85605-6530-495c-822e-6556bada0fe2` | 0.2820 | Speech AI Engineer |

- Verdict: **PASS**: all 2 hits satisfy the filter (2 points match overall)

## Notes

**Embedding method.** Search prompts are embedded with `google/embeddinggemma-300m` using the mandatory prefix `task: search result | query: `. Documents are embedded from `payload["text"]` with the model's document prompt (`title: none | text: ...`). All vectors are 768-dimensional, L2-normalised, and compared with cosine distance.

**Why the vectors in the JSONL file were not used.** The `vector` field of `Vectorized_raw_data.jsonl` is not compatible with EmbeddingGemma. For three sampled records, the cosine similarity between the stored vector and a fresh EmbeddingGemma embedding of the same text (title only, full text, with and without the document prefix) was between about -0.02 and 0.07, i.e. unrelated. The stored vectors are normalised and very similar to each other (pairwise cosine: mean 0.84, min 0.75, max 0.95 over the 10 records), which suggests they were produced by a different model that also outputs 768 dimensions. With them, semantic search scored about 0.0 for every prompt and the ranking was not meaningful. `ingest_qdrant.py` therefore re-embeds `payload["text"]` with EmbeddingGemma by default; pass `--use-file-vectors` to ingest the original vectors unchanged.

**Dataset size.** Only 10 records are available. Each filter test matches just 2 points, so the filters are verified to be applied correctly, but the filtered rankings are not a strong test of relevance.

**Pass criteria.** Semantic tests pass when 3 results are returned and at least one expected whole-word keyword appears in the top-3 results (for test 3, in the job title). Filter tests pass when every returned hit satisfies the filter and the number of hits equals `min(3, number of points matching the filter)`.
