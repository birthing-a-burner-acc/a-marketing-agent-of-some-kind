# a-marketing-agent-of-some-kind
A Gemini-powered marketing agent that combines persistent memory (Hindsight), live SEO data (OpenSEO), and 33 structured marketing workflows (marketingskills) via the Model Context Protocol. Runs SEO audits, keyword research, and competitive analysis with cross-session memory. It's kinda slow though, so be wary.

## Stack

| Component | Role |
|-----------|------|
| **Gemini** | Orchestrator — reasons over tasks and calls tools |
| **Hindsight** | Persistent memory (`retain`, `recall`, `reflect`) |
| **OpenSEO** | Live SEO data — keywords, backlinks, ranks, audits |
| **marketingskills** | 33 markdown workflows (CRO, copywriting, SEO, analytics) |

## Prerequisites

- Python 3.10+
- Docker Desktop (WSL 2 on Windows)
- Node.js 18+
- API keys: **Gemini**, **DataForSEO**, and a self-generated **OpenSEO bearer token**

## Setup

### 1. Install dependencies

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1   # Windows
source .venv/bin/activate       # macOS / Linux
pip install google-genai mcp httpx

```

### 2. Start Hindsight

```bash 
export GEMINI_API_KEY="your-key"   # PowerShell: $env:GEMINI_API_KEY = "your-key"

docker run --rm -it --pull always -p 8888:8888 -p 9999:9999 \
  -v $HOME/.hindsight-docker:/home/hindsight/.pg0 \
  -e HINDSIGHT_API_LLM_PROVIDER=gemini \
  -e HINDSIGHT_API_LLM_API_KEY=$GEMINI_API_KEY \
  -e HINDSIGHT_API_LLM_MODEL=gemini-2.5-flash \
  ghcr.io/vectorize-io/hindsight:latest
  ```
  MCP endpoint: http://localhost:8888/mcp/

### 3. Deploy OpenSEO
Deploy to Railway using the OpenSEO template, set DATAFORSEO_API_KEY and OPENSEO_MCP_TOKEN, then note your public URL — your MCP endpoint is <URL>/mcp.

### 4. Install marketingskills
```bash
npx skills add coreyhaines31/marketingskills
```
Then edit .agents/skills/product-marketing-context/context.md with your product, audience, and positioning. This is required — every other skill depends on it.

## Configuration
config.py:
    ```
    import os
    GEMINI_API_KEY    = os.environ["GEMINI_API_KEY"]
    HINDSIGHT_MCP_URL = "http://localhost:8888/mcp/"
    OPENSEO_MCP_URL   = os.environ.get("OPENSEO_MCP_URL", "https://your-app.up.railway.app/mcp")
    OPENSEO_MCP_TOKEN = os.environ["OPENSEO_MCP_TOKEN"]
    SKILLS_DIR        = os.path.abspath(os.path.join(".agents", "skills"))
    ```

Set environment variables:
``` powershell
# Windows
$env:GEMINI_API_KEY    = "your-gemini-key"
$env:OPENSEO_MCP_URL   = "https://your-app.up.railway.app/mcp"
$env:OPENSEO_MCP_TOKEN = "your-openseo-token"
```
```bash
# macOS / Linux
export GEMINI_API_KEY="your-gemini-key"
export OPENSEO_MCP_URL="https://your-app.up.railway.app/mcp"
export OPENSEO_MCP_TOKEN="your-openseo-token"
```

## Usage
python app.py "Run an SEO audit for example.com. Recall prior context, fetch live SEO data, and structure the report using the seo-audit skill."

Running python app.py with no arguments uses a default SEO-audit prompt.

### Expected Output:
```
[ok] Connected to Hindsight
[ok] Connected to OpenSEO
[info] Hindsight tools: 3 | OpenSEO tools: 24
[loop] iteration 1
[tool] hindsight_recall args={'query': 'example.com'}
[loop] iteration 2
[tool] openseo_site_audit args={'target': 'example.com'}
```

Special thanks to: [Hindsight](https://github.com/vectorize-io/hindsight), [OpenSEO](https://github.com/every-app/open-seo), [marketingskills](https://github.com/coreyhaines31/marketingskills), & Google Gen AI SDK (Gemini).

Made real for HackwithHyderabad 3.0.
