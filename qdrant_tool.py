import json
import uuid
import hashlib
import sys
from pathlib import Path
from typing import List, Optional, Dict, Any
from qdrant_client import QdrantClient, models

class MagentoSearchTool:
    def __init__(self, project_root: str, collection_name: str = "magento_index"):
        clean_root = project_root if project_root != "--index" else "."
        self.root = Path(clean_root).resolve()
        self.cache_dir = self.root / ".code_graph" / "cache"

        db_path = self.root / ".qdrant_db"
        self.client = QdrantClient(url='http://localhost:6333')
        self.collection_name = collection_name

        self.model_name = "sentence-transformers/all-MiniLM-L6-v2"
        self.client.set_model(self.model_name)

        self._init_collection()

    def _init_collection(self):
        collections = self.client.get_collections().collections
        exists = any(c.name == self.collection_name for c in collections)

        if not exists:
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=self.client.get_fastembed_vector_params(),
            )
            print(f"Collection '{self.collection_name}' created using {self.model_name}")

        # Create payload indexes for filtered search performance
        for field in ("type", "source"):
            self.client.create_payload_index(
                collection_name=self.collection_name,
                field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )

        # Cache the vector field name from the collection config
        collection_info = self.client.get_collection(self.collection_name)
        self.vector_name = list(collection_info.config.params.vectors.keys())[0]

    def _detect_source(self, file_path: str) -> str:
        """Returns 'app' if the file is under app/code, otherwise 'vendor'."""
        normalized = file_path.replace("\\", "/")
        if "/app/code/" in normalized or normalized.startswith("app/code/"):
            return "app"
        return "vendor"

    def _generate_id(self, content: str) -> str:
        return str(uuid.UUID(hashlib.md5(content.encode()).hexdigest()))

    def _load_json(self, path: Path):
        with open(path, "r") as f:
            return json.load(f)

    def index_from_cache(self):
        """Indexes all split cache JSON files into Qdrant."""
        if not self.cache_dir.exists():
            print(f"Cache directory {self.cache_dir} not found.")
            return

        documents = []
        metadata = []
        ids = []

        self._index_references(documents, metadata, ids)
        self._index_templates(documents, metadata, ids)
        self._index_modules(documents, metadata, ids)
        self._index_themes(documents, metadata, ids)
        self._index_classes(documents, metadata, ids)

        if documents:
            self._upload_batched(documents, metadata, ids)

    def _index_references(self, documents, metadata, ids):
        """Indexes XML references from index/*.json files."""
        index_dir = self.cache_dir / "index"
        if not index_dir.exists():
            return

        kinds = self._load_json(index_dir / "_kinds.json")
        for kind in kinds:
            refs = self._load_json(index_dir / f"{kind}.json")
            for ref in refs:
                text = (f"Magento {kind}: '{ref['value']}' "
                        f"in module '{ref['module']}' (area: {ref['area']}). "
                        f"File: {ref['file']}:{ref['line']+1}")
                point_id = self._generate_id(f"ref-{ref['file']}-{ref['line']}-{ref['value']}")

                documents.append(text)
                ids.append(point_id)
                metadata.append({
                    "type": "reference", "kind": kind, "text": text,
                    "source": self._detect_source(ref['file']),
                    "value": ref['value'], "module": ref['module'],
                    "area": ref['area'], "file": ref['file'], "line": ref['line'],
                })

    def _index_templates(self, documents, metadata, ids):
        """Indexes template entries from templates.json."""
        tpl_path = self.cache_dir / "templates.json"
        if not tpl_path.exists():
            return

        for tpl in self._load_json(tpl_path):
            module_name = tpl.get('module', 'Unknown')
            theme_name = tpl.get('theme', '')
            text = (f"Magento template '{tpl['id']}' "
                    f"in {theme_name or module_name} (area: {tpl.get('area', 'N/A')}). "
                    f"File: {tpl['file']}")
            point_id = self._generate_id(f"tpl-{tpl['file']}-{tpl['id']}")

            documents.append(text)
            ids.append(point_id)
            metadata.append({
                "type": "template", "text": text, "id": tpl['id'],
                "source": self._detect_source(tpl['file']),
                "module": module_name, "theme": theme_name,
                "file": tpl['file'],
            })

    def _index_modules(self, documents, metadata, ids):
        """Indexes module entries from modules.json."""
        mod_path = self.cache_dir / "modules.json"
        if not mod_path.exists():
            return

        for mod in self._load_json(mod_path):
            text = f"Magento module '{mod['name']}' at {mod['path']} (load order: {mod['order']})"
            point_id = self._generate_id(f"mod-{mod['name']}")

            documents.append(text)
            ids.append(point_id)
            metadata.append({
                "type": "module", "text": text,
                "source": self._detect_source(str(mod['path'])),
                "name": mod['name'], "path": mod['path'], "order": mod['order'],
            })

    def _index_themes(self, documents, metadata, ids):
        """Indexes theme entries from themes.json."""
        theme_path = self.cache_dir / "themes.json"
        if not theme_path.exists():
            return

        for theme in self._load_json(theme_path):
            text = (f"Magento theme '{theme['code']}' (area: {theme['area']}, "
                    f"parent: {theme.get('parent_code') or 'none'}). Path: {theme['path']}")
            point_id = self._generate_id(f"theme-{theme['code']}")

            documents.append(text)
            ids.append(point_id)
            metadata.append({
                "type": "theme", "text": text,
                "source": self._detect_source(str(theme['path'])),
                "code": theme['code'], "area": theme['area'],
                "path": theme['path'], "parent_code": theme.get('parent_code', ''),
            })

    def _index_classes(self, documents, metadata, ids):
        """Indexes PHP classes and methods from classes/*.json files."""
        classes_dir = self.cache_dir / "classes"
        if not classes_dir.exists():
            return

        modules_list = self._load_json(classes_dir / "_modules.json")
        for mod in modules_list:
            safe_name = mod.replace('\\', '_').replace('/', '_')
            cls_file = classes_dir / f"{safe_name}.json"
            if not cls_file.exists():
                continue

            for cls in self._load_json(cls_file):
                # Index the class itself
                ifaces = ', '.join(cls.get('interfaces', []))
                parent = cls.get('parent', '')
                method_names = [m['name'] for m in cls.get('methods', [])]

                cls_text = (f"PHP class '{cls['fqcn']}' in module '{cls['module']}'"
                            f"{f' extends {parent}' if parent else ''}"
                            f"{f' implements {ifaces}' if ifaces else ''}. "
                            f"Methods: {', '.join(method_names) if method_names else 'none'}. "
                            f"File: {cls['file']}:{cls['line']+1}")
                cls_id = self._generate_id(f"cls-{cls['fqcn']}")

                documents.append(cls_text)
                ids.append(cls_id)
                metadata.append({
                    "type": "class", "text": cls_text,
                    "source": self._detect_source(cls['file']),
                    "fqcn": cls['fqcn'], "module": cls['module'],
                    "parent": parent, "interfaces": cls.get('interfaces', []),
                    "file": cls['file'], "line": cls['line'],
                    "method_count": len(method_names),
                })

                # Index each method
                for meth in cls.get('methods', []):
                    calls_summary = ', '.join(meth.get('calls', [])[:10])
                    called_by_summary = ', '.join(meth.get('called_by', [])[:10])

                    ret = meth.get('return_type', '')
                    desc = meth.get('description', '')
                    params_str = ', '.join(meth.get('params', []))
                    static_str = 'static ' if meth.get('is_static') else ''

                    parts = [
                        f"{meth['visibility']} {static_str}method {cls['fqcn']}::{meth['name']}({params_str})",
                    ]
                    if ret:
                        parts[0] += f" : {ret}"
                    if desc:
                        parts.append(desc)
                    if calls_summary:
                        parts.append(f"Calls: {calls_summary}")
                    if called_by_summary:
                        parts.append(f"Called by: {called_by_summary}")
                    parts.append(f"File: {cls['file']}:{meth['line']+1}")
                    meth_text = '. '.join(parts)
                    meth_id = self._generate_id(f"meth-{cls['fqcn']}::{meth['name']}")

                    documents.append(meth_text)
                    ids.append(meth_id)
                    metadata.append({
                        "type": "method", "text": meth_text,
                        "source": self._detect_source(cls['file']),
                        "class": cls['fqcn'], "module": cls['module'],
                        "name": meth['name'], "visibility": meth['visibility'],
                        "is_static": meth.get('is_static', False),
                        "description": meth.get('description', ''),
                        "params": meth.get('params', []),
                        "return_type": meth.get('return_type', ''),
                        "calls": meth.get('calls', []),
                        "called_by": meth.get('called_by', []),
                        "file": cls['file'], "line": meth['line'],
                    })

    def _upload_batched(self, documents, metadata, ids, batch_size=300):
        """Uploads documents to Qdrant in batches."""
        total = len(documents)
        total_batches = (total + batch_size - 1) // batch_size
        print(f"Indexing {total} items in {total_batches} batches...")

        for i in range(0, total, batch_size):
            batch_num = (i // batch_size) + 1
            batch_docs = documents[i:i + batch_size]
            batch_meta = metadata[i:i + batch_size]
            batch_ids = ids[i:i + batch_size]

            print(f"  Batch {batch_num}/{total_batches} ({len(batch_docs)} items)...", end="\r")

            points = [
                models.PointStruct(
                    id=batch_ids[j],
                    vector={self.vector_name: models.Document(text=batch_docs[j], model=self.model_name)},
                    payload=batch_meta[j],
                )
                for j in range(len(batch_docs))
            ]
            self.client.upsert(
                collection_name=self.collection_name,
                points=points,
            )

        print(f"\nIndexed {total} items into Qdrant.")

    def _search_by(self, query: str, types: List[str] = None,
                   source: str = None, limit: int = 5) -> List[Dict[str, Any]]:
        """Low-level search with type list and source filters."""
        conditions = []
        if types:
            conditions.append(models.FieldCondition(
                key="type", match=models.MatchAny(any=types),
            ))
        if source:
            conditions.append(models.FieldCondition(
                key="source", match=models.MatchValue(value=source),
            ))

        query_filter = models.Filter(must=conditions) if conditions else None

        results = self.client.query_points(
            collection_name=self.collection_name,
            query=models.Document(text=query, model=self.model_name),
            using=self.vector_name,
            query_filter=query_filter,
            limit=limit,
        ).points

        return [{"score": r.score, "payload": r.payload} for r in results]

    def search_context(self, query: str, limit_per_category: int = 5) -> str:
        """Searches each category separately and returns a formatted LLM context prompt.

        Categories searched (in order):
          1. App code classes & methods (editable)
          2. App code XML references & templates (editable)
          3. Vendor classes & methods (read-only)
          4. Vendor XML references & templates (read-only)
          5. Modules & themes
        """
        sections = []

        # 1. App code — classes & methods
        app_classes = self._search_by(query, types=["class", "method"], source="app",
                                      limit=limit_per_category)
        if app_classes:
            sections.append(self._format_section(
                "App Code — Classes & Methods (Editable)", app_classes))

        # 2. App code — XML & templates
        app_xml = self._search_by(query, types=["reference", "template"], source="app",
                                  limit=limit_per_category)
        if app_xml:
            sections.append(self._format_section(
                "App Code — XML & Templates (Editable)", app_xml))

        # 3. Vendor — classes & methods
        vendor_classes = self._search_by(query, types=["class", "method"], source="vendor",
                                         limit=limit_per_category)
        if vendor_classes:
            sections.append(self._format_section(
                "Vendor — Classes & Methods (Read-Only)", vendor_classes))

        # 4. Vendor — XML & templates
        vendor_xml = self._search_by(query, types=["reference", "template"], source="vendor",
                                     limit=limit_per_category)
        if vendor_xml:
            sections.append(self._format_section(
                "Vendor — XML & Templates (Read-Only)", vendor_xml))

        # 5. Modules & themes
        modules = self._search_by(query, types=["module", "theme"], limit=limit_per_category)
        if modules:
            sections.append(self._format_section("Modules & Themes", modules))

        if not sections:
            return f"No results found for: {query}"

        header = f"# Magento Context: \"{query}\"\n"
        return header + "\n\n".join(sections)

    def _format_section(self, title: str, results: List[Dict[str, Any]]) -> str:
        """Formats a group of search results into a readable section."""
        lines = [f"## {title}"]
        for r in results:
            lines.append(self._format_result(r))
        return "\n".join(lines)

    def _format_result(self, result: Dict[str, Any]) -> str:
        """Formats a single search result into a readable entry."""
        p = result['payload']
        t = p.get('type', '')

        if t == 'method':
            static = "static " if p.get('is_static') else ""
            params = ", ".join(p.get('params', []))
            ret = f" : {p['return_type']}" if p.get('return_type') else ""
            line = f"- `{p['visibility']} {static}{p['class']}::{p['name']}({params}){ret}`"
            if p.get('description'):
                line += f"\n  {p['description']}"
            if p.get('calls'):
                line += f"\n  Calls: {', '.join(p['calls'][:5])}"
            if p.get('called_by'):
                line += f"\n  Called by: {', '.join(p['called_by'][:5])}"
            line += f"\n  File: {p['file']}:{p['line']+1}"
            return line

        elif t == 'class':
            parent = f" extends `{p['parent']}`" if p.get('parent') else ""
            ifaces = f" implements `{'`, `'.join(p['interfaces'])}`" if p.get('interfaces') else ""
            line = f"- **`{p['fqcn']}`**{parent}{ifaces}"
            line += f"\n  {p.get('method_count', 0)} methods | Module: {p['module']}"
            line += f"\n  File: {p['file']}:{p['line']+1}"
            return line

        elif t == 'reference':
            line = f"- {p['kind']}: `{p['value']}`"
            line += f"\n  Module: {p['module']} | Area: {p['area']}"
            line += f"\n  File: {p['file']}:{p['line']+1}"
            return line

        elif t == 'template':
            line = f"- Template: `{p['id']}`"
            line += f"\n  Module: {p['module']}"
            line += f"\n  File: {p['file']}"
            return line

        elif t == 'module':
            return f"- Module: `{p['name']}` at `{p['path']}`"

        elif t == 'theme':
            parent = p.get('parent_code', '')
            return (f"- Theme: `{p['code']}` (area: {p['area']}"
                    f"{f', parent: {parent}' if parent else ''})"
                    f"\n  Path: `{p['path']}`")

        return f"- {p.get('text', str(p))}"

    def search(self, query: str, limit: int = 5, type_filter: Optional[str] = None,
               source_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        """Raw search returning result dicts. Use search_context() for formatted output."""
        types = [type_filter] if type_filter else None
        return self._search_by(query, types=types, source=source_filter, limit=limit)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    root_path = args[0] if args else "."

    tool = MagentoSearchTool(root_path)

    if "--index" in flags:
        tool.index_from_cache()

    while True:
        query = input("\nSearch (or 'exit'): ").strip()
        if query.lower() == 'exit':
            break
        if not query:
            continue

        print(tool.search_context(query))
