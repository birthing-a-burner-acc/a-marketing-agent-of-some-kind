# app.py
"""
Gemini-hosted marketing agent.

Integrates:
- Hindsight  → persistent memory via Streamable HTTP MCP
- OpenSEO    → live SEO data via Streamable HTTP MCP (POST-only, no SSE)
- marketingskills → local markdown workflow files injected into the system prompt

Requires:
- Python 3.10+
- google-genai, mcp (v2.x), httpx
- Docker running Hindsight on localhost:8888
- A reachable OpenSEO MCP endpoint with a bearer token
- marketingskills installed under .agents/skills/
"""

import asyncio
import json
import os
import sys
from pathlib import Path
import random
import httpx

from google import genai
from google.genai import types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from config import (
    GEMINI_API_KEY,
    HINDSIGHT_MCP_URL,
    OPENSEO_MCP_URL,
    OPENSEO_MCP_TOKEN,
    SKILLS_DIR,
)
from google.genai import errors

# ──────────────────────────────────────────────────────────────────────
# Gemini client
# ──────────────────────────────────────────────────────────────────────
client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(
        timeout=600_000,  # 600 seconds (ms), applies to the whole request
        retry_options=types.HttpRetryOptions(
            attempts=5,
            initial_delay=2.0,
            max_delay=60.0,
            exp_base=2.0,
            jitter=1.0,
            http_status_codes=[408, 429, 500, 502, 503, 504],
        ),
    ),
)

async def generate_with_retry(client, *, model, contents, config, max_attempts=6):
    """
    Retries on transient failures at two layers:
      - google.genai.errors.APIError   (503, 500, 502, 504, ...)
      - httpx transport errors          (ReadError, ConnectError, ReadTimeout, ...)
    """
    RETRYABLE_CODES = {500, 502, 503, 504}
    RETRYABLE_STATUSES = {"UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED"}
    RETRYABLE_HTTPX = (
        httpx.ReadError,
        httpx.ConnectError,
        httpx.ReadTimeout,
        httpx.ConnectTimeout,
        httpx.RemoteProtocolError,
        httpx.WriteError,
    )

    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )

        # ── Gemini API-level errors ────────────────────────────────
        except errors.APIError as e:
            last_exc = e
            code = getattr(e, "code", None)
            status = (getattr(e, "status", None) or "").upper()
            if not (code in RETRYABLE_CODES or status in RETRYABLE_STATUSES):
                raise
            if attempt == max_attempts:
                raise
            delay = min(2.0 * (2 ** (attempt - 1)) + random.uniform(0, 1.5), 60.0)
            print(f"[retry] APIError {code} {status} "
                  f"(attempt {attempt}/{max_attempts}), sleeping {delay:.1f}s")
            await asyncio.sleep(delay)

        # ── Network-level errors ───────────────────────────────────
        except RETRYABLE_HTTPX as e:
            last_exc = e
            if attempt == max_attempts:
                raise
            delay = min(2.0 * (2 ** (attempt - 1)) + random.uniform(0, 1.5), 60.0)
            print(f"[retry] {type(e).__name__}: {e} "
                  f"(attempt {attempt}/{max_attempts}), sleeping {delay:.1f}s")
            await asyncio.sleep(delay)

    if last_exc:
        raise last_exc
# ──────────────────────────────────────────────────────────────────────
# Skill loading
# ──────────────────────────────────────────────────────────────────────
def load_skills(skill_names: list[str] | None = None) -> str:
    """Read SKILL.md files and concatenate them into a context block."""
    if skill_names is None:
        skill_names = ["product-marketing-context", "seo-audit"]

    context_parts: list[str] = []
    for name in skill_names:
        skill_dir = Path(SKILLS_DIR) / name
        skill_md = skill_dir / "SKILL.md"
        context_md = skill_dir / "context.md"
        file_to_read = skill_md if skill_md.exists() else context_md

        if file_to_read.exists():
            context_parts.append(
                f"\n\n--- SKILL: {name} ---\n"
                f"{file_to_read.read_text(encoding='utf-8')}"
            )
        else:
            print(f"[warn] No skill file found for '{name}' at {skill_dir}")

    return "\n".join(context_parts)


# ──────────────────────────────────────────────────────────────────────
# System prompt
# ──────────────────────────────────────────────────────────────────────
def build_system_prompt(skills_context: str) -> str:
    return f"""You are an expert marketing assistant with access to three capabilities:

1. **Hindsight** — a persistent memory system. Use it to:
   - `recall` relevant prior context before starting any task.
   - `retain` key findings, decisions, and results after finishing a task.
   - `reflect` when you need a synthesized, disposition-aware answer.

2. **OpenSEO** — live SEO data. Use it to fetch keyword research, rank
   tracking, backlink profiles, site audits, and AI-visibility metrics.
   Never guess at SEO numbers — always call OpenSEO tools.

3. **Marketing skills** — structured workflows for CRO, copywriting, SEO,
   analytics, and growth engineering. Follow the frameworks below to
   structure your analysis and output.

### Operating procedure for every task
1. First, `recall` any prior context related to the request.
2. Fetch live data from OpenSEO where SEO metrics are required.
3. Apply the relevant skill framework to interpret the data.
4. Produce a clear, well-structured output.
5. `retain` a summary of the findings so future sessions have continuity.

---

{skills_context}
"""


# ──────────────────────────────────────────────────────────────────────
# MCP → Gemini conversion
# ──────────────────────────────────────────────────────────────────────
def mcp_tools_to_gemini_declarations(mcp_tools: list) -> list[types.FunctionDeclaration]:
    """
    Convert MCP Tool objects into Gemini FunctionDeclaration objects.

    Strategy: whitelist, not blacklist. Gemini's `types.Schema` model
    accepts only a fixed subset of JSON Schema keywords. Rather than
    enumerating every unsupported keyword (exclusiveMinimum, multipleOf,
    patternProperties, etc.) as it appears, we keep only the keys Gemini
    knows about and drop the rest.

    $ref and $defs are handled first by inlining, with cycle detection.
    """
    # The exact set of keys Gemini's `types.Schema` pydantic model accepts.
    # Anything not in this set is dropped during cleaning.
    GEMINI_ALLOWED_KEYS = {
        "type",
        "format",
        "description",
        "nullable",
        "enum",
        "items",
        "properties",
        "required",
        "anyOf",
        "propertyOrdering",
        "default",
        "example",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "minLength",
        "maxLength",
        "pattern",
    }

    def resolve_ref(ref: str, root: dict) -> dict | None:
        """Resolve '#/$defs/Name' or '#/definitions/Name' against the root."""
        if not ref.startswith("#/"):
            return None
        path = ref.lstrip("#/").split("/")
        node = root
        for part in path:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return None
        return node if isinstance(node, dict) else None

    def clean_schema(node, root, seen_refs: frozenset = frozenset()):
        """
        Resolve $ref inline (with cycle detection), then keep only keys
        Gemini accepts. Recurses through dicts and lists.
        """
        if isinstance(node, dict):
            # 1. Inline $ref if present (with cycle guard)
            if "$ref" in node:
                ref = node["$ref"]
                if ref in seen_refs:
                    return {"type": "object"}
                resolved = resolve_ref(ref, root)
                if resolved is None:
                    return {"type": "object"}
                merged = {k: v for k, v in node.items() if k != "$ref"}
                merged.update(resolved)
                return clean_schema(merged, root, seen_refs | {ref})

            # 2. Keep only whitelisted keys; recurse into their values
            cleaned = {}
            for k, v in node.items():
                if k not in GEMINI_ALLOWED_KEYS:
                    continue
                # `properties` maps names -> subschemas; recurse each value
                if k == "properties" and isinstance(v, dict):
                    cleaned[k] = {
                        prop_name: clean_schema(prop_schema, root, seen_refs)
                        for prop_name, prop_schema in v.items()
                    }
                # `items` can be a subschema or a list of subschemas
                elif k == "items":
                    cleaned[k] = clean_schema(v, root, seen_refs)
                # `anyOf` is a list of subschemas
                elif k == "anyOf" and isinstance(v, list):
                    cleaned[k] = [clean_schema(sub, root, seen_refs) for sub in v]
                else:
                    cleaned[k] = v
            return cleaned

        if isinstance(node, list):
            return [clean_schema(item, root, seen_refs) for item in node]

        return node

    declarations = []
    for tool in mcp_tools:
        raw_schema = tool.input_schema or {}
        schema = clean_schema(raw_schema, raw_schema)

        parameters = None
        if schema.get("properties"):
            parameters = types.Schema(**schema)

        declarations.append(
            types.FunctionDeclaration(
                name=tool.name,
                description=tool.description or "",
                parameters=parameters,
            )
        )
    return declarations
# ──────────────────────────────────────────────────────────────────────
# MCP connection helpers
# ──────────────────────────────────────────────────────────────────────
async def open_hindsight_session(http_client: httpx.AsyncClient):
    """Connect to Hindsight via Streamable HTTP (no auth required locally)."""
    return streamable_http_client(
        HINDSIGHT_MCP_URL,
        http_client=http_client,
    )


async def open_openseo_session(http_client: httpx.AsyncClient):
    """Connect to OpenSEO via Streamable HTTP (bearer token required)."""
    return streamable_http_client(
        OPENSEO_MCP_URL,
        http_client=http_client,
    )


# ──────────────────────────────────────────────────────────────────────
# Agent
# ──────────────────────────────────────────────────────────────────────
async def run_agent(user_query: str) -> str | None:
    skills_context = load_skills()
    system_prompt = build_system_prompt(skills_context)

    hindsight_http = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=300.0),
        follow_redirects=True,
    )
    openseo_http = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=300.0),
        follow_redirects=True,
        headers={"Authorization": f"Bearer {OPENSEO_MCP_TOKEN}"},
    )

    try:
        async with hindsight_http:
            async with streamable_http_client(
                HINDSIGHT_MCP_URL, http_client=hindsight_http
            ) as (read_h, write_h):
                async with ClientSession(read_h, write_h) as hindsight_session:
                    await hindsight_session.initialize()
                    print("[ok] Connected to Hindsight")

                    async with openseo_http:
                        async with streamable_http_client(
                            OPENSEO_MCP_URL, http_client=openseo_http
                        ) as (read_o, write_o):
                            async with ClientSession(read_o, write_o) as openseo_session:
                                await openseo_session.initialize()
                                print("[ok] Connected to OpenSEO")

                                # 1. Discover tools from both servers
                                h_tools = (await hindsight_session.list_tools()).tools
                                o_tools = (await openseo_session.list_tools()).tools
                                print(
                                    f"[info] Hindsight tools: {len(h_tools)} | "
                                    f"OpenSEO tools: {len(o_tools)}"
                                )

                                # 2. Build a lookup map: tool name -> session
                                #    (so we know which session to call for a given tool)
                                session_map: dict[str, ClientSession] = {}
                                for t in h_tools:
                                    session_map[t.name] = hindsight_session
                                for t in o_tools:
                                    session_map[t.name] = openseo_session

                                # 3. Convert to Gemini FunctionDeclarations
                                declarations = (
                                    mcp_tools_to_gemini_declarations(h_tools)
                                    + mcp_tools_to_gemini_declarations(o_tools)
                                )

                                # 4. Build the config. NOTE: we pass
                                #    FunctionDeclaration objects, NOT ClientSessions.
                                config = types.GenerateContentConfig(
                                    system_instruction=system_prompt,
                                    temperature=0.2,
                                    tools=[types.Tool(function_declarations=declarations)],
                                )

                                # 5. Manual tool-calling loop
                                contents: list[types.Content] = [
                                    types.Content(
                                        role="user",
                                        parts=[types.Part(text=user_query)],
                                    )
                                ]

                                MAX_ITERATIONS = 15
                                for iteration in range(MAX_ITERATIONS):
                                    print(f"[loop] iteration {iteration + 1}")

                                    '''response = await client.aio.models.generate_content(
                                        model="gemini-3.5-flash",
                                        contents=contents,
                                        config=config,
                                    )'''

                                    response = await generate_with_retry(
                                        client,
                                        model="gemini-3.1-flash-lite",
                                        contents=contents,
                                        config=config,
                                    )

                                    # Collect any function calls from the response
                                    function_calls = []
                                    for candidate in response.candidates or []:
                                        for part in candidate.content.parts or []:
                                            if part.function_call:
                                                function_calls.append(part.function_call)

                                    # If no tool was called, we have the final answer
                                    if not function_calls:
                                        print("\n" + "=" * 60)
                                        print("AGENT RESPONSE")
                                        print("=" * 60)
                                        print(response.text)
                                        print("=" * 60)
                                        return response.text

                                    # Append the model's tool-call turn to history
                                    contents.append(response.candidates[0].content)

                                    # Execute each tool call against its MCP session
                                    tool_response_parts = []
                                    for fc in function_calls:
                                        print(
                                            f"[tool] {fc.name} "
                                            f"args={dict(fc.args or {})}"
                                        )
                                        session = session_map.get(fc.name)
                                        if session is None:
                                            tool_response_parts.append(
                                                types.Part(
                                                    function_response=types.FunctionResponse(
                                                        name=fc.name,
                                                        response={
                                                            "error": f"Unknown tool: {fc.name}"
                                                        },
                                                    )
                                                )
                                            )
                                            continue

                                        try:
                                            result = await session.call_tool(
                                                fc.name,
                                                arguments=dict(fc.args or {}),
                                            )
                                            # MCP returns a CallToolResult; flatten
                                            # the text content into a single string.
                                            text_output = "\n".join(
                                                c.text
                                                for c in result.content
                                                if hasattr(c, "text")
                                            ) or "(no text content)"
                                            # Inside the tool-call loop, right after you get `text_output`:

                                            MAX_TOOL_CHARS = 20_000  # ~5k tokens; adjust as needed

                                            if len(text_output) > MAX_TOOL_CHARS:
                                                trimmed = text_output[:MAX_TOOL_CHARS]
                                                text_output = (
                                                    trimmed
                                                    + f"\n\n[...truncated {len(text_output) - MAX_TOOL_CHARS} chars "
                                                    "to keep payload small. Request specific sections if you need more.]"
                                                    )
                                            print(
                                                f"[tool] {fc.name} → "
                                                f"{len(text_output)} chars"
                                            )
                                            tool_response_parts.append(
                                                types.Part(
                                                    function_response=types.FunctionResponse(
                                                        name=fc.name,
                                                        response={"result": text_output},
                                                    )
                                                )
                                            )
                                        except Exception as e:
                                            print(f"[tool] {fc.name} FAILED: {e}")
                                            tool_response_parts.append(
                                                types.Part(
                                                    function_response=types.FunctionResponse(
                                                        name=fc.name,
                                                        response={"error": str(e)},
                                                    )
                                                )
                                            )

                                    # Feed tool results back to the model
                                    contents.append(
                                        types.Content(
                                            role="user",
                                            parts=tool_response_parts,
                                        )
                                    )

                                # If we exhausted the loop without a final answer
                                print("[warn] Reached max tool-call iterations.")
                                return response.text

    except Exception as e:
        print(f"\n[error] {type(e).__name__}: {e}")
        raise


# ──────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
    else:
        query = (
            "Run an SEO audit for example.com. "
            "First recall any previous context from memory, "
            "then fetch live SEO data from OpenSEO, "
            "and finally structure the report using the seo-audit skill."
        )

    print(f"User Query: {query}\n")
    asyncio.run(run_agent(query))

# if you're reading this, I am ashamed of myself and am sincerely sorry for making AI slop, I feel really bad for it.
