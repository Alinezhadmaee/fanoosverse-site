# Smart Gatekeeper: Pipeline Explanation

This document explains how `jev_rag_filter.py` works, how to run the open-source local model, and what the extra layer costs in latency. It is the technical companion to the script in the same folder.

## 1. Problem

Qdrant ranks chunks by vector similarity. Similarity means "close in topic", not "contains the answer". A question about base salary can retrieve a chunk about working hours, because both belong to the same HR document. If that chunk reaches the generator LLM, the model may fill the gap with invented content (hallucination).

The gatekeeper adds a cheap yes/no check between retrieval and generation.

```
user prompt
    -> embed -> Qdrant (top-k chunks)
    -> Gatekeeper: "does this passage answer the question?" -> probability
          p > 0.60  -> chunk goes to the generator LLM
          p <= 0.60 -> dropped
    -> no chunk left -> return "Not Found in Documents" (generator never called)
```

## 2. The decision model

JEV is a *System One* model from TypeSafe AI. It does not write text. It receives a `state` and typed questions and returns calibrated probabilities. The relevant question type is `noul`: for a statement given in `instructions`, the model returns the probability (0 to 1) that the statement is true.

Calibrated means that if the model says 0.8 across many cases, the statement is true in roughly 80% of them. This is what makes a fixed numeric threshold meaningful.

Mapping to this task:

| System One concept | Our use |
|---|---|
| `state` | `{"question": <user prompt>, "passage": <retrieved chunk>}` |
| question type | `noul` |
| `instructions` | judge prompt, loaded from `prompts/judge_instructions.txt` |
| answer | `answers.has_answer.noul` = P(passage answers the question) |

Note that a valid probability is not the same as a correct answer. The model can be confidently wrong, so the threshold should be tuned on labeled examples (section 7).

## 3. How the code works

`jev_rag_filter.py` is organized in small, replaceable parts:

| Part | Responsibility |
|---|---|
| `Config` | Reads every setting from environment variables (and `.env`). Fails early if a required value is missing. |
| `load_instructions` | Reads the judge prompt from its own file. |
| `Retriever` | Embeds the prompt (sentence-transformers) and queries Qdrant with `query_points`. |
| `ApiJudge` | Calls the TypeSafe Jev API (`/v1/systemone`), in parallel across passages, with retry and exponential backoff on HTTP 429/5xx. |
| `LocalJudge` | Runs the open-source OpenDecider model on this machine through the same System One interface. |
| `apply_threshold` | Pure function: keeps a passage only if its probability is strictly greater than the threshold. |
| `run_gatekeeper` | Connects retrieval, judging and thresholding, and records per-step timings. |

Design decisions:

- **Strict comparison.** `p > 0.60` passes. `p == 0.60` is rejected, exactly as specified.
- **Fail loudly.** If the judge backend is down, the script exits with an error. It does not print "Not Found in Documents", because that would hide an outage behind a misleading answer.
- **One interface, two backends.** `JEV_BACKEND=api` or `local` switches the judge without touching other code.
- **Embedding recipe must match the index.** `EMBEDDING_MODEL`, the optional `EMBEDDING_QUERY_PREFIX` and vector normalization have to follow the recipe used to build the Qdrant collection, otherwise retrieval quality collapses before the gate even runs. The team's sample script uses a Gemma retrieval prefix and normalized vectors, and the script reproduces both.
- **Passage text.** The judge needs readable text. `QDRANT_TEXT_FIELDS` selects which payload fields are joined into the passage; if it is empty, the whole payload is used.

## 4. Running with the TypeSafe Jev API

Jev is served by DigitalOcean Serverless Inference through its own endpoint, which is not OpenAI compatible.

1. Create a model access key in the DigitalOcean Control Panel (Serverless Inference tab). Jev is a third-party model, so the account must be on a qualifying tier. A tier error looks like `this model is not available for your subscription tier`.
2. In `.env` set:
   ```
   JEV_BACKEND=api
   MODEL_ACCESS_KEY=<your key>
   ```
3. Run:
   ```
   python jev_rag_filter.py --prompt "your test question"
   ```

Access and cost (as reported in September 2026 by third-party guides, to be re-checked): Jev is in early access, TypeSafe's own API keys are issued through a waitlist at console.typesafe.ai, and no free tier is published. Pricing is reported as $0.042 per million input tokens with free output, so a test run of a few thousand decisions costs cents. A temporary free window on one gateway was reported to end on 25 September 2026. If a key is obtained directly from TypeSafe, set `JEV_API_URL` to their endpoint (see `.env.example`) and verify the request format in their documentation, because only the DigitalOcean format was checked while writing this script.

Limits to know: the whole request may contain at most 64K tokens, and the `state` plus the longest question at most 32K tokens. Typical chunks are far below this.

Check the vendor documentation at https://docs.typesafe.ai for any change to the request format, because the product is in early access and may evolve.

## 5. Running the open-source local model

The closed API is limited, so the task requires a self-hosted alternative. The chosen option is **OpenDecider** (Apache-2.0), an open System One model with the same `state` + typed-questions interface as Jev.

| Model | Size | Notes |
|---|---|---|
| `manjunathshiva/opendecider-small` | 4B (LoRA adapter on Qwen3-4B-Instruct-2507) | Best calibration of the family. Downloads about 8 GB base model + 126 MB adapter. |
| `manjunathshiva/opendecider-nano` | about 400M | Much lighter; the model card describes it as the choice for many decisions per second. |

Hardware reported on the model card for `small`:

| Hardware | Memory | Latency, one question |
|---|---|---|
| NVIDIA L40S (bf16) | about 9 GB | 38 ms |
| MacBook Pro M4 Max, 64 GB | 8.9 GiB | 141 ms |
| Mac mini M4, 16 GB | 8.9 GiB | 280 ms |
| CPU only (fp32) | about 17 GB RAM | slow, not recommended |

Setup:

```
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # installs opendecider[small]
```

In `.env` set:

```
JEV_BACKEND=local
LOCAL_MODEL_ID=manjunathshiva/opendecider-nano   # or ...-small if you have the GPU/RAM
```

Run the same command as for the API. The first run downloads the weights, so expect a long start. Later runs load from the local cache. If `opendecider` fails to install or import on your platform, follow the installation notes in the project's GitHub README (https://github.com/manjunathshiva/opendecider).

Important limits of the local model, taken from its model card:

- **English only so far.** No multilingual evaluation exists. If the Fanos documents or queries are in Persian, results must be tested before trusting any threshold.
- Accuracy figures on the card are reported by the model's author, not by an independent party.
- It answers one question at a time.

## 6. Latency impact

### 6.1 What is added

The gate adds one judge call per retrieved chunk after Qdrant returns. Embedding and Qdrant timings are unchanged.

```
added latency = judge time for the top-k chunks
```

- **API backend:** requests run in parallel, so the added time is close to one call plus the slowest tail.
- **Local backend:** chunks are scored sequentially, so the added time is about `k x per-question latency`.

### 6.2 Reported figures and estimates for k = 3

| Backend | Per question (reported) | Estimated added time, k = 3 |
|---|---|---|
| Jev API | 404 ms median, measured through the API including network | about 400 ms (parallel) |
| OpenDecider-small, L40S | 38 ms | about 115 ms |
| OpenDecider-small, M4 Max | 141 ms | about 425 ms |
| OpenDecider-small, Mac mini M4 | 280 ms | about 840 ms |

The per-question numbers come from the OpenDecider model card (its authors measured Jev through its own API as well). The k = 3 column is our own arithmetic, not a measurement. Cost reported there for Jev is about $0.025 per 1,000 decisions; the local model has no per-call fee but needs hardware.

### 6.3 When the extra layer pays off

```
net effect per query = rejection_rate x generator_time  -  judge_time
```

- Accepted queries pay the judge time and nothing else.
- Rejected queries skip the generator call entirely. A generator call usually takes seconds, so even a modest rejection rate can offset the added hundreds of milliseconds.
- Beyond speed, the main benefit is quality: fewer answers built on irrelevant context.

Ways to reduce the cost: lower `QDRANT_TOP_K`, judge only the top few chunks, use the nano model, or run the judge on a GPU.

### 6.4 Measured on our setup (first run)

One run on a Windows PC, CPU only, with `opendecider-nano`, 3 retrieved passages (job postings of several hundred words each, mostly Persian) and an English query:

| Step | Time (ms) |
|---|---|
| embed | 191 |
| qdrant | 9 |
| judge (3 passages) | 48,068 |
| total | 48,267 |

The judge took about 16 seconds per passage. This is a single run, it may include warm-up, and the whole payload (text plus metadata fields) was passed as the passage, so treat it as a rough upper bound, not a benchmark. For comparison, the model card reports 38 ms per question for the small model on a GPU (section 6.2). Still to do: repeat the measurement (`--repeat 10`) with `QDRANT_TEXT_FIELDS=text` to halve the input length, and on a GPU if one is available.

Conclusion so far: on CPU the local judge is too slow for interactive use. It needs a GPU or the API backend.

## 7. Risks and recommendations

1. **Threshold tuning.** 0.60 is a requirement, not a proven optimum. Build a small labeled set (question, chunk, answers: yes/no) and check precision and recall at 0.60 before production.
2. **Prompt injection through chunks.** Retrieved text is untrusted input. A third-party review of Jev reports that injected text can measurably shift its probabilities. Do not use the gate as the only protection for high-stakes decisions.
3. **Language coverage.** See section 5: the local model is English-only so far, and no multilingual claims were verified for either model.
4. **Vendor risk.** Jev is in early access and the API has had capacity problems after launch. The local backend is the fallback.
5. **Confident but wrong.** A high probability does not guarantee a correct judgment. Monitor rejected queries to catch false rejections.

## 8. Quick test checklist

- [ ] `.env` created from `.env.example`, and not committed
- [ ] Qdrant reachable, collection name and text field correct
- [ ] `EMBEDDING_MODEL` matches the indexing model
- [ ] One question that is answered in the documents returns passages with `p > 0.60`
- [ ] One unrelated question returns `Not Found in Documents`
- [x] Latency measured once on CPU (section 6.4); repeated and GPU measurements pending

## 9. First test results and open questions

**Setup.** Qdrant in local mode with 10 sample job postings (mostly Persian text with English metadata fields), EmbeddingGemma for retrieval, `opendecider-nano` as judge, threshold 0.60.

**Result.** For an English query asking for a Machine Learning Engineer role with CI/CD and LightGBM, Qdrant returned three AI-related postings with low similarity scores (0.008 to 0.018). The judge returned probabilities of 0.39, 0.23 and 0.38, all below 0.60, so the output was `Not Found in Documents`.

**What this shows.** The pipeline runs end to end, and rejecting these three postings looks reasonable, because none of them asks for an ML Engineer with those skills.

**What this does not show.**
- No positive test was run yet (a query that should pass), so the false-rejection rate is unknown.
- The sample has only 10 postings.
- Probabilities around 0.4 for unrelated postings mean the model is not very decisive on this data.

**Open question: Persian support.** The OpenDecider model card reports English-only evaluation, and Jev's performance on Persian was not verified. The postings are mostly long Persian texts, plus structured English metadata (`role_normalized`, `skills_required`, `tools`). Options to evaluate in the next task:

1. Build a labeled set of query/posting pairs in Persian and English (for example 50 to 100) and compare Jev API, OpenDecider-small and OpenDecider-nano on it.
2. Translate queries and passages to English before judging, and measure the cost in latency and accuracy.
3. Judge the structured English metadata fields instead of the long Persian text. This would also shorten the input.
4. Try a multilingual judge or reranker as an alternative backend.
5. Change where the gate sits in the pipeline (for example, judge only the top passage).

These are hypotheses to test, not conclusions.
