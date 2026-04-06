import os
import sys
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from qdrant_tool import MagentoSearchTool

# Initialize FastMCP - it handles the JSON-RPC communication automatically
mcp = FastMCP("Magento Qdrant Search")

# Initialize the search tool (assuming project root is current dir)
# In a real scenario, you'd pass the root via environment variable or config
project_root = os.getcwd()
search_tool = MagentoSearchTool(project_root)

@mcp.tool()
def search_magento(query: str, limit: int = 5) -> str:
    """
    Search for Magento 2 DI declarations, XML references, and templates using vector search.
    Useful for finding where a class is used in XML or finding template paths.
    """
    results = search_tool.search(query, limit)
    if not results:
        return "No results found."
    
    output = []
    for r in results:
        p = r['payload']
        note_str = f" | Notes: {p['notes']}" if p.get('notes') else ""
        output.append(f"- [{r['id']}] {p['text']} (Score: {r['score']:.3f}){note_str}")
    
    return "\n".join(output)

@mcp.tool()
def add_magento_note(item_id: str, note: str) -> str:
    """
    Append a permanent note to a specific indexed item (reference or template).
    Requires the UUID 'item_id' returned from search_magento.
    """
    try:
        search_tool.add_note(item_id, note)
        return f"Successfully added note to {item_id}"
    except Exception as e:
        return f"Error adding note: {str(e)}"

if __name__ == "__main__":
    # Start the server using stdio transport
    mcp.run()
