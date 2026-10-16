"""
ingest_qdrant.py

Reads Vectorized_raw_data.jsonl (keys: point_id, vector, payload), builds a
Qdrant collection (size 768, cosine), creates payload indexes for the filter
fields and upserts the records in batches of 50.

By default the document vectors are re-computed from payload["text"] with
EmbeddingGemma, because the vectors shipped in the file are NOT compatible
with google/embeddinggemma-300m (cosine ~0 against its own embeddings), so
semantic search with this model would be meaningless on them.
Use --use-file-vectors to ingest the original vectors untouched.

Usage:
    python ingest_qdrant.py
    python ingest_qdrant.py --use-file-vectors
"""

import argparse
import json
import sys

from qdrant_client import QdrantClient, models

MODEL_NAME = "google/embeddinggemma-300m"
VECTOR_SIZE = 768
BATCH_SIZE = 50
DOC_PREFIX = "title: none | text: "  # EmbeddingGemma document prefix

INDEXES = {
    "salary_min": models.PayloadSchemaType.INTEGER,
    "salary_max": models.PayloadSchemaType.INTEGER,
    "seniority": models.PayloadSchemaType.KEYWORD,
    "work_mode": models.PayloadSchemaType.KEYWORD,
    "employment_type": models.PayloadSchemaType.KEYWORD,
    "location": models.PayloadSchemaType.KEYWORD,
}


def read_jsonl(path: str) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            for key in ("point_id", "vector", "payload"):
                if key not in rec:
                    raise ValueError(f"line {n}: missing key '{key}'")
            if len(rec["vector"]) != VECTOR_SIZE:
                raise ValueError(
                    f"line {n}: vector size {len(rec['vector'])} != {VECTOR_SIZE}"
                )
            records.append(rec)
    return records


def embed_documents(records: list[dict]) -> list[list[float]]:
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(MODEL_NAME)
    texts = [str(r["payload"].get("text", "")) for r in records]
    if hasattr(model, "encode_document"):
        vecs = model.encode_document(texts, normalize_embeddings=True)
    else:
        vecs = model.encode([DOC_PREFIX + t for t in texts], normalize_embeddings=True)
    return [v.tolist() for v in vecs]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default="Vectorized_raw_data.jsonl")
    parser.add_argument("--url", default="http://localhost:6333")
    parser.add_argument("--collection", default="jobs")
    parser.add_argument("--use-file-vectors", action="store_true")
    args = parser.parse_args()

    records = read_jsonl(args.file)
    print(f"Read {len(records)} records from {args.file}")

    if args.use_file_vectors:
        vectors = [r["vector"] for r in records]
        print("Using the vectors from the file as-is")
    else:
        vectors = embed_documents(records)
        print("Re-embedded payload['text'] with", MODEL_NAME)

    client = QdrantClient(url=args.url)
    if client.collection_exists(args.collection):
        client.delete_collection(args.collection)
    client.create_collection(
        collection_name=args.collection,
        vectors_config=models.VectorParams(
            size=VECTOR_SIZE, distance=models.Distance.COSINE
        ),
    )
    for field, schema in INDEXES.items():
        client.create_payload_index(args.collection, field_name=field, field_schema=schema)

    points = [
        models.PointStruct(id=r["point_id"], vector=v, payload=r["payload"])
        for r, v in zip(records, vectors)
    ]
    for i in range(0, len(points), BATCH_SIZE):
        client.upsert(args.collection, points=points[i : i + BATCH_SIZE])
        print(f"  upserted batch {i // BATCH_SIZE + 1} ({len(points[i:i + BATCH_SIZE])} points)")

    stored = client.count(args.collection, exact=True).count
    print(f"Sent: {len(points)} | Stored: {stored}")
    return 0 if stored == len(points) else 1


if __name__ == "__main__":
    sys.exit(main())
