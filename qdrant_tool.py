import json
import uuid
import hashlib
import sys
from pathlib import Path
from typing import List, Optional, Dict, Any
from qdrant_client import QdrantClient, models
from qdrant_client.http.models import PointStruct

class MagentoSearchTool:
    def __init__(self, project_root: str, collection_name: str = "magento_index"):
        # Ensure we don't use '--index' as a path
        clean_root = project_root if project_root != "--index" else "."
        self.root = Path(clean_root).resolve()
        
        # Path to the DB folder
        db_path = self.root / ".qdrant_db"
        self.client = QdrantClient(path=str(db_path))
        self.collection_name = collection_name
        
        # 1. ALWAYS set the model first so the client knows the schema
        self.model_name = "sentence-transformers/all-MiniLM-L6-v2"
        self.client.set_model(self.model_name)
        
        self._init_collection()

    def _init_collection(self):
        """Initializes the Qdrant collection with the correct vector name."""
        collections = self.client.get_collections().collections
        exists = any(c.name == self.collection_name for c in collections)
        
        if not exists:
            # get_fastembed_vector_params uses the model set in __init__
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=self.client.get_fastembed_vector_params(),
            )
            print(f"Collection '{self.collection_name}' created using {self.model_name}")

    def _generate_id(self, content: str) -> str:
        return str(uuid.UUID(hashlib.md5(content.encode()).hexdigest()))

    def index_from_cache(self, cache_file: str = ".indexer_cache.json"):
        cache_path = self.root / cache_file
        if not cache_path.exists():
            print(f"Cache file {cache_path} not found.")
            return


        with open(cache_path, "r") as f:
            data = json.load(f)

        points_metadata = []
        documents = []
        ids = []

        # Process References
        for ref in data.get("index", []):
            text = f"Magento Reference: {ref['kind']} for '{ref['value']}' in module '{ref['module']}' (Area: {ref['area']}). File: {ref['file']}:{ref['line']+1}"
            point_id = self._generate_id(f"ref-{ref['file']}-{ref['line']}-{ref['value']}")
            
            documents.append(text)
            ids.append(point_id)
            points_metadata.append({
                "type": "reference", "text": text, "kind": ref['kind'],
                "value": ref['value'], "module": ref['module'],
                "file": ref['file'], "line": ref['line'], "notes": []
            })

        # Process Templates
        for tpl in data.get("templates", []):
            # Safely get module and theme to avoid KeyErrors
            module_name = tpl.get('module', 'Unknown')
            theme_name = tpl.get('theme', 'No Theme')
            
            text = f"Magento Template: ID '{tpl['id']}' in theme/module '{theme_name or module_name}' (Area: {tpl.get('area', 'N/A')}). File: {tpl['file']}"
            point_id = self._generate_id(f"tpl-{tpl['file']}-{tpl['id']}")
            
            documents.append(text)
            ids.append(point_id)
            points_metadata.append({
                "type": "template", 
                "text": text, 
                "id": tpl['id'],
                "module": module_name, # Safe now
                "file": tpl['file'], 
                "notes": []
            })

        if documents:
            batch_size = 300  # Adjust based on your CPU/RAM
            total_items = len(documents)
            print(f"🚀 Starting indexing of {total_items} items in batches of {batch_size}...")

            for i in range(0, total_items, batch_size):
                # Slice the data for the current batch
                batch_docs = documents[i : i + batch_size]
                batch_meta = points_metadata[i : i + batch_size]
                batch_ids = ids[i : i + batch_size]

                # Log the current status
                current_batch_num = (i // batch_size) + 1
                total_batches = (total_items + batch_size - 1) // batch_size
                print(f"📦 Processing Batch {current_batch_num}/{total_batches} ({len(batch_docs)} items)...", end="\r")

                # Upload the batch
                self.client.add(
                    collection_name=self.collection_name,
                    documents=batch_docs,
                    metadata=batch_meta,
                    ids=batch_ids,
                    batch_size=batch_size # Internal FastEmbed batching
                )

            print(f"\n✅ Successfully indexed {total_items} items into Qdrant.")

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Searches the index using the actual vector name present in the collection."""
        
        # 1. Get the actual name Qdrant gave the vector field
        collection_info = self.client.get_collection(self.collection_name)
        # This grabs the name (e.g., 'all-MiniLM-L6-v2' or 'fast-all-MiniLM-L6-v2')
        vector_name = list(collection_info.config.params.vectors.keys())[0]

        results = self.client.query_points(
            collection_name=self.collection_name,
            query=models.Document(
                text=query,
                model=self.model_name
            ),
            using=vector_name, # Use the name discovered above
            limit=limit
        ).points
        
        return [{"id": r.id, "score": r.score, "payload": r.payload} for r in results]

if __name__ == "__main__":
    # Logic to separate the path argument from the --index flag
    args = [a for a in sys.argv[1:] if a != "--index"]
    root_path = args[0] if args else "."
    
    tool = MagentoSearchTool(root_path)
    
    if "--index" in sys.argv:
        tool.index_from_cache()
    
    while True:
        query = input("\nEnter search (or 'exit'): ").strip()
        if query.lower() == 'exit': break
        if not query: continue
        
        results = tool.search(query)
        for i, res in enumerate(results):
            p = res['payload']
            print(f"[{i}] {p['text']} (Score: {res['score']:.3f})")