import os
import sys
import logging
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from qdrant_tool import MagentoSearchTool

# Suppress noisy HTTP logs from qdrant-client — they go to stderr and break MCP stdio
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Initialize FastMCP - it handles the JSON-RPC communication automatically
mcp = FastMCP("Magento Qdrant Search")

# Initialize the search tool (assuming project root is current dir)
# In a real scenario, you'd pass the root via environment variable or config
project_root = os.getcwd()
search_tool = MagentoSearchTool(project_root)

@mcp.tool()
def search_magento(query: str, limit_per_category: int = 5) -> str:
    """
    Search the Magento 2 project index across all categories.
    Returns results grouped by: app code classes/methods, app code XML/templates,
    vendor classes/methods, vendor XML/templates, and modules/themes.
    App code (editable) results are shown first, vendor (read-only) after.
    """
    return search_tool.search_context(query, limit_per_category)

@mcp.tool()
def search_magento_raw(query: str, limit: int = 5,
                       type_filter: str = "", source_filter: str = "") -> str:
    """
    Search the Magento 2 project index with explicit filters.
    type_filter: reference, template, module, theme, class, or method.
    source_filter: 'app' (editable app/code) or 'vendor' (read-only).
    """
    results = search_tool.search(
        query, limit,
        type_filter=type_filter or None,
        source_filter=source_filter or None,
    )
    if not results:
        return "No results found."

    output = []
    for r in results:
        output.append(search_tool._format_result(r))
    return "\n".join(output)

if __name__ == "__main__":
    # Start the server using stdio transport
    mcp.run()
