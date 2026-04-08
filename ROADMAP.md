# Roadmap

## Phase 0 — Stabilization

Ground work before building new features.

- [ ] Remove `context/` directory (unused legacy scripts)
- [ ] Remove dead `db_path` variable in `qdrant_tool.py`
- [ ] Fix README to reflect actual cache paths (`.code_graph/cache/`) and Qdrant server mode
- [ ] Add payload indexes on `kind`, `module`, `area` fields in Qdrant
- [ ] Better error handling when cache is missing/empty
- [ ] Add `pyproject.toml`

---

## Phase 1 — Search That Works

Right now search is vector-only. That's not enough for a developer who knows the exact class name they're looking for.

- [ ] `read_file` MCP tool — let the LLM read source files directly after search, with path guard and line range support
- [ ] Exact-match search — `search_magento_exact(value, kind, module, area)` using Qdrant scroll + payload filters
- [ ] Add `kind`, `module`, `area` filter params to `search_magento_raw`
- [ ] `get_class_info` MCP tool — exact FQCN lookup returning full class details (parent, interfaces, methods)

---

## Phase 2 — DI & Plugin Intelligence

The two questions every Magento dev asks: "what gets injected?" and "what intercepts this?"

- [ ] Parse constructor params to extract type-hinted dependencies
- [ ] Link `before*/after*/around*` plugin methods to their target class methods
- [ ] `get_class_dependencies` MCP tool
- [ ] `get_plugins_for` MCP tool

---

## Phase 3 — Broader Coverage

Fill the XML parsing gaps and start testing.

**New parsers:**
- [ ] `crontab.xml` — job name, instance, method, schedule
- [ ] `extension_attributes.xml`
- [ ] `config.xml` — default config values
- [ ] `indexer.xml` / `mview.xml`

**Improvements:**
- [ ] Richer `db_schema.xml` — column types, foreign keys, indexes
- [ ] `--clean` flag to wipe stale vectors before re-indexing
- [ ] Parallelize PHP class scanning (currently sequential, XML parsing is already parallel)
- [ ] Unit tests for XML parsers and PHP parser

---

## Phase 4 — Structural Queries & Incremental Indexing

Power tools and fast feedback loop.

- [ ] `list_methods(fqcn)` — all methods of a class
- [ ] `list_implementors(interface)` — all classes implementing an interface
- [ ] `trace_calls(class::method)` — callers and callees
- [ ] `list_module_contents(module)` — everything in a module
- [ ] Incremental indexing via file mtime tracking — only re-parse changed files
- [ ] `--watch` mode using `watchdog` for live re-indexing

---

## Phase 5 — Frontend, GraphQL & Polish

- [ ] Parse `schema.graphqls` — types, queries, mutations, resolver classes
- [ ] Parse `requirejs-config.js` — component maps, mixins, paths
- [ ] Parse `communication.xml`, `queue_*.xml`, `widget.xml`, `email_templates.xml`
- [ ] Index `.phtml` template content (block calls, JS init references)
- [ ] Better PHP parsing — handle closures, anonymous classes, enums (evaluate tree-sitter)
- [ ] Evaluate code-optimized embedding model
- [ ] Configurable vendor module include/exclude
- [ ] PyPI packaging

---

## Dependencies

Phases are sequential. Each builds on the previous:

```
0 (cleanup) → 1 (search) → 2 (DI) → 3 (coverage) → 4 (structural) → 5 (advanced)
```

Phase 5 items are mostly independent and can be picked individually.
