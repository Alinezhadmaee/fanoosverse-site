# Creative Pipelines with System One Models in Project Fanos

This is a brainstorming document. It lists ideas for using System One decision models (TypeSafe Jev, or the open-source OpenDecider alternative) beyond the RAG gatekeeper. Each idea is a proposal to discuss, not a tested result.

## 1. What these models are good at

System One models return typed, calibrated answers instead of free text. Three question types matter:

| Type | Returns | Typical use |
|---|---|---|
| `choice` | one option from a list you define, plus a probability per option | classification, routing, labeling |
| `score` | a position on an ordered scale you define | ranking, grading |
| `noul` | probability that a statement is true | yes/no checks, gating |

Several independent questions can be asked about the same `state` in one request. Our own code then decides what happens next: continue, collect more information, or hand off to a person.

Strengths: structured output that code can use directly, a confidence value for every decision, low latency compared with a generative model.

Limits to keep in mind: no written explanation for a decision, a valid answer is not always a correct answer, retrieved or user-supplied text can try to steer the probabilities (prompt injection), and the open-source model is English-only so far.

## 2. Idea list

### 2.1 Intent routing in front of the RAG pipeline
- **Question:** `choice` over intents such as `document_question`, `job_search`, `resume_help`, `chitchat`, `out_of_scope`.
- **Use:** send each message to the right path, and skip retrieval entirely for chitchat or out-of-scope messages.
- **Confidence rule:** below a set confidence, ask the user a clarifying question instead of guessing.
- **Value:** saves vector-database and generator calls; keeps answers on topic.

### 2.2 Automatic resume evaluation
- **Questions:** `score` for fit on several criteria (relevant experience, skills match, seniority), plus `noul` for hard requirements ("states a degree in the required field").
- **Use:** produce a ranked shortlist and highlight which criteria drove the ranking.
- **Safety:** this affects people, so keep a human decision-maker, audit for bias, and never auto-reject on the model's output alone. Check legal requirements for automated hiring decisions.

### 2.3 Validation of raw metadata
- **Questions:** `noul` such as "is this job title a real role title rather than a slogan?", "does this posting state a location?", "is the salary field a plausible number?", plus `choice` to normalize free-text values to a fixed vocabulary.
- **Use:** flag dirty or suspicious records before they enter the dataset; route low-confidence records to manual review.
- **Value:** cleaner data going into the vector database, which improves retrieval for everyone downstream.

### 2.4 Query routing across collections
- **Question:** `choice` over the available Qdrant collections or document groups.
- **Use:** search only the collection most likely to contain the answer, instead of all of them.
- **Variant:** when two options are close in probability, search both.

### 2.5 Post-generation grounding check
- **Question:** `noul` on the pair (generated answer, source passages): "is every claim in the answer supported by the passages?"
- **Use:** a second gate after the generator, which catches hallucinations that slipped through.
- **Caveat:** long answers should be split into single claims first, so the check stays precise.

### 2.6 Job posting triage and tagging
- **Questions:** `choice` for category or team, `noul` for flags such as "remote allowed" or "requires a security clearance", `score` for seniority level.
- **Use:** consistent tags on every record without hand labeling, which also feeds filtered search.

### 2.7 Language and translation gate
- **Question:** `choice` over languages, then `noul` "does this text need translation before processing?"
- **Use:** because the open-source model is English-only so far, detect the language first and either translate or route to the API backend. This is a practical workaround for the current coverage gap.

### 2.8 Duplicate and near-duplicate detection
- **Question:** `noul` on a pair of records: "do these two postings describe the same job?"
- **Use:** run only on pairs that vector search already flagged as close, so the number of model calls stays small.

### 2.9 Human escalation by confidence band
- **Rule:** any pipeline above can define three bands: high confidence acts automatically, middle confidence asks for more information, low confidence goes to a person.
- **Value:** makes calibration useful, because the bands mean what they say only if the probabilities are well calibrated, which should be verified on our own data.

## 3. Prioritization

| Idea | Value | Effort | Risk | Suggested order |
|---|---|---|---|---|
| 2.1 Intent routing | High | Low | Low | 1 |
| 2.3 Metadata validation | High | Low | Low | 2 |
| 2.5 Grounding check | High | Medium | Medium | 3 |
| 2.6 Posting tagging | Medium | Low | Low | 4 |
| 2.7 Language gate | Medium | Low | Low | 5 |
| 2.4 Collection routing | Medium | Medium | Low | 6 |
| 2.8 Duplicate detection | Medium | Medium | Low | 7 |
| 2.2 Resume evaluation | High | High | High (people impact) | Only with human review |

## 4. Shared building blocks

All ideas can reuse the structure of `jev_rag_filter.py`:

- one judge interface with an API backend and a local backend
- prompts stored in separate files, never inline
- thresholds read from configuration, never hardcoded
- logged timings and probabilities, so decisions can be audited and thresholds tuned

## 5. Before building any of these

1. Collect a small labeled sample for the task (a few dozen examples is enough to start).
2. Measure accuracy and calibration on that sample, in the language the data is actually written in.
3. Choose the threshold from that data, not from a default.
4. Decide what happens in the low-confidence band before launch.
