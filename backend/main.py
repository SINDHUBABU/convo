import os
import json
import logging
import time
import re
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn
import httpx
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("datasphere-agent")

# Configuration from .env
DATASPHERE_API_URL = os.getenv("DATASPHERE_API_URL", "").strip()
DATASPHERE_TOKEN_URL = os.getenv("DATASPHERE_TOKEN_URL", "").strip()
DATASPHERE_SPACE_ID = os.getenv("DATASPHERE_SPACE_ID", "RADH_S3P").strip()
OAUTH_CLIENT_ID = os.getenv("OAUTH_CLIENT_ID", "").strip()
OAUTH_CLIENT_SECRET = os.getenv("OAUTH_CLIENT_SECRET", "").strip()
API_KEY = os.getenv("OPENAI_API_KEY", os.getenv("GEMINI_API_KEY", "")).strip()

# Caches
cached_oauth_token: Optional[Dict[str, Any]] = None
view_columns_cache: Dict[str, List[str]] = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting SAP Datasphere Direct Analytics Agent (with Schema Discovery & Pagination)...")
    yield
    logger.info("Stopping Agent.")

app = FastAPI(
    title="SAP Datasphere Direct Analytics API",
    description="Intelligent Schema-Aware AI Analytics Engine for SAP Datasphere",
    version="4.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class QueryRequest(BaseModel):
    user_prompt: str = Field(..., description="Natural language request from user")
    user_email: Optional[str] = Field(None, description="Optional User email for DAC")
    view_name: str = Field(..., description="Target view name")
    space_id: Optional[str] = Field(None, description="Datasphere Space ID")

class QueryResponse(BaseModel):
    query_blueprint: Dict[str, Any]
    retrieved_view_metadata: Dict[str, Any]
    execution_info: Dict[str, Any]
    data: List[Dict[str, Any]]

# Acquire OAuth 2.0 Bearer Token from SAP BTP
async def get_datasphere_token() -> str:
    global cached_oauth_token
    now = time.time()
    
    if cached_oauth_token and cached_oauth_token.get("expires_at", 0) > now + 60:
        return cached_oauth_token["access_token"]
    
    if not DATASPHERE_TOKEN_URL or not OAUTH_CLIENT_ID or not OAUTH_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="OAuth credentials missing in backend/.env"
        )

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Try 1: HTTP Basic Auth
        try:
            res = await client.post(
                DATASPHERE_TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET),
                headers={"Accept": "application/json"}
            )
            if res.status_code == 200:
                payload = res.json()
                token = payload.get("access_token")
                expires_in = payload.get("expires_in", 3600)
                cached_oauth_token = {"access_token": token, "expires_at": now + expires_in}
                logger.info("Acquired SAP OAuth Bearer token via Basic Auth.")
                return token
        except Exception as e:
            logger.warning(f"Basic auth token attempt: {e}")

        # Try 2: Body Form Credentials
        try:
            res = await client.post(
                DATASPHERE_TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": OAUTH_CLIENT_ID,
                    "client_secret": OAUTH_CLIENT_SECRET
                },
                headers={"Accept": "application/json"}
            )
            if res.status_code == 200:
                payload = res.json()
                token = payload.get("access_token")
                expires_in = payload.get("expires_in", 3600)
                cached_oauth_token = {"access_token": token, "expires_at": now + expires_in}
                logger.info("Acquired SAP OAuth Bearer token via Body Credentials.")
                return token
            else:
                raise HTTPException(status_code=502, detail=f"SAP OAuth Error (HTTP {res.status_code}): {res.text}")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Cannot reach SAP Token URL: {str(e)}")

def get_tenant_base_host(url: str) -> str:
    match = re.match(r"(https?://[^/]+)", url.strip())
    if match:
        return match.group(1)
    return "https://bdcds.us10.hcs.cloud.sap"

def resolve_next_link(current_url: str, next_link_str: str) -> str:
    """Accurately resolves relative or absolute OData nextLink URLs."""
    if next_link_str.startswith("http://") or next_link_str.startswith("https://"):
        return next_link_str
    if next_link_str.startswith("/"):
        base_h = get_tenant_base_host(current_url)
        return f"{base_h}{next_link_str}"
    if next_link_str.startswith("?"):
        clean_curr = current_url.split("?")[0]
        return f"{clean_curr}{next_link_str}"
    # Relative to entity set (e.g. "ZFI_FA_DSO_KPI1?$skiptoken=..." or "$skiptoken=...")
    parent_url = current_url.split("?")[0].rsplit("/", 1)[0]
    return f"{parent_url}/{next_link_str}"

# LLM Query Planner with EXACT schema column awareness
async def generate_odata_query_blueprint(user_prompt: str, view_name: str, space_id: str, available_columns: List[str]) -> Dict[str, Any]:
    default_blueprint: Dict[str, Any] = {
        "view_name": view_name,
        "space_id": space_id,
        "$select": None,
        "$filter": None,
        "$orderby": None,
        "$top": None,
        "explanation": f"Query view {view_name}"
    }

    prompt_lower = user_prompt.lower()
    
    # 1. Detect Top N
    top_match = re.search(r'\btop\s+(\d+)\b', prompt_lower) or re.search(r'\bfirst\s+(\d+)\b', prompt_lower) or re.search(r'\blimit\s+(\d+)\b', prompt_lower)
    if top_match:
        default_blueprint["$top"] = int(top_match.group(1))
    elif "all" in prompt_lower or "full" in prompt_lower or "entire" in prompt_lower:
        default_blueprint["$top"] = None

    # 2. Detect Sort Column and Direction
    direction = "asc" if ("ascending" in prompt_lower or " asc" in prompt_lower or "lowest" in prompt_lower or "bottom" in prompt_lower) else "desc"
    
    # Match against available columns or clean alphanumeric tokens
    for c in available_columns:
        c_clean = c.lower().replace("_", "")
        # Remove spaces/punctuation from prompt for matching
        p_clean = prompt_lower.replace(" ", "").replace("_", "")
        if c_clean in p_clean:
            default_blueprint["$orderby"] = f"{c} {direction}"
            default_blueprint["explanation"] = f"Sorted by {c} in {direction.upper()} order"
            break

    # Fallback heuristic if column list wasn't populated yet
    if not default_blueprint["$orderby"]:
        words = re.findall(r'\b[a-zA-Z_]+\b', prompt_lower)
        for w in ["grossrevenueamount", "grossrevenue", "netrevenue", "amount", "salesorder", "accountingdocument"]:
            if w in prompt_lower.replace(" ", ""):
                # Try finding in available columns or uppercase
                matched_c = next((c for c in available_columns if c.lower().replace("_", "") == w), w.upper())
                default_blueprint["$orderby"] = f"{matched_c} {direction}"
                break

    cols_str = ", ".join(available_columns) if available_columns else "Unknown"

    if API_KEY and not API_KEY.startswith("sk-your"):
        system_prompt = f"""You are an expert SAP Datasphere SQL/OData Query Generator.
Target View: {view_name}
Target Space: {space_id}
Available Exact View Columns: [{cols_str}]

User Question: "{user_prompt}"

Instructions:
1. Map the user's intent to the EXACT column names provided above.
2. If the user asks for "top N", set "$top": N and set "$orderby": "EXACT_COLUMN_NAME desc".
3. Output ONLY a raw valid JSON object with no markdown syntax:
{{
  "$select": "<comma-separated exact column names or null>",
  "$filter": "<valid OData filter expression using exact column names or null>",
  "$orderby": "<exact column name followed by asc or desc or null>",
  "$top": <integer or null>,
  "explanation": "<brief rationale>"
}}"""

        async with httpx.AsyncClient(timeout=10.0) as client:
            # Case A: Google Gemini Key
            if API_KEY.startswith("AQ") or API_KEY.startswith("AIza"):
                try:
                    gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={API_KEY}"
                    gemini_payload = {
                        "contents": [{"parts": [{"text": system_prompt}]}],
                        "generationConfig": {"response_mime_type": "application/json"}
                    }
                    res = await client.post(gemini_url, json=gemini_payload)
                    if res.status_code == 200:
                        data = res.json()
                        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
                        parsed = json.loads(raw_text)
                        for k in ["$select", "$filter", "$orderby", "$top", "explanation"]:
                            if k in parsed and parsed[k] is not None:
                                default_blueprint[k] = parsed[k]
                        return default_blueprint
                except Exception as eg:
                    logger.warning(f"Gemini generation notice: {eg}")

            # Case B: OpenAI Key
            elif API_KEY.startswith("sk-"):
                try:
                    openai_payload = {
                        "model": "gpt-4o",
                        "messages": [{"role": "system", "content": system_prompt}],
                        "response_format": {"type": "json_object"}
                    }
                    res = await client.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                        json=openai_payload
                    )
                    if res.status_code == 200:
                        data = res.json()
                        parsed = json.loads(data["choices"][0]["message"]["content"])
                        for k in ["$select", "$filter", "$orderby", "$top", "explanation"]:
                            if k in parsed and parsed[k] is not None:
                                default_blueprint[k] = parsed[k]
                        return default_blueprint
                except Exception as eo:
                    logger.warning(f"OpenAI generation notice: {eo}")

    return default_blueprint

# Probing helper to discover schema columns of any view
async def probe_view_columns(base_host: str, space_id: str, view_name: str, bearer_token: str) -> List[str]:
    cache_key = f"{space_id}/{view_name}"
    if cache_key in view_columns_cache and view_columns_cache[cache_key]:
        return view_columns_cache[cache_key]

    headers = {"Authorization": f"Bearer {bearer_token}", "Accept": "application/json"}
    test_urls = [
        f"{base_host}/api/v1/datasphere/consumption/relational/{space_id}/{view_name}/{view_name}",
        f"{base_host}/api/v1/datasphere/consumption/relational/{space_id}/{view_name}",
        f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id}/{view_name}/{view_name}",
        f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id}/{view_name}"
    ]

    async with httpx.AsyncClient(timeout=10.0) as client:
        for url in test_urls:
            try:
                res = await client.get(url, headers=headers, params={"$top": "1"})
                if res.status_code == 200:
                    payload = res.json()
                    val = payload.get("value", payload.get("d", {}).get("results", []))
                    if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                        # Check if it was service root
                        if "url" in val[0] and "name" in val[0] and len(val[0].keys()) <= 3:
                            continue
                        cols = list(val[0].keys())
                        view_columns_cache[cache_key] = cols
                        logger.info(f"Discovered {len(cols)} schema columns for {cache_key}: {cols[:6]}...")
                        return cols
            except Exception:
                pass
    return []

@app.get("/api/info")
@app.get("/health")
async def get_info():
    return {
        "status": "online",
        "space_id": DATASPHERE_SPACE_ID,
        "token_url": DATASPHERE_TOKEN_URL,
        "base_url": DATASPHERE_API_URL
    }

@app.post("/api/analytics", response_model=QueryResponse)
async def execute_analytics(request: QueryRequest):
    user_prompt = request.user_prompt.strip()
    view_name = request.view_name.strip()
    space_id = request.space_id.strip() if request.space_id else DATASPHERE_SPACE_ID
    user_email = request.user_email.strip() if request.user_email else None

    if not view_name:
        raise HTTPException(status_code=400, detail="View Name is required.")

    logger.info(f"Querying View [{view_name}] in Space [{space_id}] for: '{user_prompt}'")

    # 1. Fetch live Bearer token
    bearer_token = await get_datasphere_token()
    base_host = get_tenant_base_host(DATASPHERE_API_URL)

    # 2. Discover live columns for this view
    discovered_columns = await probe_view_columns(base_host, space_id, view_name, bearer_token)

    # 3. Generate query blueprint with exact schema awareness
    blueprint = await generate_odata_query_blueprint(user_prompt, view_name, space_id, discovered_columns)

    # 4. Assemble query parameters
    params: Dict[str, str] = {}
    for key in ["$select", "$filter", "$orderby"]:
        val = blueprint.get(key)
        if val is not None and str(val).strip() and str(val).lower() != "null":
            params[key] = str(val)

    # Only pass $top to Datasphere if it's a small slice (<= 1000)
    explicit_top = blueprint.get("$top")
    if explicit_top is not None:
        try:
            top_int = int(explicit_top)
            if top_int <= 1000:
                params["$top"] = str(top_int)
        except ValueError:
            pass

    # Candidate endpoints
    candidate_endpoints = [
        f"{base_host}/api/v1/datasphere/consumption/relational/{space_id}/{view_name}/{view_name}",
        f"{base_host}/api/v1/datasphere/consumption/relational/{space_id}/{view_name}",
        f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id}/{view_name}/{view_name}",
        f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id}/{view_name}"
    ]

    headers_no_dac = {
        "Authorization": f"Bearer {bearer_token}",
        "Accept": "application/json",
        "User-Agent": "Datasphere-Agent/4.0"
    }

    header_attempts = []
    if user_email:
        headers_with_dac = {
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "application/json",
            "SAP-User-Context": f"email={user_email}",
            "User-Agent": "Datasphere-Agent/4.0"
        }
        header_attempts.append((headers_with_dac, "DAC"))
    header_attempts.append((headers_no_dac, "Direct"))

    raw_data: List[Dict[str, Any]] = []
    executed_url = ""
    error_logs: List[str] = []
    success = False
    next_link: Optional[str] = None

    if explicit_top is not None:
        try:
            target_limit = int(explicit_top)
        except ValueError:
            target_limit = 50000
    else:
        target_limit = 50000  # Default to fetching all records (25,000+)

    async with httpx.AsyncClient(timeout=120.0) as client:
        for ep in candidate_endpoints:
            for active_headers, h_mode in header_attempts:
                # Try with query params, then fallback without params if 400
                for active_params, p_mode in [(params, "query-params"), ({}, "all-records")]:
                    try:
                        logger.info(f"Calling [{h_mode}|{p_mode}]: {ep} (params={active_params})")
                        res = await client.get(ep, headers=active_headers, params=active_params)
                        
                        if res.status_code == 200:
                            payload = res.json()
                            val = payload.get("value")
                            if val is None:
                                val = payload.get("d", {}).get("results", payload if isinstance(payload, list) else [])

                            # Check for Service Root Document
                            is_service_doc = (
                                isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict) and
                                ("name" in val[0] and "url" in val[0] and len(val[0].keys()) <= 3)
                            )
                            if is_service_doc:
                                entity_url = f"{ep.rstrip('/')}/{val[0].get('url', view_name)}"
                                logger.info(f"Drilling down to entity set: {entity_url}")
                                res_e = await client.get(entity_url, headers=active_headers, params=active_params)
                                if res_e.status_code == 200:
                                    p_e = res_e.json()
                                    val = p_e.get("value", p_e.get("d", {}).get("results", []))
                                    next_link = p_e.get("@odata.nextLink") or p_e.get("__next")
                                    executed_url = entity_url
                            else:
                                next_link = payload.get("@odata.nextLink") or payload.get("__next")
                                executed_url = ep

                            if isinstance(val, list):
                                raw_data = val
                                success = True
                                
                                # Follow pagination links if user requested more records (e.g. up to 25k+ records)
                                page_count = 1
                                max_pages = 100  # Up to 50,000 records (100 pages * 500-1000 rows)
                                current_page_url = executed_url
                                while next_link and len(raw_data) < target_limit and page_count < max_pages:
                                    try:
                                        p_url = resolve_next_link(current_page_url, str(next_link))
                                        logger.info(f"Fetching next page {page_count+1} ({len(raw_data)} records retrieved so far) -> {p_url[:90]}...")
                                        res_p = await client.get(p_url, headers=active_headers)
                                        if res_p.status_code == 200:
                                            p_data = res_p.json()
                                            p_rows = p_data.get("value", p_data.get("d", {}).get("results", []))
                                            if not p_rows:
                                                logger.info("Next page returned empty rows, pagination complete.")
                                                break
                                            raw_data.extend(p_rows)
                                            current_page_url = p_url
                                            next_link = p_data.get("@odata.nextLink") or p_data.get("__next")
                                            page_count += 1
                                        else:
                                            logger.warning(f"Pagination request HTTP {res_p.status_code} at {p_url}: {res_p.text[:200]}")
                                            break
                                    except Exception as ep_err:
                                        logger.warning(f"Pagination page error: {ep_err}")
                                        break

                                logger.info(f"SUCCESS: Retrieved {len(raw_data)} total records across {page_count} pages from {executed_url}")
                                break
                            elif isinstance(val, dict):
                                raw_data = [val]
                                executed_url = ep
                                success = True
                                break
                        else:
                            err_txt = f"[{h_mode}] HTTP {res.status_code} from {ep}: {res.text.strip()[:200]}"
                            error_logs.append(err_txt)
                    except Exception as e:
                        err_txt = f"[{h_mode}] Error on {ep}: {str(e)}"
                        error_logs.append(err_txt)

                    if success or res.status_code not in (400, 403, 404):
                        break
                if success:
                    break
            if success:
                break

    if not success:
        last_errors = "\n".join(error_logs[-4:]) if error_logs else "Endpoint unreachable"
        raise HTTPException(
            status_code=400,
            detail=(
                f"SAP Datasphere cannot access view '{view_name}' in Space '{space_id}'.\n\n"
                f"Datasphere response:\n{last_errors}\n\n"
                f"💡 Common Solutions in SAP Datasphere:\n"
                f"1. Open SAP Datasphere Data Builder > Open '{view_name}'.\n"
                f"2. Ensure the 'Expose for Consumption' toggle is turned ON (in the view properties).\n"
                f"3. Click 'Deploy' (or Save & Deploy) to publish the view.\n"
                f"4. Verify the Technical Name matches '{view_name}' exactly (not the Business Name)."
            )
        )

    # 5. In-Memory Precision Sorting / Slicing Guarantee
    # If SAP OData server ignored orderby or if user requested top N, enforce on client dataset
    orderby_clause = blueprint.get("$orderby")
    if orderby_clause and raw_data:
        try:
            parts = orderby_clause.strip().split()
            col_target = parts[0]
            desc = len(parts) > 1 and parts[1].lower() == "desc"
            
            # Find matching key in raw_data (case-insensitive)
            sample_keys = list(raw_data[0].keys())
            matched_key = next((k for k in sample_keys if k.lower() == col_target.lower()), col_target)
            
            if matched_key in raw_data[0]:
                def sort_key(row):
                    v = row.get(matched_key)
                    if v is None:
                        return -float('inf') if desc else float('inf')
                    if isinstance(v, (int, float)):
                        return v
                    try:
                        return float(str(v).replace(",", ""))
                    except ValueError:
                        return str(v).lower()
                
                raw_data.sort(key=sort_key, reverse=desc)
                logger.info(f"Applied precision sorting on column '{matched_key}' (desc={desc})")
        except Exception as es:
            logger.warning(f"Precision sort notice: {es}")

    # Enforce Top N limit if specified
    top_val = blueprint.get("$top")
    if top_val and isinstance(top_val, int) and top_val < len(raw_data):
        raw_data = raw_data[:top_val]
        logger.info(f"Applied top limit: {top_val} rows")

    return QueryResponse(
        query_blueprint=blueprint,
        retrieved_view_metadata={
            "view_name": view_name,
            "space_id": space_id,
            "description": f"SAP Datasphere View {view_name}",
            "columns": list(raw_data[0].keys()) if raw_data else []
        },
        execution_info={
            "datasphere_endpoint": executed_url,
            "space_id": space_id,
            "view_name": view_name,
            "odata_parameters": params,
            "live_mode": True,
            "total_records": len(raw_data)
        },
        data=raw_data
    )

if __name__ == "__main__":
    logger.info("Starting SAP Datasphere Direct Analytics backend on http://0.0.0.0:9050")
    uvicorn.run(app, host="0.0.0.0", port=9050)
