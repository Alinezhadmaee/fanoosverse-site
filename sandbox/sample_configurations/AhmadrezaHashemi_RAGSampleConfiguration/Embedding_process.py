"""
Module: Embedding_process.py
Description: Iterates through raw JSONL job data, validates it against the Pydantic schema,
applies the EmbeddingGemma 2 prefix, vectorizes the natural language text, and appends
the structured output (UUID, Vector, Payload) to a final JSONL file for Qdrant ingestion.
"""

import json
import uuid
import os
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer
from Pydantic_schema_verified import VectorStorePayload

# Load environment variables (Model path/name should be in .env)
load_dotenv()
EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME", "google/gemma-2-embedding-placeholder")

# Input and Output file paths
INPUT_FILE = "sample_raw_data.jsonl"
OUTPUT_FILE = "Vectorized_raw_data.jsonl"

def initialize_model():
    """Loads the sentence transformer model into memory."""
    print(f"Loading embedding model: {EMBEDDING_MODEL_NAME}...")
    # NOTE: Ensure you have accepted Gemma terms on HuggingFace and logged in if using gated models
    return SentenceTransformer(EMBEDDING_MODEL_NAME)

def process_and_vectorize():
    """Reads raw data, vectorizes the text field, and appends to the output file."""
    model = initialize_model()
    
    # Ensure output file is fresh if running the script from scratch
    if os.path.exists(OUTPUT_FILE):
        os.remove(OUTPUT_FILE)
        print(f"Removed old {OUTPUT_FILE}")

    print(f"Starting line-by-line processing of {INPUT_FILE}...")
    
    # Open input for reading, output for appending ('a' mode)
    with open(INPUT_FILE, 'r', encoding='utf-8') as infile, \
         open(OUTPUT_FILE, 'a', encoding='utf-8') as outfile:
        
        for line_num, line in enumerate(infile, 1):
            line = line.strip()
            if not line:
                continue
                
            try:
                # 1. Load raw JSON
                raw_dict = json.loads(line)
                
                # 2. Validate using Pydantic Schema
                validated_data = VectorStorePayload.model_validate(raw_dict)
                payload_dict = validated_data.model_dump(exclude_none=True, by_alias=True)
                
                # 3. Extract necessary fields for vectorization
                role_title = payload_dict.get("role_normalized", "Unknown Role")
                raw_text = payload_dict.get("text", "")
                
                # 4. Apply Gemma 2 Indexing Prefix (CRITICAL STEP)
                gemma_formatted_text = f"title: {role_title} | text: {raw_text}"
                
                # 5. Vectorize (Returns a normalized 768-dimensional float array)
                vector = model.encode(gemma_formatted_text, normalize_embeddings=True).tolist()
                
                # 6. Generate UUID
                point_id = str(uuid.uuid4())
                
                # 7. Construct final object for Qdrant
                qdrant_point = {
                    "point_id": point_id,
                    "vector": vector,
                    "payload": payload_dict # The original validated data acts as our payload
                }
                
                # 8. Append to output file instantly to save memory
                outfile.write(json.dumps(qdrant_point, ensure_ascii=False) + '\n')
                
                print(f"Processed line {line_num}: UUID generated -> {point_id}")
                
            except Exception as e:
                print(f"Error processing line {line_num}: {e}")

if __name__ == "__main__":
    process_and_vectorize()
    print(f"Vectorization complete. Output saved to {OUTPUT_FILE}")