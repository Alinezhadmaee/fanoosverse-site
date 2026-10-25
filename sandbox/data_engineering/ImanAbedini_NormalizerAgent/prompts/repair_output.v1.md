# System prompt — repair_output.v1

Your previous answer for this normalization task was rejected by validation.

The validation error was:

### VALIDATION ERROR
{validation_error}

### YOUR PREVIOUS ANSWER
{previous_answer}

Answer again with STRICT JSON only (no prose, no markdown fences), exactly in
this shape:

{"items": [{"field": "<field>", "index": <int>, "canonical": ["<value>"] | null, "category": null, "confidence": "high" | "low", "reason": "<short>"}]}

Fix the reported problem:
- unknown/malformed keys or missing entries -> return one entry per input item, same order;
- wrong value shape -> `canonical` must be a one-element list or null;
- values not allowed by the vocabulary/enum -> use null (never guess);
- placeholders ("unknown", "N/A", "-") are never valid values -> use null;
- keep the unchanged entries identical to your previous answer.
