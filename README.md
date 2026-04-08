# Magento LLM Context

A Python toolkit that indexes a Magento 2 project's XML configuration, DI declarations, templates, and more into a vector database (Qdrant), making it searchable via semantic queries or an MCP server for LLM-powered workflows.

## Components

| File | Purpose |
|---|---|
| `indexer.py` | Parses Magento 2 XML configs (DI, events, webapi, layout, ACL, routes, db_schema, system config) and discovers modules/themes |
| `qdrant_tool.py` | Ingests the indexer output into a local Qdrant vector DB and provides semantic search |
| `mcp_qdrant_server.py` | Exposes the search as an MCP (Model Context Protocol) server for use with LLM tools like Claude Code |

## Requirements

- Python 3.10+
- Docker (for Qdrant)
- A Magento 2 project with `app/etc/config.php` and `vendor/` present

## Installation

### 1. Start Qdrant

Pull and run the Qdrant Docker container:

```bash
docker pull qdrant/qdrant
docker run -d --name qdrant -p 6333:6333 -p 6334:6334 \
  -v qdrant_storage:/qdrant/storage \
  qdrant/qdrant
```

Verify it's running:

```bash
curl http://localhost:6333/healthz
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

Dependencies: `lxml`, `qdrant-client[fastembed]`, `mcp`

## Usage

### 1. Index the Magento project

Run the indexer against your Magento 2 root directory. It parses all enabled modules, themes, and XML configs, then saves a JSON cache and a Markdown summary.

```bash
# From the Magento project root
python indexer.py /path/to/magento-root

# Or use current directory
cd /path/to/magento-root
python indexer.py
```

**What it produces:**
- `.indexer_cache.json` — full structured index (DI preferences, events, webapi routes, layout blocks, templates, etc.)
- `.indexer_summary.md` — human/LLM-readable Markdown summary of the project

On subsequent runs it loads from cache automatically. Delete `.indexer_cache.json` to force a fresh scan.

### 2. Build the vector database

Ingest the indexer cache into a local Qdrant database for semantic search.

```bash
python qdrant_tool.py /path/to/magento-root --index
```

This creates a `.qdrant_db/` folder in the Magento root containing the embedded vectors (using `sentence-transformers/all-MiniLM-L6-v2`).

**Interactive search mode:**

```bash
python qdrant_tool.py /path/to/magento-root
```

You'll get a prompt where you can type natural language queries:

```
Enter search (or 'exit'): catalog product save event observer
[0] Magento Reference: observer-instance for 'Magento\Catalog\...' (Score: 0.812)
...
```

### 3. Run the MCP server

The MCP server exposes two tools to LLM agents:

- `search_magento(query, limit_per_category)` — searches across all categories (app code classes, app XML, vendor classes, vendor XML, modules) and returns a formatted context prompt with app/code (editable) results first
- `search_magento_raw(query, limit, type_filter, source_filter)` — targeted search with explicit type (`class`, `method`, `reference`, `template`, `module`, `theme`) and source (`app` or `vendor`) filters

```bash
# Start the server (uses stdio transport)
python mcp_qdrant_server.py
```

To use with Claude Code, add it to your MCP config:

```json
{
  "mcpServers": {
    "magento-search": {
      "command": "python",
      "args": ["/path/to/mcp_qdrant_server.py"],
      "cwd": "/path/to/magento-root"
    }
  }
}
```

## CLAUDE.md Instructions

Add the following to the `CLAUDE.md` file in your Magento project root so Claude Code uses the MCP search tool instead of scanning files directly:

```markdown
# Magento Code Search

IMPORTANT: This project has a Magento search MCP tool connected. Always use it before reading or grepping files.

## Rules

- Before searching for any Magento class, method, XML config, template, event, plugin, or DI preference, call `search_magento(query)` FIRST.
- Do NOT grep or glob through `vendor/` or large XML directories. The search tool already indexes all modules, classes, methods, XML references, and templates.
- Do NOT read vendor files to understand class hierarchy. Use `search_magento("ClassName")` to get inheritance, interfaces, and method signatures.
- Use `search_magento_raw(query, type_filter="method")` to find specific function/method implementations.
- Use `search_magento_raw(query, source_filter="app")` when you only care about editable app/code files.
- Only read a file directly AFTER the search tool gives you the exact file path and line number.

## Search examples

- Find where tier pricing is implemented: `search_magento("tier price calculation")`
- Find DI preferences for an interface: `search_magento("CatalogInventory StockRegistryInterface")`
- Find event observers: `search_magento("checkout submit observer")`
- Find a specific method: `search_magento_raw("getPrice", type_filter="method")`
- Find only app/code classes: `search_magento_raw("TierPrice", type_filter="class", source_filter="app")`
- Find layout XML blocks: `search_magento("product.info.price block")`

## Why

- vendor/ has 10,000+ PHP files and 5,000+ XML files. Grepping is slow and wastes tokens.
- The search tool returns only relevant results with file paths, line numbers, and context.
- App code (editable) results are always shown before vendor (read-only) results.
```

## What gets indexed

| Reference Kind | Source XML |
|---|---|
| `preference-for`, `preference-type`, `type-name`, `plugin-type`, `virtualtype-*` | `di.xml` |
| `event-name`, `observer-instance` | `events.xml` |
| `service-class`, `service-method`, `resource-ref` | `webapi.xml` |
| `block-class`, `block-template`, `reference-block`, `update-handle` | Layout XMLs |
| `acl-resource` | `acl.xml` |
| `route-id`, `route-frontname`, `route-module` | `routes.xml` |
| `table-name`, `column-name` | `db_schema.xml` |
| `menu-resource` | `menu.xml` |
| `system-source-model`, `system-backend-model` | `system.xml` |
| `hyva-compat` | Hyva compatibility modules |
| Templates (`.phtml`) | Module `view/` and theme overrides |
