#!/usr/bin/env python3
"""Vectorize job-ad records with EmbeddingGemma and write a Qdrant-ready JSONL file.

Rules enforced by this script:
  * Only the "text" field (plus "role_title" inside the mandatory prefix) is sent to the model.
  * Every other field is copied untouched into the payload.
  * Each output line has the exact shape: {"point_id": <UUID>, "vector": [...], "payload": {...}}.

Usage:
  python gemma_vectorizer.py --input main-processed-v3.jsonl --output vectors.jsonl
  python gemma_vectorizer.py --input main-processed-v3.jsonl --output vectors.jsonl --dry-run
  python gemma_vectorizer.py --validate vectors.jsonl --input main-processed-v3.jsonl
  python gemma_vectorizer.py --renormalize vectors.jsonl --output vectors_fixed.jsonl
"""

import argparse
import hashlib
import json
import logging
import math
import sys
import uuid
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

LOG = logging.getLogger("gemma_vectorizer")

DEFAULT_MODEL = "google/embeddinggemma-2"
DEFAULT_DIM = 768
DEFAULT_BATCH_SIZE = 16
DEFAULT_TEXT_KEY = "text"
DEFAULT_TITLE_KEY = "role_title"
DEFAULT_ID_KEY = "document_id"
EMPTY_TITLE_FALLBACK = "none"
PREFIX_TEMPLATE = "title: {title} | text: {text}"
UUID_NAMESPACE = uuid.UUID("6f1b6e0e-5d57-4b0e-9a57-5c2f6a9e1f10")


def read_jsonl(path: Path) -> Iterator[Tuple[int, Dict]]:
    """Yield (line_number, record) for every non-empty line of a JSONL file."""
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON ({exc})") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{line_no}: expected a JSON object")
            yield line_no, record


def make_point_id(record: Dict, id_key: str, mode: str, line_no: int) -> str:
    """Return a UUID string. uuid5 is deterministic per document id; uuid4 is random."""
    if mode == "uuid4":
        return str(uuid.uuid4())
    basis = record.get(id_key)
    if basis is None or str(basis).strip() == "":
        basis = f"line-{line_no}"
    return str(uuid.uuid5(UUID_NAMESPACE, str(basis)))


def build_document(record: Dict, text_key: str, title_key: str) -> str:
    """Build the prefixed string passed to the model. Only title and text are used."""
    text = record.get(text_key)
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"record has no usable '{text_key}' field")
    title = record.get(title_key)
    if not isinstance(title, str) or not title.strip():
        title = EMPTY_TITLE_FALLBACK
    return PREFIX_TEMPLATE.format(title=title.strip(), text=text)


def build_payload(record: Dict, text_key: str, drop_text: bool) -> Dict:
    """Copy every field untouched. Optionally drop the text field to save space."""
    payload = dict(record)
    if drop_text:
        payload.pop(text_key, None)
    return payload


def l2_normalize(vector: List[float]) -> List[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return vector
    return [x / norm for x in vector]


def fake_embed(documents: List[str], dim: int) -> List[List[float]]:
    """Deterministic pseudo vectors for pipeline testing only. NOT real embeddings."""
    vectors = []
    for doc in documents:
        values: List[float] = []
        counter = 0
        while len(values) < dim:
            digest = hashlib.sha256(f"{counter}:{doc}".encode("utf-8")).digest()
            values.extend((b - 127.5) / 127.5 for b in digest)
            counter += 1
        vectors.append(l2_normalize(values[:dim]))
    return vectors


class RealEmbedder:
    """Wraps sentence-transformers so the model is only imported when needed."""

    def __init__(self, model_name: str, device: Optional[str], batch_size: int, dim: int, text_only: bool = False):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise SystemExit(
                "sentence-transformers is not installed. Run: pip install -r requirements.txt"
            ) from exc
        LOG.info("Loading model %s", model_name)
        load_kwargs = {}
        if text_only:
            load_kwargs["config_kwargs"] = {"vision_config": None, "audio_config": None}
        self.model = SentenceTransformer(model_name, device=device, **load_kwargs)
        self.batch_size = batch_size
        self.dim = dim
        native_dim = self.model.get_sentence_embedding_dimension()
        if native_dim < dim:
            raise SystemExit(f"Model dimension {native_dim} is smaller than requested {dim}")
        if native_dim != dim:
            LOG.warning("Model dimension %d differs from requested %d; truncating and renormalizing", native_dim, dim)
        self.truncated_count = 0
        self.token_check_failed = False

    def count_truncated(self, documents: List[str]) -> int:
        """Count documents longer than the model limit. Returns 0 if the tokenizer is unavailable."""
        if self.token_check_failed:
            return 0
        try:
            limit = self.model.max_seq_length
            tokenizer = getattr(self.model, "tokenizer", None)
            if tokenizer is None or limit is None:
                raise AttributeError("tokenizer or max_seq_length unavailable")
            over = 0
            for doc in documents:
                if len(tokenizer(doc)["input_ids"]) > limit:
                    over += 1
            return over
        except Exception as exc:
            self.token_check_failed = True
            LOG.warning("Token length check disabled: %s", exc)
            return 0

    def embed(self, documents: List[str]) -> List[List[float]]:
        self.truncated_count += self.count_truncated(documents)
        matrix = self.model.encode(
            documents,
            prompt="",
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        vectors = []
        for row in matrix:
            values = [float(x) for x in row[: self.dim]]
            vectors.append(l2_normalize(values))
        return vectors


def renormalize_file(source: Path, target: Path) -> int:
    """Rewrite a JSONL file with every vector L2-normalized in float64. Payload and ids stay untouched."""
    if not source.is_file():
        raise SystemExit(f"File not found: {source}")
    if source.resolve() == target.resolve():
        raise SystemExit("--output must differ from the file being renormalized")
    target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with target.open("w", encoding="utf-8") as out_handle:
        for _, line in read_jsonl(source):
            line["vector"] = l2_normalize([float(x) for x in line["vector"]])
            out_handle.write(json.dumps(line, ensure_ascii=False) + "\n")
            count += 1
    LOG.info("Renormalized %d lines into %s", count, target)
    return 0


def run_pipeline(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    output_path = Path(args.output)
    if not input_path.is_file():
        raise SystemExit(f"Input file not found: {input_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    embedder = None
    if not args.dry_run:
        embedder = RealEmbedder(args.model, args.device, args.batch_size, args.dim, args.text_only)
    else:
        LOG.warning("DRY RUN: vectors are deterministic fakes, not model output")

    written = 0
    skipped: List[str] = []
    seen_ids = set()
    batch: List[Tuple[int, Dict, str]] = []

    def flush(out_handle) -> int:
        if not batch:
            return 0
        docs = [item[2] for item in batch]
        vectors = fake_embed(docs, args.dim) if args.dry_run else embedder.embed(docs)
        for (line_no, record, _), vector in zip(batch, vectors):
            if len(vector) != args.dim:
                raise RuntimeError(f"line {line_no}: vector has {len(vector)} dims, expected {args.dim}")
            point_id = make_point_id(record, args.id_key, args.uuid_mode, line_no)
            if point_id in seen_ids:
                raise RuntimeError(f"line {line_no}: duplicate point_id {point_id}")
            seen_ids.add(point_id)
            line = {
                "point_id": point_id,
                "vector": vector,
                "payload": build_payload(record, args.text_key, args.drop_text_from_payload),
            }
            out_handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        count = len(batch)
        batch.clear()
        return count

    with output_path.open("w", encoding="utf-8") as out_handle:
        for line_no, record in read_jsonl(input_path):
            try:
                document = build_document(record, args.text_key, args.title_key)
            except ValueError as exc:
                skipped.append(f"line {line_no}: {exc}")
                continue
            batch.append((line_no, record, document))
            if len(batch) >= args.batch_size:
                written += flush(out_handle)
                LOG.info("Processed %d records", written)
        written += flush(out_handle)

    LOG.info("Done. Written: %d, skipped: %d, output: %s", written, len(skipped), output_path)
    for reason in skipped:
        LOG.warning("Skipped %s", reason)
    if embedder is not None and embedder.truncated_count:
        LOG.warning(
            "%d documents exceeded the model max sequence length and were truncated by the model",
            embedder.truncated_count,
        )
    return 0


def validate_output(args: argparse.Namespace) -> int:
    """Check the final JSONL against the contract. Returns a non-zero exit code on failure."""
    path = Path(args.validate)
    if not path.is_file():
        raise SystemExit(f"File not found: {path}")

    source: Dict[str, Dict] = {}
    if args.input:
        for _, record in read_jsonl(Path(args.input)):
            source[str(record.get(args.id_key))] = record

    errors: List[str] = []
    ids = set()
    doc_ids = set()
    count = 0
    for line_no, line in read_jsonl(path):
        count += 1
        if set(line.keys()) != {"point_id", "vector", "payload"}:
            errors.append(f"line {line_no}: keys are {sorted(line.keys())}")
            continue
        try:
            uuid.UUID(line["point_id"])
        except (ValueError, AttributeError, TypeError):
            errors.append(f"line {line_no}: point_id is not a valid UUID")
        if line["point_id"] in ids:
            errors.append(f"line {line_no}: duplicate point_id")
        ids.add(line["point_id"])

        vector = line["vector"]
        if not isinstance(vector, list) or len(vector) != args.dim:
            errors.append(f"line {line_no}: vector length is {len(vector) if isinstance(vector, list) else 'n/a'}")
        elif not all(isinstance(x, (int, float)) and math.isfinite(x) for x in vector):
            errors.append(f"line {line_no}: vector has non-finite or non-numeric values")
        else:
            norm = math.sqrt(sum(x * x for x in vector))
            if abs(norm - 1.0) > 1e-3:
                errors.append(f"line {line_no}: vector norm is {norm:.5f}, expected 1.0")

        payload = line["payload"]
        if not isinstance(payload, dict):
            errors.append(f"line {line_no}: payload is not an object")
            continue
        doc_id = str(payload.get(args.id_key))
        doc_ids.add(doc_id)
        if source:
            original = source.get(doc_id)
            if original is None:
                errors.append(f"line {line_no}: {doc_id} not found in input")
            else:
                expected = build_payload(original, args.text_key, args.drop_text_from_payload)
                if payload != expected:
                    errors.append(f"line {line_no}: payload differs from input for {doc_id}")

    if source:
        missing = sorted(set(source) - doc_ids)
        if missing:
            LOG.warning("%d input records not present in output: %s", len(missing), missing[:10])

    LOG.info("Validated %d lines, %d errors", count, len(errors))
    for err in errors[:50]:
        LOG.error(err)
    return 1 if errors else 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", help="Input JSONL with text, role_title and metadata fields")
    parser.add_argument("--output", help="Output JSONL path")
    parser.add_argument("--validate", metavar="JSONL", help="Validate an existing output file and exit")
    parser.add_argument("--renormalize", metavar="JSONL", help="Fix vector norms of an existing output file (needs --output)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Hugging Face model id or local path")
    parser.add_argument("--dim", type=int, default=DEFAULT_DIM, help="Vector dimension")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--device", default=None, help="cpu, cuda or mps (auto when omitted)")
    parser.add_argument("--text-key", default=DEFAULT_TEXT_KEY)
    parser.add_argument("--title-key", default=DEFAULT_TITLE_KEY)
    parser.add_argument("--id-key", default=DEFAULT_ID_KEY, help="Field used to derive the UUID")
    parser.add_argument("--uuid-mode", choices=["uuid5", "uuid4"], default="uuid5")
    parser.add_argument("--drop-text-from-payload", action="store_true", help="Do not store the text field in payload")
    parser.add_argument("--text-only", action="store_true", help="Load only the text part of the multimodal model")
    parser.add_argument("--dry-run", action="store_true", help="Use fake deterministic vectors, no model needed")
    args = parser.parse_args(argv)
    if args.validate:
        return args
    if args.renormalize:
        if not args.output:
            parser.error("--renormalize requires --output")
        return args
    if not args.input or not args.output:
        parser.error("--input and --output are required unless --validate is used")
    return args


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    if args.validate:
        return validate_output(args)
    if args.renormalize:
        return renormalize_file(Path(args.renormalize), Path(args.output))
    return run_pipeline(args)


if __name__ == "__main__":
    sys.exit(main())
