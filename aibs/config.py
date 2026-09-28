# config.py
import os

# --- Gemini ---
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# --- Hindsight (Local MCP Server) ---
HINDSIGHT_MCP_URL = "http://localhost:8888/mcp/"

# --- OpenSEO (Remote MCP Server) ---
OPENSEO_MCP_URL = os.environ.get(
    "OPENSEO_MCP_URL",
    "https://your-app.up.railway.app/mcp"
)
OPENSEO_MCP_TOKEN = os.environ["OPENSEO_MCP_TOKEN"]

# --- Marketing Skills (Local Filesystem) ---
SKILLS_DIR = os.path.abspath(
    os.path.join(".agents", "skills")
)
