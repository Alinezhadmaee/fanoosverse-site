"""
Module: Qdrant_configuration.py
Description: Configures the Qdrant vector database, forces HNSW graph generation on small datasets,
batch-ingests vectorized JSONL data, and performs a sample vector search using Gemma query prefixes.
"""

import json
import os
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, HnswConfigDiff, PointStruct, SearchParams

# Load environment variables
load_dotenv()
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", 6333))
COLLECTION_NAME = "lantern_sample_collection"
VECTORIZED_FILE = "Vectorized_raw_data.jsonl"
EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME", "google/gemma-2-embedding-placeholder")

def setup_and_ingest():
    """Initializes Qdrant, configures HNSW parameters, and ingests data."""
    # Initialize client
    # With this (creates a local folder instead of needing a running server):
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    
    # Check if collection exists and recreate it for testing
    if client.collection_exists(collection_name=COLLECTION_NAME):
        client.delete_collection(collection_name=COLLECTION_NAME)
        print(f"Deleted existing collection: {COLLECTION_NAME}")

    print("Creating new collection with HNSW parameters...")
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=768, distance=Distance.COSINE),
        # Set full_scan_threshold to the minimum allowed value (10 KB) to trigger HNSW early
        hnsw_config=HnswConfigDiff(
            m=16,                           # Number of edges per node
            ef_construct=100,               # Effort spent during index building
            full_scan_threshold=10          # Minimum valid threshold in modern Qdrant (>= 10)
        )
    )
    
    # Optional but highly recommended: Create payload index for hard filters (e.g., location)
    client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="location",
        field_schema="keyword"
    )

    # Ingestion process
    print(f"Starting batch ingestion from {VECTORIZED_FILE}...")
    points_to_upsert = []
    
    with open(VECTORIZED_FILE, 'r', encoding='utf-8') as infile:
        for line in infile:
            if not line.strip():
                continue
                
            data = json.loads(line)
            point = PointStruct(
                id=data["point_id"],
                vector=data["vector"],
                payload=data["payload"]
            )
            points_to_upsert.append(point)
            
            # Batch size of 100 for memory efficiency
            if len(points_to_upsert) >= 100:
                client.upsert(collection_name=COLLECTION_NAME, points=points_to_upsert)
                points_to_upsert = []
                
    # Upsert any remaining points
    if points_to_upsert:
        client.upsert(collection_name=COLLECTION_NAME, points=points_to_upsert)
        
    print(f"Ingestion complete. Total points: {client.get_collection(COLLECTION_NAME).points_count}")
    return client

def run_retrieval_test(client: QdrantClient):
    """Embeds a sample query and tests the HNSW retrieval logic using the modern query_points API."""
    print("\nLoading model for query embedding...")
    model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    
    # 1. Define the sample user query
    user_query = "Looking for a Machine Learning Engineer role in Tehran with experience in CI/CD and LightGBM."
    print(f"User Query: '{user_query}'")
    
    # 2. Apply Gemma 2 Retrieval Prefix
    gemma_query = f"task: search result | query: {user_query}"
    
    # 3. Vectorize the query
    query_vector = model.encode(gemma_query, normalize_embeddings=True).tolist()
    
    # 4. Search Qdrant using the modern query_points API (qdrant-client >= 1.16)
    print("Executing HNSW-powered vector search...")
    search_response = client.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        limit=3,
        # Define search effort (hnsw_ef) - higher means more accurate but slower
        search_params=SearchParams(
            hnsw_ef=128
        )
    )
    
    # Extract the list of ScoredPoint objects from the QueryResponse
    search_results = search_response.points
    
    # 5. Display Results
    print("\n--- TOP 3 RAG RESULTS ---")
    for hit in search_results:
        print(f"Score: {hit.score:.4f} | ID: {hit.id}")
        print(f"Role: {hit.payload.get('role_normalized')}")
        print(f"Location: {hit.payload.get('location')}")
        print("-" * 30)

if __name__ == "__main__":
    qdrant_client = setup_and_ingest()
    try:
        run_retrieval_test(qdrant_client)
    finally:
        # Explicitly close the client before Python shuts down to avoid the __del__ ImportError
        qdrant_client.close()