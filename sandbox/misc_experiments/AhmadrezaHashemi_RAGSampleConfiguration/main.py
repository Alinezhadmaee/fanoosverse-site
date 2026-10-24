"""
Module: main.py
Description: The central orchestrator for the Lantern RAG sample pipeline.
It sequentially executes data validation, vectorization, Qdrant ingestion, 
and tests the HNSW retrieval logic.
"""

import time
import os

# Import the core functions from our modules
from Embedding_process import process_and_vectorize
from Qdrant_configuration import setup_and_ingest, run_retrieval_test
from dotenv import load_dotenv
load_dotenv(dotenv_path=".env")
HF_TOKEN = os.environ.get("HF_TOKEN")
def run_pipeline():
    """Executes the complete data processing and retrieval pipeline."""
    print("STARTING CORE PIPELINE")
    
    start_time = time.time()

    # STEP 1: Vectorize Raw Data
    # This reads sample_raw_data.jsonl, filters it via Pydantic, 
    # vectorizes the text via Gemma, and outputs Vectorized_raw_data.jsonl
    print("\n[STEP 1] Initializing Vectorization Process...")
    try:
        process_and_vectorize()
        print("Step 1 Completed Successfully.")
    except Exception as e:
        print(f"Pipeline failed during Vectorization: {e}")
        return

    # STEP 2: Ingest into Qdrant
    # This reads Vectorized_raw_data.jsonl, configures Qdrant with HNSW,
    # and batch-upserts the vectors and payloads.
    print("\n[STEP 2] Initializing Qdrant Ingestion...")
    try:
        qdrant_client = setup_and_ingest()
        print("Step 2 Completed Successfully.")
    except Exception as e:
        print(f"Pipeline failed during Qdrant Ingestion: {e}")
        return

    # STEP 3: Test Retrieval
    # This takes a sample query, embeds it, and runs an HNSW-forced search
    print("\n[STEP 3] Running Retrieval Test...")
    try:
        run_retrieval_test(qdrant_client)
        print("\nStep 3 Completed Successfully.")
    except Exception as e:
        print(f"Pipeline failed during Retrieval Test: {e}")
        return

    # Completion
    elapsed_time = time.time() - start_time
    print("="*50)
    print(f"PIPELINE COMPLETED IN {elapsed_time:.2f} SECONDS")
    print("="*50)

if __name__ == "__main__":
    # Sanity check to ensure the raw data file exists before starting
    if not os.path.exists("sample_raw_data.jsonl"):
        print("Error: 'sample_raw_data.jsonl' not found in the root directory.")
        print("Please ensure the raw data file is present before running the pipeline.")
    else:
        run_pipeline()