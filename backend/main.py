import os
import json
import logging
import time
import re
import asyncio
import sqlite3
import uuid
import difflib
from typing import Optional, Dict, Any, List, Tuple
from contextlib import asynccontextmanager

import rag_service

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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
API_KEY = GEMINI_API_KEY or OPENAI_API_KEY

# Caches & Session Memory
cached_oauth_token: Optional[Dict[str, Any]] = None
view_columns_cache: Dict[str, List[str]] = {}
view_working_endpoint_cache: Dict[str, str] = {}
space_views_cache: Dict[str, List[str]] = {}
cached_gemini_working: Optional[bool] = None

# Supabase Cloud Database Configuration (Optional for Cloud Session Persistence)
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", os.getenv("SUPABASE_SERVICE_ROLE_KEY", os.getenv("SUPABASE_ANON_KEY", ""))).strip()
USE_SUPABASE = bool(SUPABASE_URL and SUPABASE_KEY)

# Multi-Turn Conversational Session Store (Zero Data Leakage)
conversation_sessions: Dict[str, Dict[str, Any]] = {}

SESSION_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "datasphere_chat_sessions.db")

def init_session_db():
    if USE_SUPABASE:
        logger.info(f"Using Supabase Database at {SUPABASE_URL} for Persistent Chat Sessions.")
        return
    try:
        conn = sqlite3.connect(SESSION_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                session_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                space_id TEXT,
                views TEXT,
                created_at REAL,
                updated_at REAL,
                messages_json TEXT
            )
        """)
        conn.commit()
        conn.close()
        logger.info(f"Initialized Local SQLite Chat Session Store at {SESSION_DB_PATH}")
    except Exception as e:
        logger.warning(f"Could not initialize session DB: {e}")

def save_chat_session_to_db(session_id: str, title: str, space_id: str, views: List[str], messages: List[Dict[str, Any]]):
    # 1. Primary Store: Supabase Cloud Database if configured
    if USE_SUPABASE:
        try:
            url = f"{SUPABASE_URL}/rest/v1/chat_sessions"
            headers = {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates"
            }
            payload = {
                "id": session_id,
                "title": title,
                "space_id": space_id,
                "views": views,
                "messages": messages,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            }
            with httpx.Client(timeout=6.0) as client:
                res = client.post(url, headers=headers, json=payload)
                if res.status_code in (200, 201, 204):
                    return
                else:
                    logger.warning(f"Supabase save session error (HTTP {res.status_code}): {res.text}")
        except Exception as e:
            logger.warning(f"Supabase save error (falling back to SQLite): {e}")

    # 2. Local SQLite Persistence Fallback
    try:
        conn = sqlite3.connect(SESSION_DB_PATH)
        cursor = conn.cursor()
        now = time.time()
        cursor.execute("""
            INSERT INTO chat_sessions (session_id, title, space_id, views, created_at, updated_at, messages_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                title = excluded.title,
                space_id = excluded.space_id,
                views = excluded.views,
                updated_at = excluded.updated_at,
                messages_json = excluded.messages_json
        """, (
            session_id,
            title,
            space_id,
            json.dumps(views),
            now,
            now,
            json.dumps(messages)
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Error saving chat session {session_id}: {e}")

def get_all_chat_sessions_from_db() -> List[Dict[str, Any]]:
    # 1. Fetch from Supabase Cloud Database if configured
    if USE_SUPABASE:
        try:
            url = f"{SUPABASE_URL}/rest/v1/chat_sessions?select=*&order=updated_at.desc"
            headers = {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Accept": "application/json"
            }
            with httpx.Client(timeout=6.0) as client:
                res = client.get(url, headers=headers)
                if res.status_code == 200:
                    rows = res.json()
                    sessions = []
                    for r in rows:
                        sessions.append({
                            "id": r.get("id") or r.get("session_id"),
                            "title": r.get("title", "Untitled Session"),
                            "space_id": r.get("space_id", DATASPHERE_SPACE_ID),
                            "views": r.get("views") if isinstance(r.get("views"), list) else [],
                            "timestamp": r.get("updated_at") or r.get("created_at") or time.strftime("%b %d, %Y"),
                            "messages": r.get("messages") if isinstance(r.get("messages"), list) else []
                        })
                    return sessions
                else:
                    logger.warning(f"Supabase fetch sessions error (HTTP {res.status_code}): {res.text}")
        except Exception as e:
            logger.warning(f"Supabase fetch error (falling back to SQLite): {e}")

    # 2. Local SQLite Fetch Fallback
    try:
        conn = sqlite3.connect(SESSION_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT session_id, title, space_id, views, created_at, updated_at, messages_json FROM chat_sessions ORDER BY updated_at DESC")
        rows = cursor.fetchall()
        sessions = []
        for r in rows:
            try:
                msgs = json.loads(r["messages_json"]) if r["messages_json"] else []
            except Exception:
                msgs = []
            try:
                v_list = json.loads(r["views"]) if r["views"] else []
            except Exception:
                v_list = []
            sessions.append({
                "id": r["session_id"],
                "title": r["title"],
                "space_id": r["space_id"],
                "views": v_list,
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "timestamp": time.strftime("%b %d, %Y", time.localtime(r["updated_at"])),
                "messages": msgs
            })
        conn.close()
        return sessions
    except Exception as e:
        logger.warning(f"Error fetching chat sessions: {e}")
        return []

def delete_chat_session_from_db(session_id: str):
    if USE_SUPABASE:
        try:
            url = f"{SUPABASE_URL}/rest/v1/chat_sessions?id=eq.{session_id}"
            headers = {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}"
            }
            with httpx.Client(timeout=6.0) as client:
                client.delete(url, headers=headers)
        except Exception as e:
            logger.warning(f"Supabase delete error: {e}")

    try:
        conn = sqlite3.connect(SESSION_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM chat_sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Error deleting chat session {session_id}: {e}")

def clear_all_chat_sessions_from_db():
    if USE_SUPABASE:
        try:
            url = f"{SUPABASE_URL}/rest/v1/chat_sessions?id=neq.none"
            headers = {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}"
            }
            with httpx.Client(timeout=6.0) as client:
                client.delete(url, headers=headers)
        except Exception as e:
            logger.warning(f"Supabase clear error: {e}")

    try:
        conn = sqlite3.connect(SESSION_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM chat_sessions")
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Error clearing chat sessions: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting SAP Datasphere Conversational AI Agent (Space Auto-Discovery & Multi-Turn Chat)...")
    init_session_db()
    rag_service.init_rag_db()
    yield
    logger.info("Stopping Agent.")

app = FastAPI(
    title="SAP Datasphere Conversational AI Agent",
    description="Zero-Leakage Multi-Turn Chatbot & Auto-Routing Analytics for SAP Datasphere",
    version="5.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class SchemaRequest(BaseModel):
    view_names: Optional[List[str]] = Field(default_factory=list, description="List of view names (or empty to discover all in space)")
    space_id: Optional[str] = Field(None, description="Datasphere Space ID")

class QueryRequest(BaseModel):
    user_prompt: str = Field(..., description="Natural language request from user")
    user_email: Optional[str] = Field(None, description="Optional User email for DAC")
    session_id: Optional[str] = Field(None, description="Conversational session ID for multi-turn chat")
    view_name: Optional[str] = Field(None, description="Target view name (single view mode)")
    view_names: Optional[List[str]] = Field(default_factory=list, description="Target view names (or empty for Space Auto-Discovery)")
    selected_fields: Optional[List[str]] = Field(default_factory=list, description="User-selected columns to retrieve and display")
    space_id: Optional[str] = Field(None, description="Datasphere Space ID")

class KPICard(BaseModel):
    label: str
    value: Any
    formatted_value: str
    metric_type: str  # sum, avg, max, min, count

class KPISummary(BaseModel):
    direct_answer: str
    target_metric: Optional[str] = None
    cards: List[KPICard] = []

class QueryResponse(BaseModel):
    session_id: str
    query_blueprint: Dict[str, Any]
    retrieved_view_metadata: Dict[str, Any]
    execution_info: Dict[str, Any]
    kpi_summary: Optional[KPISummary] = None
    data: List[Dict[str, Any]]
    views_data: Optional[Dict[str, List[Dict[str, Any]]]] = None

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
    if next_link_str.startswith("http://") or next_link_str.startswith("https://"):
        return next_link_str
    if next_link_str.startswith("/"):
        base_h = get_tenant_base_host(current_url)
        return f"{base_h}{next_link_str}"
    if next_link_str.startswith("?"):
        clean_curr = current_url.split("?")[0]
        return f"{clean_curr}{next_link_str}"
    parent_url = current_url.split("?")[0].rsplit("/", 1)[0]
    return f"{parent_url}/{next_link_str}"

def resolve_best_matching_column(target_col: str, available_columns: List[str]) -> Optional[str]:
    if not target_col or not available_columns:
        return None
    cleaned_input = target_col.strip().strip("'\"`")
    raw_clean = re.sub(r'^(?:the|a|an|all|its|their|that|where|by|each|per|for)\s+', '', cleaned_input, flags=re.IGNORECASE).strip().strip("'\"`")
    target_clean = raw_clean.lower().replace("_", "").replace(" ", "")
    if not target_clean or len(target_clean) < 2:
        return None

    # 1. Exact case-insensitive match
    for c in available_columns:
        if c.lower() == raw_clean.lower() or c.lower() == cleaned_input.lower():
            return c
    # 2. Clean alphanumeric match (no underscores/spaces)
    for c in available_columns:
        if c.lower().replace("_", "").replace(" ", "") == target_clean:
            return c
    # 3. Substring match (require target_clean >= 3 characters to avoid false matches on single characters)
    if len(target_clean) >= 3:
        for c in available_columns:
            c_clean = c.lower().replace("_", "").replace(" ", "")
            if target_clean in c_clean or (len(c_clean) >= 4 and c_clean in target_clean):
                return c
    # 4. Token overlap
    t_tokens = [t for t in re.split(r'[^a-zA-Z0-9]+', raw_clean.lower()) if len(t) >= 3]
    if t_tokens:
        best_c = None
        best_score = 0
        for c in available_columns:
            c_tokens = [t for t in re.split(r'[^a-zA-Z0-9]+', c.lower()) if len(t) >= 3]
            overlap = sum(1 for t in t_tokens if any(t in ct or ct in t for ct in c_tokens))
            if overlap > best_score:
                best_score = overlap
                best_c = c
        if best_score > 0:
            return best_c
    # 5. Fuzzy match using difflib (require length >= 3)
    if len(target_clean) >= 3:
        matches = difflib.get_close_matches(target_clean, [c.lower().replace("_", "").replace(" ", "") for c in available_columns], n=1, cutoff=0.60)
        if matches:
            matched_str = matches[0]
            for c in available_columns:
                if c.lower().replace("_", "").replace(" ", "") == matched_str:
                    return c
    return None

def infer_column_sqlite_type(values: List[Any]) -> str:
    non_nulls = [v for v in values if v is not None and str(v).strip() != ""]
    if not non_nulls:
        return "TEXT"
    is_int = True
    is_float = True
    for v in non_nulls[:100]:
        v_str = str(v).replace(",", "").replace("$", "").replace("€", "").replace("₹", "").strip()
        try:
            val_f = float(v_str)
            if not (val_f.is_integer() or abs(val_f - round(val_f)) < 0.0001):
                is_int = False
        except ValueError:
            is_int = False
            is_float = False
            break
            
    if is_int:
        return "INTEGER"
    if is_float:
        return "REAL"
    return "TEXT"

def deduplicate_view_names(views: List[str]) -> List[str]:
    """Deduplicates view names case-insensitively so that casing variations (e.g. Agent_kpi vs AGENT_KPI) are treated as ONE view."""
    seen = set()
    deduped = []
    for v in views:
        if not v:
            continue
        v_clean = v.strip()
        v_lower = v_clean.lower()
        if v_lower not in seen:
            seen.add(v_lower)
            deduped.append(v_clean)
    return deduped

import xml.etree.ElementTree as ET

def extract_views_from_xml(xml_text: str) -> List[str]:
    names = set()
    if not xml_text:
        return []

    # 1. Regex across newlines for EntitySet Name
    for m in re.finditer(r'<[a-zA-Z0-9_:]*EntitySet\b[\s\S]*?\bName=["\']([a-zA-Z0-9_]+)["\']', xml_text, re.IGNORECASE):
        v = m.group(1).strip()
        if not v.startswith("Edm.") and not v.startswith("SAP__"):
            names.add(v)
            
    # 2. Regex for collection href
    for m in re.finditer(r'<[a-zA-Z0-9_:]*collection\b[\s\S]*?\bhref=["\']([a-zA-Z0-9_]+)["\']', xml_text, re.IGNORECASE):
        v = m.group(1).strip()
        if not v.startswith("Edm.") and not v.startswith("SAP__"):
            names.add(v)

    # 3. Regex for EntityType Name
    for m in re.finditer(r'<[a-zA-Z0-9_:]*EntityType\b[\s\S]*?\bName=["\']([a-zA-Z0-9_]+)["\']', xml_text, re.IGNORECASE):
        v = m.group(1).strip()
        if v.endswith("Type") and len(v) > 4:
            names.add(v[:-4])
        elif not v.startswith("Edm.") and not v.startswith("SAP__"):
            names.add(v)

    # 4. General Name/EntitySet attributes in container elements
    for m in re.finditer(r'<[a-zA-Z0-9_:]*(?:Singleton|FunctionImport|ActionImport|EntitySetMapping)\b[\s\S]*?\b(?:Name|EntitySet)=["\']([a-zA-Z0-9_]+)["\']', xml_text, re.IGNORECASE):
        v = m.group(1).strip()
        if not v.startswith("Edm.") and not v.startswith("SAP__"):
            names.add(v)

    # 5. ElementTree parsing with namespace stripping
    try:
        clean_xml = re.sub(r'\sxmlns(:\w+)?="[^"]+"', '', xml_text, count=0)
        root = ET.fromstring(clean_xml)
        for elem in root.iter():
            tag_name = elem.tag.split("}")[-1].lower()
            if tag_name in ("entityset", "collection", "singleton"):
                n = elem.get("Name") or elem.get("name") or elem.get("href")
                if n and not n.startswith("Edm.") and not n.startswith("SAP__"):
                    names.add(n.strip())
            elif tag_name == "entitytype":
                n = elem.get("Name") or elem.get("name")
                if n and not n.startswith("Edm.") and not n.startswith("SAP__"):
                    if n.endswith("Type") and len(n) > 4:
                        names.add(n[:-4].strip())
                    else:
                        names.add(n.strip())
    except Exception:
        pass

    return list(names)

def extract_views_from_json(json_obj: Any) -> List[str]:
    names = []
    if isinstance(json_obj, dict):
        if "value" in json_obj and isinstance(json_obj["value"], list):
            for item in json_obj["value"]:
                if isinstance(item, dict):
                    n = (item.get("name") or item.get("url") or item.get("title") or 
                         item.get("id") or item.get("technicalName") or item.get("businessName") or
                         item.get("artifactName") or item.get("viewName") or item.get("tableName") or 
                         item.get("entityName") or item.get("assetName") or item.get("displayName") or 
                         item.get("externalName") or item.get("objectName") or item.get("resourceName"))
                    if n and isinstance(n, str) and not n.startswith("Edm.") and not n.startswith("$") and not n.startswith("http"):
                        names.append(n.strip())
                elif isinstance(item, str) and not item.startswith("Edm.") and not item.startswith("$"):
                    names.append(item.strip())
        d_obj = json_obj.get("d")
        if isinstance(d_obj, dict):
            items = d_obj.get("EntitySets", d_obj.get("results", []))
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        n = (item.get("name") or item.get("url") or item.get("title") or 
                             item.get("technicalName") or item.get("businessName") or item.get("artifactName") or
                             item.get("assetName") or item.get("displayName"))
                        if n and isinstance(n, str) and not n.startswith("Edm."):
                            names.append(n.strip())
                    elif isinstance(item, str) and not item.startswith("Edm."):
                        names.append(item.strip())
        for k, v in json_obj.items():
            k_lower = k.lower()
            if k_lower in ("name", "url", "viewname", "tablename", "entityname", "technicalname", "businessname", "artifactname", "entitysetname", "assetname", "displayname", "externalname", "objectname", "resourcename") and isinstance(v, str):
                if v and not v.startswith("Edm.") and not v.startswith("$") and not v.startswith("http") and len(v) < 100:
                    names.append(v.strip())
            elif isinstance(v, (dict, list)):
                names.extend(extract_views_from_json(v))
    elif isinstance(json_obj, list):
        for item in json_obj:
            names.extend(extract_views_from_json(item))
    return names

def extract_columns_from_metadata_xml(xml_text: str, target_view: str) -> List[str]:
    if not xml_text:
        return []
    cols: List[str] = []
    
    clean_target = target_view.lower().replace("_", "")
    target_tokens = [t for t in re.split(r'[^a-zA-Z0-9]+', target_view.lower()) if t]

    # 1. Look for EntityType matching target_view or token match
    entity_blocks = re.findall(r'<[a-zA-Z0-9_:]*EntityType\b[\s\S]*?</[a-zA-Z0-9_:]*EntityType>', xml_text, re.IGNORECASE)
    for block in entity_blocks:
        name_m = re.search(r'\bName=["\']([a-zA-Z0-9_.]+)["\']', block, re.IGNORECASE)
        if name_m:
            raw_e_name = name_m.group(1).lower()
            e_name_clean = raw_e_name.replace("_", "")
            # check direct match, substring match, or token overlap
            if (clean_target in e_name_clean or 
                e_name_clean in clean_target or 
                any(t in e_name_clean for t in target_tokens if len(t) >= 3)):
                for pm in re.finditer(r'<[a-zA-Z0-9_:]*Property\b[\s\S]*?\bName=["\']([a-zA-Z0-9_]+)["\']', block, re.IGNORECASE):
                    c = pm.group(1).strip()
                    if c and not c.startswith("SAP__") and not c.startswith("@") and c not in cols:
                        cols.append(c)
                if cols:
                    return cols

    # 2. General Property extraction if block matching did not return
    for pm in re.finditer(r'<[a-zA-Z0-9_:]*Property\b[\s\S]*?\bName=["\']([a-zA-Z0-9_]+)["\']', xml_text, re.IGNORECASE):
        c = pm.group(1).strip()
        if c and not c.startswith("SAP__") and not c.startswith("@") and c not in cols:
            cols.append(c)
            
    return cols

# Probing helper to discover schema columns of any view
async def probe_view_columns(base_host: str, space_id: str, view_name: str, bearer_token: str) -> List[str]:
    cache_key = f"{space_id}/{view_name}"
    if cache_key in view_columns_cache and view_columns_cache[cache_key]:
        return view_columns_cache[cache_key]

    headers_json = {
        "Authorization": f"Bearer {bearer_token}", 
        "Accept": "application/json",
        "User-Agent": "Datasphere-Agent/5.0"
    }
    headers_xml = {
        "Authorization": f"Bearer {bearer_token}", 
        "Accept": "application/xml, text/xml, */*",
        "User-Agent": "Datasphere-Agent/5.0"
    }

    test_urls = [
        f"{base_host}/api/v1/datasphere/consumption/relational/{space_id}/{view_name}/{view_name}",
        f"{base_host}/api/v1/datasphere/consumption/relational/{space_id}/{view_name}",
        f"{base_host}/api/v1/datasphere/consumption/relational/{space_id}/{view_name.upper()}/{view_name.upper()}",
        f"{base_host}/api/v1/datasphere/consumption/relational/{space_id}/{view_name.upper()}",
        f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id}/{view_name}/{view_name}",
        f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id}/{view_name}",
        f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id}/{view_name.upper()}/{view_name.upper()}",
        f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id}/{view_name.upper()}"
    ]

    metadata_urls = [
        f"{base_host}/api/v1/datasphere/consumption/relational/{space_id}/{view_name}/$metadata",
        f"{base_host}/api/v1/datasphere/consumption/relational/{space_id}/{view_name.upper()}/$metadata",
        f"{base_host}/api/v1/datasphere/consumption/relational/{space_id}/$metadata",
        f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id}/{view_name}/$metadata",
        f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id}/{view_name.upper()}/$metadata",
        f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id}/$metadata"
    ]

    seen_urls = set()
    unique_test_urls = []
    for u in test_urls:
        if u not in seen_urls:
            seen_urls.add(u)
            unique_test_urls.append(u)

    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        # Phase 1: Try JSON data endpoints with $top=1
        for url in unique_test_urls:
            try:
                res = await client.get(url, headers=headers_json, params={"$top": "1"})
                if res.status_code == 200:
                    payload = res.json()
                    val = payload.get("value", payload.get("d", {}).get("results", []))
                    if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                        if "url" in val[0] and "name" in val[0] and len(val[0].keys()) <= 3:
                            entity_url = f"{url.rstrip('/')}/{val[0].get('url', view_name)}"
                            res_e = await client.get(entity_url, headers=headers_json, params={"$top": "1"})
                            if res_e.status_code == 200:
                                p_e = res_e.json()
                                val_e = p_e.get("value", p_e.get("d", {}).get("results", []))
                                if isinstance(val_e, list) and len(val_e) > 0 and isinstance(val_e[0], dict):
                                    cols = [k for k in val_e[0].keys() if not k.startswith("@") and not k.startswith("__")]
                                    view_columns_cache[cache_key] = cols
                                    view_working_endpoint_cache[cache_key] = entity_url
                                    logger.info(f"Discovered {len(cols)} columns for {cache_key}: {cols[:6]}...")
                                    asyncio.create_task(rag_service.index_view_metadata(space_id, view_name, cols, gemini_key=GEMINI_API_KEY, openai_key=OPENAI_API_KEY))
                                    return cols
                            continue
                        cols = [k for k in val[0].keys() if not k.startswith("@") and not k.startswith("__")]
                        view_columns_cache[cache_key] = cols
                        view_working_endpoint_cache[cache_key] = url
                        logger.info(f"Discovered {len(cols)} columns for {cache_key}: {cols[:6]}...")
                        asyncio.create_task(rag_service.index_view_metadata(space_id, view_name, cols, gemini_key=GEMINI_API_KEY, openai_key=OPENAI_API_KEY))
                        return cols
            except Exception:
                pass

        # Phase 2: If table has 0 rows, extract columns directly from $metadata XML
        for m_url in metadata_urls:
            try:
                res_m = await client.get(m_url, headers=headers_xml)
                if res_m.status_code == 200 and "<" in res_m.text:
                    cols = extract_columns_from_metadata_xml(res_m.text, view_name)
                    if cols:
                        view_columns_cache[cache_key] = cols
                        logger.info(f"Discovered {len(cols)} columns from $metadata for {cache_key}: {cols[:6]}...")
                        return cols
            except Exception:
                pass

    # Phase 3: Fallback to sibling view cache with matching prefix (e.g. AGENT_KPI1 inherits from AGENT_KPI)
    v_clean = view_name.lower().rstrip("0123456789_")
    for k, cached_cols in view_columns_cache.items():
        if k.startswith(f"{space_id}/") and cached_cols:
            k_view = k.split("/", 1)[1].lower().rstrip("0123456789_")
            if v_clean == k_view or k_view.startswith(v_clean) or v_clean.startswith(k_view):
                view_columns_cache[cache_key] = cached_cols
                logger.info(f"Inherited {len(cached_cols)} columns for {cache_key} from sibling {k}")
                return cached_cols

    return []

# Automatically Discover All Exposed Views in a Datasphere Space via Service Catalog & Metadata
async def discover_space_views(base_host: str, space_id: str, bearer_token: str, search_hint: Optional[str] = None) -> List[str]:
    # Check cache first
    if space_id in space_views_cache and space_views_cache[space_id]:
        cached = space_views_cache[space_id]
        if search_hint:
            matched = find_views_matching_pattern(search_hint, cached)
            if matched:
                return deduplicate_view_names(cached)
            # If search_hint didn't match anything in cache, query live endpoints to pick up new deployments!
        else:
            return deduplicate_view_names(cached)

    common_hdrs = {
        "Authorization": f"Bearer {bearer_token}",
        "Accept": "application/json, application/xml, text/xml, */*",
        "User-Agent": "Datasphere-Agent/5.0"
    }

    endpoints_to_query = [
        # OData Consumption Metadata
        (f"{base_host}/api/v1/datasphere/consumption/relational/{space_id}/$metadata", common_hdrs),
        (f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id}/$metadata", common_hdrs),
        (f"{base_host}/api/v1/datasphere/consumption/relational/{space_id.upper()}/$metadata", common_hdrs),
        (f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id.upper()}/$metadata", common_hdrs),
        (f"{base_host}/api/v1/datasphere/consumption/relational/{space_id.lower()}/$metadata", common_hdrs),
        (f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id.lower()}/$metadata", common_hdrs),

        # OData Service Documents
        (f"{base_host}/api/v1/datasphere/consumption/relational/{space_id}/", common_hdrs),
        (f"{base_host}/api/v1/datasphere/consumption/relational/{space_id}", common_hdrs),
        (f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id}/", common_hdrs),
        (f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id}", common_hdrs),
        (f"{base_host}/api/v1/datasphere/consumption/relational/{space_id.upper()}/", common_hdrs),
        (f"{base_host}/api/v1/datasphere/consumption/relational/{space_id.upper()}", common_hdrs),
        (f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id.upper()}/", common_hdrs),
        (f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id.upper()}", common_hdrs),

        # Datasphere Catalog APIs
        (f"{base_host}/api/v1/datasphere/catalog/assets", common_hdrs),
        (f"{base_host}/api/v1/datasphere/catalog/v1/assets", common_hdrs),
        (f"{base_host}/api/v1/datasphere/catalog/assets?space={space_id}", common_hdrs),
        (f"{base_host}/api/v1/datasphere/catalog/assets?spaceId={space_id}", common_hdrs),
        (f"{base_host}/api/v1/datasphere/catalog/assets?space={space_id.upper()}", common_hdrs),
        (f"{base_host}/api/v1/datasphere/catalog/assets?filter=spaceId eq '{space_id}'", common_hdrs),
        (f"{base_host}/api/v1/datasphere/catalog/assets?$filter=space eq '{space_id}'", common_hdrs),

        # Data Builder & Space Repository APIs
        (f"{base_host}/dwaas-core/api/v1/spaces/{space_id}/views", common_hdrs),
        (f"{base_host}/dwaas-core/api/v1/spaces/{space_id}/artifacts", common_hdrs),
        (f"{base_host}/dwaas-core/api/v1/spaces/{space_id}/entities", common_hdrs),
        (f"{base_host}/dwaas-core/api/v1/spaces/{space_id}/objects", common_hdrs),
        (f"{base_host}/dwaas-core/api/v1/spaces/{space_id}/files", common_hdrs),
        (f"{base_host}/dwaas-core/api/v1/spaces/{space_id.upper()}/views", common_hdrs),
        (f"{base_host}/dwaas-core/api/v1/spaces/{space_id.upper()}/artifacts", common_hdrs),
        (f"{base_host}/dwaas-core/api/v1/spaces/{space_id.upper()}/entities", common_hdrs),
        (f"{base_host}/dwaas-core/api/v1/spaces/{space_id.upper()}/objects", common_hdrs),
        (f"{base_host}/api/v1/datasphere/spaces/{space_id}/views", common_hdrs),
        (f"{base_host}/api/v1/datasphere/spaces/{space_id}/tables", common_hdrs),
        (f"{base_host}/api/v1/datasphere/spaces/{space_id}/entities", common_hdrs),
        (f"{base_host}/api/v1/datasphere/spaces/{space_id}/artifacts", common_hdrs),
        (f"{base_host}/api/v1/datasphere/spaces/{space_id.upper()}/views", common_hdrs),
        (f"{base_host}/api/v1/datasphere/spaces/{space_id.upper()}/artifacts", common_hdrs),
        (f"{base_host}/api/v1/datasphere/repository/spaces/{space_id}/artifacts", common_hdrs),
        (f"{base_host}/api/v1/datasphere/repository/spaces/{space_id.upper()}/artifacts", common_hdrs),
        (f"{base_host}/api/v1/datasphere/repository/artifacts?space={space_id}", common_hdrs),
        (f"{base_host}/api/v1/datasphere/repository/artifacts?spaceId={space_id}", common_hdrs)
    ]

    discovered: List[str] = []
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        async def fetch_url(url: str, hdrs: Dict[str, str]):
            try:
                res = await client.get(url, headers=hdrs)
                if res.status_code == 200:
                    found = []
                    if "<" in res.text:
                        found.extend(extract_views_from_xml(res.text))
                    try:
                        payload = res.json()
                        found.extend(extract_views_from_json(payload))
                    except Exception:
                        pass
                    return found
            except Exception as e:
                logger.warning(f"Catalog probe error on {url}: {e}")
            return []

        tasks = [fetch_url(url, hdrs) for url, hdrs in endpoints_to_query]
        results = await asyncio.gather(*tasks)
        for r in results:
            discovered.extend(r)

    # Dynamic word probe against Datasphere if search_hint is present
    if search_hint:
        hint_lower = search_hint.lower().strip()
        extracted_terms = []
        for pat in [
            r'view\s+name\s+(?:has|contains|like|is|matching|with|having)\s+["\']?([^"\',.?;]+)["\']?',
            r'views\s+(?:have|contain|like|matching|with|having)\s+["\']?([^"\',.?;]+)["\']?',
            r'related\s+to\s+["\']?([^"\',.?;]+)["\']?',
            r'regarding\s+["\']?([^"\',.?;]+)["\']?',
            r'about\s+["\']?([^"\',.?;]+)["\']?'
        ]:
            for m in re.finditer(pat, hint_lower):
                raw = m.group(1).strip()
                clean = re.sub(r'\b(view|views|table|tables|data|records|dataset|space|please|get|me|the)\b', '', raw).strip()
                if clean and len(clean) >= 2:
                    extracted_terms.append(clean)
        
        stop_words = {"get", "the", "data", "where", "view", "views", "name", "has", "contains", "like", "with", "from", "show", "all", "select", "records", "give", "display", "please", "fetch"}
        salient_words = [w for w in re.findall(r'\b[a-zA-Z0-9_]{3,}\b', hint_lower) if w not in stop_words]
        all_probe_terms = list(set(extracted_terms + salient_words))

        # Split compound words (e.g. "grossmargin" -> "gross", "margin", "gross_margin")
        expanded_terms = list(all_probe_terms)
        for term in all_probe_terms:
            t_clean = term.replace(" ", "_").strip("_")
            # Try subword splits for words >= 6 chars
            for i in range(3, len(t_clean) - 2):
                p1, p2 = t_clean[:i], t_clean[i:]
                if len(p1) >= 3 and len(p2) >= 3:
                    expanded_terms.extend([f"{p1}_{p2}", p1, p2])

        expanded_terms = list(set(expanded_terms))

        sap_prefixes = [
            "", "zsa_gv_", "zsa_gv_sales_", "zsa_", "zfi_fa_", "zfi_", "zsd_", "zmm_", "z_", 
            "gv_", "cv_", "iv_", "dv_", "sales_", "purchase_", "agent_", "supplier_", "kpi_", "gm_"
        ]
        suffixes = [
            "", "s", "_kpi", "_kpis", "_view", "_v", "_table", "_data", 
            "_1_ds", "_card_1_ds", "_score_card_1_ds", "_test_table_new_v", "_test_table_new",
            "_performance", "_summary", "_details", "_margin"
        ]

        probe_candidates = []
        for term in expanded_terms:
            t_clean = term.replace(" ", "_").strip("_")
            for pfx in sap_prefixes:
                for sfx in suffixes:
                    cand = f"{pfx}{t_clean}{sfx}".strip("_")
                    if cand and len(cand) >= 3:
                        probe_candidates.extend([cand, cand.upper(), cand.lower(), cand.title()])

        probe_candidates = deduplicate_view_names(probe_candidates)

        probe_tasks = [probe_view_columns(base_host, space_id, cand, bearer_token) for cand in probe_candidates]
        probe_results = await asyncio.gather(*probe_tasks)
        for idx, cols in enumerate(probe_results):
            if cols:
                cand_name = probe_candidates[idx]
                if cand_name not in discovered:
                    discovered.append(cand_name)
                    logger.info(f"Live confirmed Datasphere View '{cand_name}' with {len(cols)} columns.")

    # Fallback to previously probed / known views in cache
    for key in view_columns_cache.keys():
        if key.startswith(f"{space_id}/"):
            v_name = key.split("/", 1)[1]
            discovered.append(v_name)

    existing = space_views_cache.get(space_id, [])
    merged = deduplicate_view_names(existing + discovered)

    if merged:
        space_views_cache[space_id] = merged
        logger.info(f"Discovered/Updated {len(merged)} unique views in space '{space_id}': {merged}")

    return merged

def find_views_matching_pattern(user_prompt: str, candidate_views: List[str]) -> List[str]:
    """Matches any view(s) where the view name contains the user's search terms/phrases dynamically (Zero hardcoding)."""
    if not candidate_views:
        return []
    
    prompt_lower = user_prompt.lower().strip()
    matched_views: List[str] = []

    # 1. Check for explicit pattern phrases: "where view name has X", "related to X", "about X", "views with X", etc.
    view_pattern_regex = [
        r'view\s+name\s+(?:has|contains|like|is|matching|with|having)\s+["\']?([^"\',.?;]+)["\']?',
        r'views\s+(?:have|contain|like|matching|with|having)\s+["\']?([^"\',.?;]+)["\']?',
        r'related\s+to\s+["\']?([^"\',.?;]+)["\']?',
        r'regarding\s+["\']?([^"\',.?;]+)["\']?',
        r'about\s+["\']?([^"\',.?;]+)["\']?',
        r'views?\s+named\s+["\']?([^"\',.?;]+)["\']?',
        r'from\s+(?:all\s+)?views?\s+(?:with|having|like|matching|has|containing)\s+["\']?([^"\',.?;]+)["\']?',
        r'of\s+(?:the\s+)?(?:given\s+)?(?:view\s+|table\s+)?["\']?([a-zA-Z0-9_]+)["\']?\s+(?:view|table)',
        r'(?:the\s+)?view\s+["\']?([a-zA-Z0-9_]+)["\']?'
    ]

    target_phrases: List[str] = []
    for pat in view_pattern_regex:
        for m in re.finditer(pat, prompt_lower):
            raw_phrase = m.group(1).strip()
            cleaned_phrase = re.sub(r'\b(view|views|table|tables|data|records|dataset|space|please|get|me|the)\b', '', raw_phrase).strip()
            if cleaned_phrase and len(cleaned_phrase) >= 2:
                target_phrases.append(cleaned_phrase)

    # Check each extracted phrase against all candidate views
    for phrase in target_phrases:
        phrase_clean = phrase.replace("_", "").replace(" ", "").lower()
        phrase_tokens = [t for t in re.findall(r'[a-zA-Z0-9]{2,}', phrase) if t not in ("the", "and", "or", "for", "with", "get", "data", "where", "related", "about")]
        
        for v in candidate_views:
            v_clean = v.lower().replace("_", "").replace(" ", "")
            # Check continuous substring match (e.g. "grossmargin" in "grossmarginkpi" or "purchase" in "purchasekpis")
            if phrase_clean and (phrase_clean in v_clean or v_clean in phrase_clean):
                matched_views.append(v)
            # Check token match (all keywords present in view name)
            elif phrase_tokens and all(tok in v_clean for tok in phrase_tokens):
                matched_views.append(v)

    matched_views = deduplicate_view_names(matched_views)
    if matched_views:
        logger.info(f"Dynamically matched {len(matched_views)} unique view(s) for target phrases {target_phrases}: {matched_views}")
        return matched_views

    # 2. General token substring matching across all views in space
    prompt_tokens = set(re.findall(r'\b[a-zA-Z0-9_]{3,}\b', prompt_lower))
    stop_words = {
        "get", "the", "data", "where", "view", "views", "name", "has", "contains", "like", 
        "with", "from", "show", "all", "select", "records", "give", "display", "please", 
        "fetch", "related", "regarding", "about", "based", "into", "over", "under", "having", 
        "within", "across", "details", "info", "information", "dataset", "table", "tables", "space"
    }
    salient_tokens = [t for t in prompt_tokens if t not in stop_words]

    for v in candidate_views:
        v_clean = v.lower().replace("_", "")
        # If any salient token from user's prompt is a substring of the view name
        if any(tok in v_clean or v_clean in tok for tok in salient_tokens):
            matched_views.append(v)

    return deduplicate_view_names(matched_views)

def extract_view_names_from_prompt(user_prompt: str) -> List[str]:
    prompt_lower = user_prompt.lower().strip()
    extracted: List[str] = []
    
    # Check for explicit exact view name patterns
    view_pattern_regex = [
        r'view\s+name\s+(?:is|=|==)\s+["\']?([a-zA-Z0-9_]+)["\']?',
        r'from\s+(?:the\s+)?view\s+["\']?([a-zA-Z0-9_]+)["\']?',
        r'of\s+(?:the\s+)?view\s+["\']?([a-zA-Z0-9_]+)["\']?',
        r'in\s+(?:the\s+)?view\s+["\']?([a-zA-Z0-9_]+)["\']?',
        r'table\s+["\']?([a-zA-Z0-9_]+)["\']?'
    ]
    for pat in view_pattern_regex:
        for m in re.finditer(pat, prompt_lower):
            v_cand = m.group(1).strip()
            if v_cand and v_cand not in (
                "has", "contains", "like", "with", "where", "data", "records", 
                "the", "all", "is", "for", "fore", "from", "show", "get", "give", "name", "dataset"
            ):
                orig_m = re.search(re.escape(v_cand), user_prompt, re.IGNORECASE)
                val_to_add = orig_m.group(0) if orig_m else v_cand
                if val_to_add not in extracted:
                    extracted.append(val_to_add)
                    
    return extracted

def has_explicit_view_name_intent(prompt: str) -> bool:
    prompt_lower = prompt.lower().strip()
    return bool(re.search(
        r'\b(?:view\s+name|views\s+named|views\s+have|view\s+has|view\s+contains|view\s+like|from\s+view|view\s+[a-zA-Z0-9_]{2,})\b',
        prompt_lower
    ))

# Semantic View Matcher & LLM Router (Picks the right view without user guessing)
async def route_prompt_to_views(
    user_prompt: str, 
    space_id: str, 
    candidate_views: List[str], 
    base_host: str, 
    bearer_token: str
) -> List[str]:
    if not candidate_views:
        return []

    # 1. First check dynamic view name pattern matches (e.g. "where view name has supplier score")
    name_matched_views = find_views_matching_pattern(user_prompt, candidate_views)
    if name_matched_views:
        logger.info(f"Direct View Name Pattern Match: '{user_prompt}' -> {name_matched_views}")
        return name_matched_views

    prompt_lower = user_prompt.lower()
    
    # 2. Probe columns for all candidate views in parallel
    probe_tasks = [probe_view_columns(base_host, space_id, v, bearer_token) for v in candidate_views]
    probe_results = await asyncio.gather(*probe_tasks)
    
    view_scores: List[Tuple[str, int, List[str]]] = []
    
    # Extract salient terms from user prompt and expand compound subwords
    stop_words = {"get", "the", "data", "where", "view", "views", "name", "has", "contains", "like", "with", "from", "show", "all", "select", "records", "give", "display", "please", "fetch"}
    raw_tokens = set(re.findall(r'\b[a-zA-Z0-9_]{3,}\b', prompt_lower)) - stop_words
    prompt_tokens = set(raw_tokens)
    for tok in raw_tokens:
        for i in range(3, len(tok) - 2):
            p1, p2 = tok[:i], tok[i:]
            if len(p1) >= 3 and len(p2) >= 3:
                prompt_tokens.add(p1)
                prompt_tokens.add(p2)
                prompt_tokens.add(f"{p1}_{p2}")
    
    for idx, v in enumerate(candidate_views):
        cols = probe_results[idx] or []
        score = 0
        matched_cols = []
        
        # View name relevance
        v_clean = v.lower().replace("_", "")
        for t in prompt_tokens:
            t_clean = t.replace("_", "")
            if t_clean in v_clean or v_clean in t_clean:
                score += 8

        # Column name relevance
        for c in cols:
            c_clean = c.lower().replace("_", "")
            for t in prompt_tokens:
                t_clean = t.replace("_", "")
                if t_clean in c_clean or c_clean in t_clean:
                    score += 4
                    matched_cols.append(c)
            if any(k in c_clean for k in ["kpiname", "metric", "indicator", "kpi", "name", "description"]):
                score += 2

        view_scores.append((v, score, matched_cols))

    view_scores.sort(key=lambda x: x[1], reverse=True)
    logger.info(f"Dynamic View Routing scores for '{user_prompt}': {[(x[0], x[1]) for x in view_scores]}")

    if view_scores and view_scores[0][1] >= 2:
        top_score = view_scores[0][1]
        top_views = [x[0] for x in view_scores if x[1] >= top_score - 2 and x[1] >= 2]
        if top_views:
            return top_views
        return [view_scores[0][0]]

    # If no view scored above 2, return candidate views with KPI/dimension columns as fallback
    kpi_views = [v for idx, v in enumerate(candidate_views) if any(k in c.lower() for c in (probe_results[idx] or []) for k in ["kpi", "name", "desc", "metric"])]
    if kpi_views:
        return kpi_views

    return candidate_views[:1] if candidate_views else []

# Conversational Context & Entity Follow-Up Resolution (Zero Data Leakage)
def resolve_conversational_follow_up(
    user_prompt: str,
    session_id: Optional[str]
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Resolves follow-up references ('1st customer', 'that plant', 'same like this for gross margin', 'break down their orders') using private local session state."""
    if not session_id or session_id not in conversation_sessions:
        return user_prompt, None

    session = conversation_sessions[session_id]
    prev_data = session.get("last_data", [])
    prev_views = session.get("last_views", [])
    prev_prompt = session.get("last_prompt", "")
    
    prompt_lower = user_prompt.lower().strip()
    
    # 1. Follow-up "same like this for ...", "same for ...", "do the same for ...", "now for ...", "what about for ..."
    same_patterns = [
        r'\b(?:same\s+like\s+this\s+for|same\s+for|do\s+(?:the\s+)?same\s+for|now\s+get\s+me\s+same\s+(?:like\s+this\s+)?for|now\s+for|what\s+about\s+(?:for\s+)?|also\s+for|similar\s+for|repeat\s+for)\s+(?:the\s+)?(.+)',
        r'\b(?:same\s+like\s+this|same\s+thing|do\s+the\s+same)\b'
    ]
    for s_pat in same_patterns:
        s_match = re.search(s_pat, prompt_lower)
        if s_match:
            target_entity = s_match.group(1).strip() if s_match.groups() else ""
            augmented_prompt = f"get me only the data for {target_entity}" if target_entity else user_prompt
            context_info = {
                "follow_up_type": "same_operation",
                "previous_views": prev_views,
                "previous_prompt": prev_prompt,
                "target_entity": target_entity
            }
            logger.info(f"Conversational Follow-Up Resolved: '{user_prompt}' -> Augmented: '{augmented_prompt}' with View(s) {prev_views}")
            return augmented_prompt, context_info

    if not prev_data:
        if prev_views:
            return user_prompt, {"previous_views": prev_views}
        return user_prompt, None
    
    # Check for ordinal references: "1st customer", "first one", "2nd customer", "top 1", "that customer"
    ordinal_map = {
        "1st": 0, "first": 0, "top 1": 0, "top one": 0, "number 1": 0, "#1": 0,
        "2nd": 1, "second": 1, "top 2": 1, "number 2": 1, "#2": 1,
        "3rd": 2, "third": 2, "number 3": 2, "#3": 2,
        "4th": 3, "fourth": 3, "number 4": 3,
        "5th": 4, "fifth": 4, "number 5": 4,
        "last": -1, "last one": -1
    }

    target_index = None
    for pattern, idx in ordinal_map.items():
        if re.search(rf'\b{re.escape(pattern)}\b', prompt_lower):
            target_index = idx
            break

    # If asking "explain that customer" or "show their orders" without ordinal, default to 1st record
    if target_index is None and any(p in prompt_lower for p in ["their orders", "explain customer", "that customer", "this customer", "their details", "break down"]):
        target_index = 0

    if target_index is not None and abs(target_index) < len(prev_data):
        target_row = prev_data[target_index]
        
        # Find the primary entity identifier column dynamically
        key_col = None
        key_val = None
        
        # Priority 1: Any column with ID / Name / Number / Code / Key
        for col, val in target_row.items():
            c_clean = col.lower().replace("_", "")
            if val is not None and str(val).strip() != "" and any(term in c_clean for term in ["name", "code", "id", "num", "key"]):
                key_col = col
                key_val = val
                break

        # Priority 2: Any non-numeric descriptive text column
        if not key_col:
            for col, val in target_row.items():
                if isinstance(val, str) and val.strip() and not col.lower().startswith("sum") and not col.lower().startswith("avg"):
                    key_col = col
                    key_val = val
                    break

        if key_col and key_val:
            augmented_prompt = f"{user_prompt} WHERE {key_col} IS '{key_val}'"
            context_info = {
                "referenced_entity": f"{key_col} = '{key_val}'",
                "entity_key": key_col,
                "entity_value": key_val,
                "target_row": target_row,
                "previous_views": prev_views
            }
            logger.info(f"Conversational Context Resolved: '{user_prompt}' -> Filter: [{key_col} = '{key_val}']")
            return augmented_prompt, context_info

    if prev_views:
        return user_prompt, {"previous_views": prev_views}
    return user_prompt, None

def detect_foreign_key_relations(view_schemas: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    relations = []
    view_names = list(view_schemas.keys())
    
    # Priority keywords for primary business entity join keys
    high_priority_terms = ["company", "plant", "customer", "material", "doc", "salesorg", "fiscalyear", "code", "id", "num", "key", "bukrs", "werks", "kunnr", "matnr", "belnr"]
    exact_code_terms = ["companycode", "salesorder", "customernumber", "materialnumber", "plant", "fiscalyear", "customer", "material", "order"]
    low_priority_terms = ["breach", "flag", "status", "indicator", "warning", "critical", "type", "mark", "valid", "name"]

    for i in range(len(view_names)):
        for j in range(i + 1, len(view_names)):
            v1 = view_names[i]
            v2 = view_names[j]
            cols1 = view_schemas[v1]
            cols2 = view_schemas[v2]
            
            scored_keys = []
            for c1 in cols1:
                c1_clean = c1.lower().replace("_", "").replace(" ", "")
                for c2 in cols2:
                    c2_clean = c2.lower().replace("_", "").replace(" ", "")
                    is_match = (c1.lower() == c2.lower() or c1_clean == c2_clean)
                    is_fuzzy = not is_match and (c1_clean in c2_clean or c2_clean in c1_clean) and any(term in c1_clean for term in high_priority_terms)
                    
                    if is_match or is_fuzzy:
                        score = 100 if is_match else 50
                        if any(t in c1_clean for t in exact_code_terms):
                            score += 80
                        elif any(t in c1_clean for t in high_priority_terms):
                            score += 50
                        if any(t in c1_clean for t in low_priority_terms):
                            score -= 90
                        
                        scored_keys.append((score, {
                            "col1": c1,
                            "col2": c2,
                            "key_name": c1 if is_match else f"{c1} ~ {c2}"
                        }))
            
            if scored_keys:
                scored_keys.sort(key=lambda x: x[0], reverse=True)
                sorted_keys = [k[1] for k in scored_keys]
                relations.append({
                    "view_a": v1,
                    "view_b": v2,
                    "join_keys": sorted_keys
                })
    return relations

def evaluate_condition_value(row_val: Any, op: str, target_val: Any, is_num: bool) -> bool:
    if row_val is None:
        return False
    if is_num:
        try:
            num_clean = float(str(row_val).replace(",", "").replace("$", "").replace("€", "").replace("₹", "").strip())
            target_num = float(target_val)
            if op == ">": return num_clean > target_num
            if op == ">=": return num_clean >= target_num
            if op == "<": return num_clean < target_num
            if op == "<=": return num_clean <= target_num
            if op == "=": return abs(num_clean - target_num) < 0.0001
            if op == "!=": return abs(num_clean - target_num) >= 0.0001
        except Exception:
            return False
    else:
        str_val = str(row_val).strip().lower()
        target_str = str(target_val).strip().lower()
        if op == "=": return str_val == target_str
        if op == "!=": return str_val != target_str
        if op == "LIKE": return target_str in str_val
# ==============================================================================
# SECTION 6: IN-MEMORY RELATIONAL SQL COMPILER & EXECUTION ENGINE
# ==============================================================================

def infer_column_sqlite_type(sample_values: List[Any]) -> str:
    """
    Infers the most appropriate SQLite column data type (INTEGER, REAL, or TEXT)
    from a sample list of values returned by SAP Datasphere.
    
    Logic:
    - If all non-null values can be converted to integer without decimal loss -> INTEGER
    - If all non-null values can be converted to floating-point number -> REAL
    - Otherwise -> TEXT
    """
    valid_vals = [v for v in sample_values if v is not None and str(v).strip() != ""]
    if not valid_vals:
        return "TEXT"

    is_int = True
    is_float = True

    for v in valid_vals[:100]:
        s = str(v).replace(",", "").replace("$", "").replace("€", "").replace("₹", "").replace("%", "").strip()
        try:
            f = float(s)
            if not f.is_integer():
                is_int = False
        except (ValueError, TypeError):
            is_int = False
            is_float = False
            break

    if is_int:
        return "INTEGER"
    if is_float:
        return "REAL"
    return "TEXT"


def evaluate_condition_value(row_val: Any, op: str, target_val: Any, is_num: bool) -> bool:
    """
    Evaluates a single relational filter condition in Python memory.
    Acts as a resilient fallback engine if SQLite syntax or type casting fails.
    
    Parameters:
    - row_val: The raw value in the current data row.
    - op: The comparison operator ('>', '>=', '<', '<=', '=', '!=', 'LIKE').
    - target_val: The target value extracted from the user's prompt.
    - is_num: True if numerical comparison should be performed, False for string matching.
    """
    if row_val is None:
        return False
    if is_num:
        try:
            num_clean = float(str(row_val).replace(",", "").replace("$", "").replace("€", "").replace("₹", "").strip())
            target_num = float(target_val)
            if op == ">": return num_clean > target_num
            if op == ">=": return num_clean >= target_num
            if op == "<": return num_clean < target_num
            if op == "<=": return num_clean <= target_num
            if op == "=": return abs(num_clean - target_num) < 0.0001
            if op == "!=": return abs(num_clean - target_num) >= 0.0001
        except Exception:
            return False
    else:
        str_val = str(row_val).strip().lower()
        target_str = str(target_val).strip().lower()
        if op == "=": return str_val == target_str
        if op == "!=": return str_val != target_str
        if op == "LIKE": return target_str in str_val
    return True


def execute_multi_table_sql(
    multi_table_data: Dict[str, List[Dict[str, Any]]], 
    sql_query: str,
    selected_fields: Optional[List[str]] = None,
    structured_conditions: Optional[List[Dict[str, Any]]] = None,
    view_schemas: Optional[Dict[str, List[str]]] = None
) -> List[Dict[str, Any]]:
    """
    Executes compiled ANSI SQL queries across multi-view SAP Datasphere datasets
    using an isolated, in-memory SQLite database.
    
    Key Workflow:
    1. Ensures all tables (t1, t2, ...) are created in SQLite even if a view returns 0 rows.
    2. Case-insensitively deduplicates column names to avoid SQLite duplicate column errors.
    3. Dynamically infers data types and creates tables (t1, t2, ...) and friendly view aliases.
    4. Populates rows with sanitized numerical/text conversions.
    5. Executes the relational SQL query (with LEFT JOINs, COALESCE filters, and aggregations).
    6. Automatically swaps driving table if t1 has 0 rows but t2 has populated records.
    7. Fallback: If SQLite fails, gracefully executes using Python relational filter engine.
    """
    if not multi_table_data and not view_schemas:
        return []

    results = []
    try:
        # Step 1: Initialize in-memory SQLite instance
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        all_target_views = list(view_schemas.keys()) if view_schemas else list(multi_table_data.keys())
        table_row_counts = {}

        for idx, view_name in enumerate(all_target_views, start=1):
            raw_data = multi_table_data.get(view_name, [])
            primary_name = f't{idx}'
            table_row_counts[primary_name] = len(raw_data)
            
            # Step 2: Determine column names and types for this view
            unique_cols = []
            seen_c = set()
            col_types = {}

            if raw_data:
                for c in raw_data[0].keys():
                    c_clean = str(c).strip()
                    if c_clean.lower() not in seen_c:
                        seen_c.add(c_clean.lower())
                        unique_cols.append(c_clean)
                for c in unique_cols:
                    col_vals = [r.get(c) for r in raw_data[:200]]
                    col_types[c] = infer_column_sqlite_type(col_vals)
            elif view_schemas and view_name in view_schemas and view_schemas[view_name]:
                for c in view_schemas[view_name]:
                    c_clean = str(c).strip()
                    if c_clean.lower() not in seen_c:
                        seen_c.add(c_clean.lower())
                        unique_cols.append(c_clean)
                        col_types[c_clean] = "TEXT"
            else:
                unique_cols = ["ID"]
                col_types["ID"] = "TEXT"

            # Step 3: Create table with typed definitions (t1, t2, ...) and case-insensitive text collation
            col_defs = ", ".join([
                f'"{c}" TEXT COLLATE NOCASE' if col_types.get(c, "TEXT") == "TEXT" else f'"{c}" {col_types.get(c, "TEXT")}'
                for c in unique_cols
            ])
            cursor.execute(f'CREATE TABLE {primary_name} ({col_defs})')

            # Step 4: Clean and insert records into table
            if raw_data:
                placeholders = ", ".join(["?"] * len(unique_cols))
                insert_sql = f'INSERT INTO {primary_name} VALUES ({placeholders})'
                rows_to_insert = []
                for r in raw_data:
                    row_vals = []
                    for c in unique_cols:
                        v = r.get(c)
                        if v is not None and col_types.get(c) in ("INTEGER", "REAL"):
                            try:
                                v_clean = str(v).replace(",", "").replace("$", "").replace("€", "").replace("₹", "").strip()
                                row_vals.append(int(float(v_clean)) if col_types[c] == "INTEGER" else float(v_clean))
                            except (ValueError, TypeError):
                                row_vals.append(v)
                        else:
                            row_vals.append(v)
                    rows_to_insert.append(row_vals)

                cursor.executemany(insert_sql, rows_to_insert)

            # Step 5: Register view aliases for flexible SQL referencing
            table_aliases = [
                f'datasphere_table' if idx == 1 else f'datasphere_table_{idx}',
                f'table_{idx}',
                f'"{view_name}"',
                f'v_{re.sub(r"[^a-zA-Z0-9_]", "_", view_name).lower()}'
            ]
            for alias in table_aliases:
                try:
                    cursor.execute(f'CREATE VIEW {alias} AS SELECT * FROM {primary_name}')
                except Exception:
                    pass

        conn.commit()

        # Step 6: Execute compiled SQL
        cursor.execute(sql_query)
        results = [dict(row) for row in cursor.fetchall()]

        # Step 6b: If query returned 0 rows because t1 is empty but t2 has rows, adapt driving table
        if len(results) == 0 and len(all_target_views) > 1 and table_row_counts.get("t1", 0) == 0:
            populated_table = next((t_name for t_name, count in table_row_counts.items() if count > 0), None)
            if populated_table and populated_table != "t1":
                # Swap t1 and the populated table in the SQL query
                swapped_sql = re.sub(r'\bFROM\s+t1\s+LEFT\s+JOIN\s+' + populated_table, f'FROM {populated_table} LEFT JOIN t1', sql_query, flags=re.IGNORECASE)
                if swapped_sql == sql_query:
                    swapped_sql = re.sub(r'\bFROM\s+t1\b', f'FROM {populated_table}', sql_query, flags=re.IGNORECASE)
                try:
                    cursor.execute(swapped_sql)
                    retry_results = [dict(row) for row in cursor.fetchall()]
                    if retry_results:
                        logger.info(f"Re-executed SQL driving from populated table '{populated_table}': {len(retry_results)} rows returned.")
                        results = retry_results
                except Exception as retry_err:
                    logger.debug(f"Retry query error: {retry_err}")

        conn.close()
        logger.info(f"Executed Multi-Table SQL: '{sql_query}' -> {len(results)} rows returned.")
    except Exception as sql_err:
        # Step 7: Fallback to in-memory Python relational filter engine on any SQL error
        logger.warning(f"SQL execution error on '{sql_query}': {sql_err}. Running in-memory Python relational filter fallback.")
        results = []
        
        all_views = list(multi_table_data.keys())
        # Pick the first view that actually contains data
        populated_views = [v for v in all_views if multi_table_data.get(v)]
        if populated_views:
            base_rows = multi_table_data[populated_views[0]]
            if len(populated_views) > 1:
                sec_rows = multi_table_data[populated_views[1]]
                common_keys = [k for k in base_rows[0].keys() if sec_rows and k in sec_rows[0].keys()] if base_rows and sec_rows else []
                if common_keys:
                    join_k = common_keys[0]
                    sec_map = {str(r.get(join_k)): r for r in sec_rows if r.get(join_k) is not None}
                    combined_rows = []
                    for pr in base_rows:
                        combined_r = dict(pr)
                        sec_match = sec_map.get(str(pr.get(join_k)))
                        if sec_match:
                            for k, v in sec_match.items():
                                if k not in combined_r or combined_r[k] is None:
                                    combined_r[k] = v
                        combined_rows.append(combined_r)
                    base_rows = combined_rows
        else:
            base_rows = []

        if structured_conditions:
            for r in base_rows:
                match = True
                for cond in structured_conditions:
                    col_name = cond["col"]
                    val = r.get(col_name)
                    if val is None:
                        for k, v in r.items():
                            if k.lower() == col_name.lower():
                                val = v
                                break
                    if not evaluate_condition_value(val, cond["op"], cond["target"], cond.get("is_num", False)):
                        match = False
                        break
                if match:
                    results.append(r)
        else:
            results = base_rows

    return results


# ==============================================================================
# SECTION 7: NATURAL LANGUAGE TO SQL COMPILER & PARSER
# ==============================================================================

def generate_table_info_and_descriptions(raw_data: List[Dict[str, Any]], view_name: str, space_id: str) -> Dict[str, Any]:
    """
    Generates rich column metadata, business type inferences, null counts,
    and distinct value statistics for view profiling queries.
    """
    if not raw_data:
        return {
            "view_name": view_name,
            "space_id": space_id,
            "total_records": 0,
            "total_columns": 0,
            "columns": [],
            "summary": "Dataset is empty."
        }

    total_rows = len(raw_data)
    cols = list(raw_data[0].keys())
    col_profiles = []

    for c in cols:
        vals = [r.get(c) for r in raw_data if r.get(c) is not None and str(r.get(c)).strip() != ""]
        distinct_vals = list(dict.fromkeys(vals))
        inferred_type = infer_column_sqlite_type(vals)

        c_lower = c.lower()
        if "year" in c_lower:
            biz_type = "Fiscal/Posting Year"
        elif "month" in c_lower:
            biz_type = "Posting Month"
        elif "date" in c_lower or "time" in c_lower:
            biz_type = "Date / Timestamp"
        elif "breach" in c_lower or "status" in c_lower or "flag" in c_lower or (len(distinct_vals) <= 3 and any(v in ('X', '1', '0', 'Y', 'N') for v in distinct_vals)):
            biz_type = "SAP Indicator / Flag"
        elif inferred_type in ("INTEGER", "REAL"):
            biz_type = "Numerical Metric / KPI"
        else:
            biz_type = "Text Dimension"

        friendly_name = c.replace("_", " ").title()
        col_profiles.append({
            "column_name": c,
            "friendly_name": friendly_name,
            "data_type": biz_type,
            "storage_type": inferred_type,
            "null_count": total_rows - len(vals),
            "distinct_count": len(distinct_vals),
            "sample_values": distinct_vals[:5],
            "description": f"Column '{c}' contains {len(distinct_vals)} distinct {biz_type.lower()} values."
        })

    return {
        "view_name": view_name,
        "space_id": space_id,
        "total_records": total_rows,
        "total_columns": len(cols),
        "columns": col_profiles,
        "summary": f"Table '{view_name}' in Space '{space_id}' contains {total_rows:,} records across {len(cols)} columns."
    }


def extract_where_conditions_from_prompt(
    user_prompt: str, 
    all_cols: List[str],
    col_where_map: Optional[Dict[str, str]] = None
) -> Tuple[List[str], List[str], List[Dict[str, Any]]]:
    """
    Extracts structured relational filtering conditions (WHERE clauses, OData $filter strings,
    and Python evaluation tuples) from conversational natural language prompts.
    
    Features:
    - Precedence-ordered relational operator recognition ('>=', '<=', '>', '<', '!=', 'LIKE', '=').
    - Copula / linking verb separation ('are greaterthan 40' -> operator '>' with value 40).
    - Conjunction protection (prevents greedy swallowing of trailing 'and' / 'or').
    - Fuzzy column matching with typo tolerance ('kpi_beach' -> 'KPI_Breach').
    - Multi-table COALESCE expressions for safe LEFT JOIN querying.
    """
    where_clauses: List[str] = []
    odata_filters: List[str] = []
    structured_conditions: List[Dict[str, Any]] = []
    prompt_lower = user_prompt.lower().strip()

    noise_col_words = {
        "where", "filter", "select", "and", "or", "get", "show", "data", "records", 
        "view", "views", "table", "tables", "space", "by", "each", "per", "for", 
        "group", "grouped", "breakdown", "summarize", "top", "bottom", "first", 
        "last", "highest", "lowest", "limit", "me", "the"
    }

    # Pattern 0: High-Precision Natural Language Column + Equality + Multi-word String Value
    # Example: "for kpi name as days sales outstanding", "where kpi_name is days sales outstanding", "status is pending approval"
    nl_matches = re.finditer(
        r'(?:(?:where|filter(?:ed)?\s+by|for|of|with|having|on)\s+)?'
        r'(?:the\s+)?'
        r'([a-zA-Z0-9_\s]{2,30}?)\s*'
        r'(?:\b(?:is\s+equal\s+to|equals\s+to|equal\s+to|equals|is|as|must\s+be|should\s+be|named|like|contains)\b|==|=|:=)\s*'
        r'[\'"`]?([a-zA-Z0-9_\-\s]{2,60}?)[\'"`]?'
        r'(?:\s*(?:and\s+|or\s+|order\s+by|group\s+by|limit\s+|sorted\s+|only\b|$))',
        user_prompt,
        re.IGNORECASE
    )
    for nl_m in nl_matches:
        cand_col = nl_m.group(1).strip()
        cand_val = nl_m.group(2).strip()
        if not cand_col or not cand_val:
            continue
        # Clean trailing noise words from cand_val
        for stop_w in (" and", " or", " where", " with", " by", " each", " per", " only", " data", " please"):
            if cand_val.lower().endswith(stop_w):
                cand_val = cand_val[:-len(stop_w)].strip()

        matched_c = resolve_best_matching_column(cand_col, all_cols)
        if matched_c and matched_c.lower() not in ("view", "table", "space") and cand_val.lower() not in noise_col_words:
            if not any(matched_c.lower() in wc.lower() for wc in where_clauses):
                col_expr = col_where_map.get(matched_c, f'"{matched_c}"') if col_where_map else f'"{matched_c}"'
                clean_str = cand_val.replace("'", "''")
                clean_val = clean_str.lower().strip()
                no_space_val = clean_val.replace(" ", "").replace("_", "")
                where_clauses.append(
                    f'(LOWER(TRIM(CAST({col_expr} AS TEXT))) = \'{clean_val}\' '
                    f'OR LOWER(REPLACE(REPLACE(CAST({col_expr} AS TEXT), \' \', \'\'), \'_\', \'\')) = \'{no_space_val}\' '
                    f'OR {col_expr} LIKE \'%{clean_str}%\' '
                    f'OR {col_expr} = \'{clean_str}\')'
                )
                odata_filters.append(f"{matched_c} eq '{clean_str}'")
                structured_conditions.append({"col": matched_c, "op": "=", "target": clean_str, "is_num": False})

    # Pattern 0b: Direct Preposition + Column + Multi-Word Value (e.g. "for kpi name gross margin", "where status pending approval")
    prep_val_matches = re.finditer(
        r'\b(?:where|filter(?:ed)?\s+by|for|with|having|on)\s+(?:the\s+)?([a-zA-Z0-9_\s]{2,60})',
        user_prompt,
        re.IGNORECASE
    )
    for p_m in prep_val_matches:
        full_clause = p_m.group(1).strip()
        tokens = full_clause.split()
        for split_idx in range(1, min(4, len(tokens))):
            cand_c = " ".join(tokens[:split_idx])
            cand_v = " ".join(tokens[split_idx:])
            for stop_w in (" and", " or", " where", " with", " by", " each", " per", " only", " data", " please"):
                if cand_v.lower().endswith(stop_w):
                    cand_v = cand_v[:-len(stop_w)].strip()
            
            matched_c = resolve_best_matching_column(cand_c, all_cols)
            if matched_c and matched_c.lower() not in ("view", "table", "space") and cand_v and cand_v.lower() not in noise_col_words:
                if not any(matched_c.lower() in wc.lower() for wc in where_clauses):
                    col_expr = col_where_map.get(matched_c, f'"{matched_c}"') if col_where_map else f'"{matched_c}"'
                    clean_str = cand_v.replace("'", "''")
                    clean_val = clean_str.lower().strip()
                    no_space_val = clean_val.replace(" ", "").replace("_", "")
                    where_clauses.append(
                        f'(LOWER(TRIM(CAST({col_expr} AS TEXT))) = \'{clean_val}\' '
                        f'OR LOWER(REPLACE(REPLACE(CAST({col_expr} AS TEXT), \' \', \'\'), \'_\', \'\')) = \'{no_space_val}\' '
                        f'OR {col_expr} LIKE \'%{clean_str}%\' '
                        f'OR {col_expr} = \'{clean_str}\')'
                    )
                    odata_filters.append(f"{matched_c} eq '{clean_str}'")
                    structured_conditions.append({"col": matched_c, "op": "=", "target": clean_str, "is_num": False})
                    break

    # Pattern 1: Between X and Y
    between_pat = re.compile(
        r'(?:the\s+)?([a-zA-Z0-9_]+)\s+(?:is|are|was|were)?\s*(?:between)\s+([$€₹]?\s*[\d,]+(?:\.\d+)?)\s+and\s+([$€₹]?\s*[\d,]+(?:\.\d+)?)',
        re.IGNORECASE
    )
    for m in between_pat.finditer(user_prompt):
        col_raw = m.group(1)
        matched_c = resolve_best_matching_column(col_raw, all_cols)
        if matched_c:
            col_expr = col_where_map.get(matched_c, f'"{matched_c}"') if col_where_map else f'"{matched_c}"'
            v1_raw = re.sub(r'[^\d.\-]', '', m.group(2))
            v2_raw = re.sub(r'[^\d.\-]', '', m.group(3))
            try:
                v1_num = float(v1_raw)
                v2_num = float(v2_raw)
                fmt_v1 = int(v1_num) if v1_num.is_integer() else v1_num
                fmt_v2 = int(v2_num) if v2_num.is_integer() else v2_num
                where_clauses.append(f'({col_expr} >= {fmt_v1} AND {col_expr} <= {fmt_v2})')
                odata_filters.append(f"({matched_c} ge {fmt_v1} and {matched_c} le {fmt_v2})")
                structured_conditions.append({"col": matched_c, "op": ">=", "target": fmt_v1, "is_num": True})
                structured_conditions.append({"col": matched_c, "op": "<=", "target": fmt_v2, "is_num": True})
            except ValueError:
                pass

    # Pattern 2: Comprehensive Operator List ordered strictly by precedence (multi-word / inequality before copula verbs)
    op_specs = [
        (r'(?:>=|\b(?:greater\s*than\s*or\s*equal\s*(?:to|s)?|greaterthanorequal(?:to|s)?|at\s*least|min\s*(?:of)?|minimum\s*(?:of)?)\b)', '>='),
        (r'(?:<=|\b(?:less\s*than\s*or\s*equal\s*(?:to|s)?|lessthanorequal(?:to|s)?|at\s*most|max\s*(?:of)?|maximum\s*(?:of)?)\b)', '<='),
        (r'(?:!=|<>|\b(?:not\s*equal\s*(?:to|s)?|notequal(?:to|s)?|is\s*not|are\s*not|does\s*not\s*equal|doesn\'t\s*equal)\b)', '!='),
        (r'(?:>|\b(?:greater\s*than|greaterthan|more\s*than|morethan|higher\s*than|higherthan|larger\s*than|largerthan|strictly\s*greater|strictly\s*more|exceeds|exceeding|above|over)\b)', '>'),
        (r'(?:<|\b(?:less\s*than|lessthan|lower\s*than|lowerthan|smaller\s*than|smallerthan|strictly\s*less|below|under)\b)', '<'),
        (r'\b(?:contains|like|includes|starting\s*with|starts\s*with|ending\s*with|ends\s*with)\b', 'LIKE'),
        (r'(?:==|=|:=|\b(?:equals\s*(?:to)?|equal\s*(?:to)?|equalto|is\s*equal\s*to|are\s*equal\s*to|equals|must\s*be|should\s*be|is|are)\b)', '='),
    ]

    # Pattern matches: [optional noise words] [column] [optional copula: is/are/was/were] [operator] [value]
    comp_regex = re.compile(
        r'(?:(?:where|and|or|,)\s+)?'
        r'(?:(?:the|a|an|that|all|these|those)\s+)*'
        r'[\'"`]?(?P<col>[a-zA-Z0-9_]{2,})[\'"`]?\s*'
        r'(?P<op>'
        r'(?:(?:is|are|was|were)\s+)?'
        r'(?:'
        r'greater\s*than\s*or\s*equal\s*(?:to|s)?|greaterthanorequal(?:to|s)?|at\s*least|min\s*(?:of)?|minimum\s*(?:of)?'
        r'|less\s*than\s*or\s*equal\s*(?:to|s)?|lessthanorequal(?:to|s)?|at\s*most|max\s*(?:of)?|maximum\s*(?:of)?'
        r'|greater\s*than|greaterthan|more\s*than|morethan|higher\s*than|higherthan|larger\s*than|largerthan|strictly\s*greater|strictly\s*more|exceeds|exceeding|above|over'
        r'|less\s*than|lessthan|lower\s*than|lowerthan|smaller\s*than|smallerthan|strictly\s*less|below|under'
        r'|not\s*equal\s*(?:to|s)?|notequal(?:to|s)?|is\s*not|are\s*not|does\s*not\s*equal|doesn\'t\s*equal'
        r'|contains|like|includes'
        r'|equals\s*(?:to)?|equal\s*(?:to)?|equalto|is\s*equal\s*to|are\s*equal\s*to|equals|must\s*be|should\s*be|is|are'
        r'|>=|<=|!=|<>|>|<|==|='
        r')'
        r')\s*'
        r'(?P<val>'
        r'\'[^\']+\'|"[^"]+"|`[^`]+`|[$€₹]?\s*[\d,]+(?:\.\d+)?(?:\s*(?:million|billion|thousand|k|m|b|cr|lakh|crore|usd|eur|inr|days|hours|hrs|mins|percent|%))?|[a-zA-Z0-9_\-]+'
        r')'
        r'(?:\s+only)?',
        re.IGNORECASE
    )

    for m in comp_regex.finditer(user_prompt):
        raw_col = m.group("col").strip().strip("'\"`")
        op_text = m.group("op").strip()
        raw_val = m.group("val").strip().strip("'\"`")
        
        # Skip noise words that are not column identifiers
        if raw_col.lower() in noise_col_words:
            continue
            
        before_match = user_prompt[:m.start()].lower().strip()
        if any(before_match.endswith(k) for k in ["view", "views", "table", "tables", "space", "by", "each", "per", "for"]):
            continue

        matched_c = resolve_best_matching_column(raw_col, all_cols)
        if not matched_c or matched_c.lower() in ("view", "table", "space"):
            continue

        # Avoid duplicate filters for the same column
        if any(matched_c.lower() in wc.lower() for wc in where_clauses):
            continue

        col_expr = col_where_map.get(matched_c, f'"{matched_c}"') if col_where_map else f'"{matched_c}"'
        if not raw_val:
            continue
        raw_val = raw_val.strip()

        # Clean trailing stop words if accidentally captured in unquoted value
        for stop_w in (" and", " or", " where", " with", " by", " each", " per"):
            if raw_val.lower().endswith(stop_w):
                raw_val = raw_val[:-len(stop_w)].strip()

        # Determine operator strictly from op_text by precedence
        op = "="
        for pat, op_symbol in op_specs:
            if re.search(pat, op_text, re.IGNORECASE):
                op = op_symbol
                break

        # Check if numeric
        num_clean = re.sub(r'[^\d.\-]', '', raw_val)
        is_num = False
        num_val = None
        if num_clean and not any(c.isalpha() for c in raw_val.replace('$', '').replace('€', '').replace('₹', '').replace('%', '').strip()):
            try:
                num_val = float(num_clean)
                is_num = True
            except ValueError:
                is_num = False

        if is_num and num_val is not None:
            formatted_num = int(num_val) if num_val.is_integer() else num_val
            if op == 'LIKE':
                where_clauses.append(f'LOWER(CAST({col_expr} AS TEXT)) LIKE \'%{formatted_num}%\'')
            elif op == '=':
                where_clauses.append(f'({col_expr} = {formatted_num} OR CAST({col_expr} AS REAL) = {formatted_num} OR {col_expr} = \'{formatted_num}\')')
            else:
                where_clauses.append(f'({col_expr} {op} {formatted_num} OR CAST({col_expr} AS REAL) {op} {formatted_num})')
            
            odata_op = "eq"
            if op == ">=": odata_op = "ge"
            elif op == "<=": odata_op = "le"
            elif op == ">": odata_op = "gt"
            elif op == "<": odata_op = "lt"
            elif op == "!=": odata_op = "ne"
            odata_filters.append(f"{matched_c} {odata_op} {formatted_num}")
            structured_conditions.append({"col": matched_c, "op": op, "target": formatted_num, "is_num": True})
        else:
            clean_str = raw_val.replace("'", "''")
            if op == 'LIKE' or 'contains' in op_text.lower() or 'includes' in op_text.lower():
                where_clauses.append(f'LOWER(CAST({col_expr} AS TEXT)) LIKE \'%{clean_str.lower()}%\'')
                odata_filters.append(f"contains({matched_c}, '{clean_str}')")
                structured_conditions.append({"col": matched_c, "op": "LIKE", "target": clean_str, "is_num": False})
            elif op == '!=':
                where_clauses.append(f'({col_expr} != \'{clean_str}\' AND {col_expr} IS NOT NULL)')
                odata_filters.append(f"{matched_c} ne '{clean_str}'")
                structured_conditions.append({"col": matched_c, "op": "!=", "target": clean_str, "is_num": False})
            else:
                where_clauses.append(f'({col_expr} = \'{clean_str}\' OR LOWER(CAST({col_expr} AS TEXT)) = \'{clean_str.lower()}\')')
                odata_filters.append(f"{matched_c} eq '{clean_str}'")
                structured_conditions.append({"col": matched_c, "op": "=", "target": clean_str, "is_num": False})

    # Pattern 3: Year / Fiscal Period Extraction
    year_match = re.search(r'\b(?:year|fiscalyear|posting_year|fiscal_year)\s*(?:must\s+be|is|=|==|>=|<=|>|<)?\s*(\d{4})\b', prompt_lower) or re.search(r'\b(\d{4})\s+(?:only|data)\b', prompt_lower)
    if year_match and not any("year" in c.lower() for c in where_clauses):
        target_year = year_match.group(1)
        year_col = resolve_best_matching_column("year", all_cols) or resolve_best_matching_column("fiscalyear", all_cols) or resolve_best_matching_column("posting_year", all_cols)
        if year_col:
            col_expr = col_where_map.get(year_col, f'"{year_col}"') if col_where_map else f'"{year_col}"'
            where_clauses.append(f'({col_expr} = \'{target_year}\' OR {col_expr} = {target_year})')
            odata_filters.append(f"{year_col} eq '{target_year}'")
            structured_conditions.append({"col": year_col, "op": "=", "target": target_year, "is_num": False})

    # Pattern 4: Prepositional & Direct Entity Filters with explicit delimiter: "for materialnumber: MAT100054", "plant = 1710"
    prep_entity_pat = re.compile(
        r'\b(?:for|of|with|having|where|regarding|about|on)\s+(?:the\s+)?[\'"`]?([a-zA-Z0-9_]{2,})[\'"`]?\s*[:=]\s*[\'"`]?([a-zA-Z0-9_\-]+)[\'"`]?',
        re.IGNORECASE
    )
    for m in prep_entity_pat.finditer(user_prompt):
        cand_col = m.group(1).strip().strip("'\"`")
        cand_val = m.group(2).strip().strip("'\"`")
        
        if cand_col.lower() in noise_col_words or cand_val.lower() in noise_col_words:
            continue
            
        matched_c = resolve_best_matching_column(cand_col, all_cols)
        if not matched_c or matched_c.lower() in ("view", "table", "space"):
            continue
            
        # Avoid duplicate filters for the same column
        if any(matched_c.lower() in wc.lower() for wc in where_clauses):
            continue
            
        col_expr = col_where_map.get(matched_c, f'"{matched_c}"') if col_where_map else f'"{matched_c}"'
        
        # Check if numeric
        num_clean = re.sub(r'[^\d.\-]', '', cand_val)
        is_num = False
        num_val = None
        if num_clean and not any(c.isalpha() for c in cand_val.replace('$', '').replace('€', '').replace('₹', '').replace('%', '').strip()):
            try:
                num_val = float(num_clean)
                is_num = True
            except ValueError:
                is_num = False

        if is_num and num_val is not None:
            formatted_num = int(num_val) if num_val.is_integer() else num_val
            where_clauses.append(f'({col_expr} = {formatted_num} OR CAST({col_expr} AS REAL) = {formatted_num} OR {col_expr} = \'{formatted_num}\')')
            odata_filters.append(f"{matched_c} eq {formatted_num}")
            structured_conditions.append({"col": matched_c, "op": "=", "target": formatted_num, "is_num": True})
        else:
            clean_str = cand_val.replace("'", "''")
            where_clauses.append(f'({col_expr} = \'{clean_str}\' OR LOWER(CAST({col_expr} AS TEXT)) = \'{clean_str.lower()}\')')
            odata_filters.append(f"{matched_c} eq '{clean_str}'")
            structured_conditions.append({"col": matched_c, "op": "=", "target": clean_str, "is_num": False})

    # Pattern 5: SAP Alphanumeric Codes in prompt matching known column types (e.g. MAT100054)
    mat_match = re.search(r'\b(MAT[a-zA-Z0-9_\-]+)\b', user_prompt, re.IGNORECASE)
    if mat_match:
        mat_val = mat_match.group(1).strip()
        mat_col = resolve_best_matching_column("materialnumber", all_cols) or resolve_best_matching_column("material", all_cols)
        if mat_col and not any(mat_col.lower() in wc.lower() for wc in where_clauses):
            col_expr = col_where_map.get(mat_col, f'"{mat_col}"') if col_where_map else f'"{mat_col}"'
            clean_str = mat_val.replace("'", "''")
            where_clauses.append(f'({col_expr} = \'{clean_str}\' OR LOWER(CAST({col_expr} AS TEXT)) = \'{clean_str.lower()}\')')
            odata_filters.append(f"{mat_col} eq '{clean_str}'")
            structured_conditions.append({"col": mat_col, "op": "=", "target": clean_str, "is_num": False})

    return where_clauses, odata_filters, structured_conditions


def parse_conversational_sql_or_metadata(
    user_prompt: str, 
    view_schemas: Dict[str, List[str]], 
    foreign_keys: List[Dict[str, Any]],
    selected_fields: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Compiles natural language prompt and view metadata into an exact ANSI SQL query blueprint.
    
    Core Responsibilities:
    1. Case-insensitively qualifies columns and creates COALESCE expressions across joined tables.
    2. Builds LEFT JOIN / CROSS JOIN relational clauses between primary and secondary views.
    3. Analyzes aggregations (SUM, AVG, COUNT), GROUP BY dimensions, and ORDER BY sort clauses.
    4. Projects only user-selected columns while avoiding duplicate column alias collisions.
    5. Returns structured SQL, OData filter string, and execution metadata.
    """
    prompt_lower = user_prompt.lower().strip()
    view_names = list(view_schemas.keys())
    primary_view = view_names[0]
    
    # Step 1: Collect unique columns across all views case-insensitively
    col_select_map: Dict[str, str] = {}
    col_where_map: Dict[str, str] = {}
    all_unique_cols: List[str] = []
    seen_unique_lower = set()
    
    for v_idx, (v_name, cols) in enumerate(view_schemas.items()):
        for c in cols:
            c_clean = str(c).strip()
            if c_clean.lower() not in seen_unique_lower:
                seen_unique_lower.add(c_clean.lower())
                all_unique_cols.append(c_clean)

    # Step 2: Map all column variants across tables using COALESCE for safe outer-joins
    for c in all_unique_cols:
        c_lower = c.lower().strip()
        matching_aliases = []
        for idx, (v_name, cols) in enumerate(view_schemas.items()):
            alias = f"t{idx + 1}"
            exact_col = next((col for col in cols if col.lower().strip() == c_lower), None)
            if exact_col:
                matching_aliases.append(f'{alias}."{exact_col}"')
        
        if len(matching_aliases) == 1:
            col_select_map[c] = f'{matching_aliases[0]} AS "{c}"'
            col_where_map[c] = matching_aliases[0]
        elif len(matching_aliases) > 1:
            coalesce_expr = f'COALESCE({", ".join(matching_aliases)})'
            col_select_map[c] = f'{coalesce_expr} AS "{c}"'
            col_where_map[c] = coalesce_expr
        else:
            col_select_map[c] = f'"{c}"'
            col_where_map[c] = f'"{c}"'

    # Step 3: Handle Schema / Column Description Intent
    info_keywords = [
        "describe", "description", "table info", "what columns", "columns description",
        "column description", "schema", "table information", "about the table",
        "tell me about", "what data is this", "summary of table", "fields", "list columns"
    ]
    if any(k in prompt_lower for k in info_keywords) and not any(k in prompt_lower for k in ["where", "filter", "top ", "sum ", "avg ", "join", "greater", "less", "more"]):
        return {
            "is_table_info": True,
            "sql_query": f"SELECT * FROM t1",
            "direct_answer": f"Displaying schema metadata and column descriptions for {', '.join(view_names)}.",
            "explanation": "Generated dataset profile and column descriptions"
        }

    # Step 4: Construct FROM and LEFT JOIN clauses
    join_clause = ""
    from_clause = f'FROM t1'
    if len(view_names) > 1:
        join_parts = []
        for idx, sec_view in enumerate(view_names[1:], start=2):
            t_alias = f"t{idx}"
            matched_rel = next((r for r in foreign_keys if (r["view_a"] == primary_view and r["view_b"] == sec_view) or (r["view_b"] == primary_view and r["view_a"] == sec_view)), None)
            if matched_rel and matched_rel["join_keys"]:
                first_key = matched_rel["join_keys"][0]
                k1 = first_key["col1"] if matched_rel["view_a"] == primary_view else first_key["col2"]
                k2 = first_key["col2"] if matched_rel["view_a"] == primary_view else first_key["col1"]
                join_parts.append(f'LEFT JOIN {t_alias} ON t1."{k1}" = {t_alias}."{k2}"')
            else:
                common_cols = [c for c in view_schemas[primary_view] if any(c.lower() == sc.lower() for sc in view_schemas[sec_view])]
                if common_cols:
                    join_parts.append(f'LEFT JOIN {t_alias} ON t1."{common_cols[0]}" = {t_alias}."{common_cols[0]}"')
                else:
                    join_parts.append(f'CROSS JOIN {t_alias}')
        join_clause = " " + " ".join(join_parts)

    # Step 5: Detect GROUP BY dimensions & Top N queries FIRST (Prevents grouping words from being swallowed as WHERE filters)
    group_by_col = None
    target_metric = None
    limit_n = None
    
    # Pattern A: "top 5 plants based on grossrevenueamount" or "top 10 customers by spend"
    top_dim_match = re.search(
        r'\b(?:top|bottom|first|last|highest|lowest)\s+(?:(\d+)\s+)?([a-zA-Z0-9_\s]+?)\s+(?:based\s+on|by|with|for|ordered\s+by|having|in\s+terms\s+of)\s+([a-zA-Z0-9_\s]+)\b',
        prompt_lower
    )
    if top_dim_match:
        lim_cand = top_dim_match.group(1)
        dim_cand = top_dim_match.group(2).strip()
        metric_cand = top_dim_match.group(3).strip()
        if lim_cand:
            limit_n = int(lim_cand)
        matched_d = resolve_best_matching_column(dim_cand, all_unique_cols)
        if matched_d:
            group_by_col = matched_d
        matched_m = resolve_best_matching_column(metric_cand, all_unique_cols)
        if matched_m:
            target_metric = matched_m

    # Pattern B: "top 5 plants" (without explicit "based on")
    if not group_by_col:
        top_simple_match = re.search(
            r'\b(?:top|bottom|first|last|highest|lowest)\s+(\d+)\s+([a-zA-Z0-9_]+)\b',
            prompt_lower
        )
        if top_simple_match:
            limit_n = int(top_simple_match.group(1))
            cand_dim = top_simple_match.group(2).strip()
            if cand_dim.lower() not in ("records", "rows", "items", "data", "results", "entries"):
                matched_d = resolve_best_matching_column(cand_dim, all_unique_cols)
                if matched_d:
                    group_by_col = matched_d

    # Pattern C: Standard explicit group by: "for each distinct material", "by each fiscalyear", "group by plants", "per plant", "by customer"
    if not group_by_col:
        group_pat = re.search(
            r'\b(?:group\s+by|grouped\s+by|breakdown\s+by|summarize\s+by|by\s+each|for\s+each(?:\s+distinct)?|for\s+every(?:\s+distinct)?|for\s+all(?:\s+distinct)?|per(?:\s+distinct)?|across\s+each|by|for\s+every|each(?:\s+distinct)?)\s+[\'"`]?([a-zA-Z0-9_]+)[\'"`]?', 
            prompt_lower
        )
        if group_pat:
            cand = group_pat.group(1).strip().strip("'\"`")
            if cand not in ("distinct", "each", "all", "every", "data", "records", "rows", "table", "view"):
                matched_g = resolve_best_matching_column(cand, all_unique_cols)
                if matched_g:
                    group_by_col = matched_g
        elif "customer" in prompt_lower and any(k in prompt_lower for k in ["top ", "purchases", "most", "spend", "orders"]):
            group_by_col = resolve_best_matching_column("CustomerName", all_unique_cols) or resolve_best_matching_column("CustomerNumber", all_unique_cols)
        elif "material" in prompt_lower and any(k in prompt_lower for k in ["distinct", "each", "every", "per", "by material"]):
            group_by_col = resolve_best_matching_column("MaterialNumber", all_unique_cols) or resolve_best_matching_column("MaterialDescription", all_unique_cols)

    # Step 6: Extract WHERE filters (excluding detected group_by dimension)
    where_clauses, odata_filters, structured_conditions = extract_where_conditions_from_prompt(
        user_prompt, 
        all_unique_cols, 
        col_where_map
    )
    if group_by_col:
        # Strip any false where clauses matching the group by column
        where_clauses = [wc for wc in where_clauses if f'"{group_by_col}"' not in wc and group_by_col.lower() not in wc.lower()]

    # Step 7: Detect Aggregations and Metrics
    agg_type = "SUM" if any(k in prompt_lower for k in ["most", "top", "purchases", "spend", "sum", "total", "highest", "revenue", "amount", "each", "per", "by"]) else None
    if not target_metric:
        # Check if user mentioned a column explicitly in prompt
        for c in all_unique_cols:
            c_clean = c.lower().replace("_", "")
            if (c.lower() in prompt_lower or (len(c_clean) >= 4 and c_clean in prompt_lower.replace("_", "").replace(" ", ""))) and c != group_by_col:
                target_metric = c
                break

    if not target_metric:
        for c in all_unique_cols:
            if any(term in c.lower() for term in ["grossrevenue", "netrevenue", "amount", "revenue", "spend", "quantity", "price", "val", "ordercount"]):
                if c != group_by_col:
                    target_metric = c
                    break

    # Step 8: Detect ORDER BY and LIMIT clauses
    if limit_n is None:
        top_match = re.search(r'\b(?:top|bottom|first|last|highest|lowest|limit)\s+(\d+)\b', prompt_lower)
        if top_match:
            limit_n = int(top_match.group(1))

    is_bottom = bool(re.search(r'\b(?:bottom|lowest|least|last)\b', prompt_lower))
    order_by_clause = ""
    
    if group_by_col:
        sort_metric = target_metric or group_by_col
        sort_expr = f'SUM(CAST({col_where_map.get(sort_metric, f"{sort_metric}")} AS REAL))' if sort_metric != group_by_col else col_where_map.get(sort_metric, f'"{sort_metric}"')
        order_by_clause = f'ORDER BY {sort_expr} {"ASC" if is_bottom else "DESC"}'
    elif limit_n or "most" in prompt_lower or is_bottom:
        sort_metric = target_metric or all_unique_cols[0]
        sort_expr = col_where_map.get(sort_metric, f'"{sort_metric}"')
        order_by_clause = f'ORDER BY {sort_expr} {"ASC" if is_bottom else "DESC"}'
    elif target_metric and any(k in prompt_lower for k in ["greater", "more", "higher", "exceeds", "above"]):
        sort_expr = col_where_map.get(target_metric, f'"{target_metric}"')
        order_by_clause = f'ORDER BY {sort_expr} DESC'

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    limit_sql = f"LIMIT {limit_n}" if limit_n else ""

    # Step 9: Build Projection List (Deduplicating column aliases to avoid SQLite collisions)
    if selected_fields and len(selected_fields) > 0 and not group_by_col:
        seen_proj_aliases = set()
        valid_fields = []
        for f in selected_fields:
            matched_f = next((c for c in all_unique_cols if c.lower() == f.lower().strip()), None)
            if matched_f and matched_f.lower() not in seen_proj_aliases:
                seen_proj_aliases.add(matched_f.lower())
                valid_fields.append(col_select_map.get(matched_f, f'"{matched_f}"'))
        select_projection = ", ".join(valid_fields) if valid_fields else "*"
    else:
        select_projection = ", ".join([col_select_map[c] for c in all_unique_cols]) if len(view_names) > 1 else "*"

    # Step 10: Final SQL Assembly
    if group_by_col:
        gb_expr = col_where_map.get(group_by_col, f'"{group_by_col}"')
        gb_select = col_select_map.get(group_by_col, f'{gb_expr} AS "{group_by_col}"')
        agg_selects = [gb_select]
        
        if target_metric and target_metric != group_by_col:
            tm_expr = col_where_map.get(target_metric, f'"{target_metric}"')
            agg_selects.append(f'SUM(CAST({tm_expr} AS REAL)) AS "{target_metric}"')
        
        # If no explicit metric found, add first 2 numerical metrics
        if not target_metric:
            num_cols = [c for c in all_unique_cols if c != group_by_col and any(p in c.lower() for p in ["spend", "amount", "revenue", "qty", "val", "dso", "kpi", "days"])]
            for nc in num_cols[:2]:
                nc_expr = col_where_map.get(nc, f'"{nc}"')
                agg_selects.append(f'SUM(CAST({nc_expr} AS REAL)) AS "{nc}"')
        
        agg_selects.append('COUNT(*) AS "RECORD_COUNT"')
        sql_query = f'SELECT {", ".join(agg_selects)} {from_clause}{join_clause} {where_sql} GROUP BY {gb_expr} {order_by_clause} {limit_sql}'.strip()
    elif agg_type and target_metric and not where_clauses and not limit_n:
        tm_expr = col_where_map.get(target_metric, f'"{target_metric}"')
        sql_query = f'SELECT {agg_type}(CAST({tm_expr} AS REAL)) AS "{agg_type}_{target_metric}", COUNT(*) AS "RECORD_COUNT" {from_clause}{join_clause} {where_sql}'.strip()
    else:
        sql_query = f'SELECT {select_projection} {from_clause}{join_clause} {where_sql} {order_by_clause} {limit_sql}'.strip()

    odata_filter_str = " and ".join(odata_filters) if odata_filters else None

    return {
        "is_table_info": False,
        "sql_query": sql_query,
        "$filter": odata_filter_str,
        "$top": limit_n,
        "group_by": group_by_col,
        "target_metric": target_metric,
        "structured_conditions": structured_conditions,
        "explanation": f"Compiled Relational SQL: {sql_query}"
    }

def extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```[a-zA-Z]*\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean)
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\})", clean, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
    return None

async def call_gemini_api(system_prompt: str, gem_key: str) -> Optional[Dict[str, Any]]:
    global cached_gemini_working
    if cached_gemini_working is False:
        return None

    payload_json = {
        "contents": [{"parts": [{"text": system_prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"}
    }
    
    models = ["gemini-1.5-flash", "gemini-2.0-flash"]
    async with httpx.AsyncClient(timeout=4.0) as client:
        for model in models:
            for api_ver in ["v1beta", "v1"]:
                try:
                    url = f"https://generativelanguage.googleapis.com/{api_ver}/models/{model}:generateContent?key={gem_key}"
                    headers = {"Content-Type": "application/json"}
                    res = await client.post(url, headers=headers, json=payload_json)
                    if res.status_code == 200:
                        cached_gemini_working = True
                        data = res.json()
                        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
                        return extract_json_from_text(raw_text)
                    elif res.status_code in (401, 403, 404):
                        cached_gemini_working = False
                        logger.warning(f"Gemini API Key returned HTTP {res.status_code}. Using local multi-view relational SQL compiler.")
                        return None
                except Exception:
                    pass
                
    cached_gemini_working = False
    return None

async def generate_odata_query_blueprint(
    user_prompt: str, 
    view_schemas: Dict[str, List[str]], 
    space_id: str, 
    foreign_keys: List[Dict[str, Any]],
    selected_fields: Optional[List[str]] = None
) -> Dict[str, Any]:
    view_names = list(view_schemas.keys())
    schema_desc = []
    for v, cols in view_schemas.items():
        schema_desc.append(f"- View '{v}': [{', '.join(cols)}]")
    
    fk_desc = []
    for r in foreign_keys:
        for k in r["join_keys"]:
            fk_desc.append(f"- Relate '{r['view_a']}' and '{r['view_b']}' on '{k['col1']}' = '{k['col2']}'")

    sel_str = f"Selected Fields to Retrieve: [{', '.join(selected_fields)}]" if selected_fields else "All fields selected"

    gem_key = GEMINI_API_KEY or (API_KEY if API_KEY and not API_KEY.startswith("sk-") else "")
    oa_key = OPENAI_API_KEY or (API_KEY if API_KEY and API_KEY.startswith("sk-") else "")

    # Retrieve Golden SQL Few-Shot Examples and Business Glossary from RAG Knowledge Base
    golden_examples = await rag_service.retrieve_golden_sql_examples(user_prompt, space_id=space_id, top_k=2, gemini_key=gem_key, openai_key=oa_key)
    glossary_terms = await rag_service.retrieve_glossary_terms(user_prompt, top_k=2, gemini_key=gem_key, openai_key=oa_key)

    few_shot_section = ""
    if golden_examples:
        few_shot_section = "\n\nFEW-SHOT VERIFIED QUERY EXAMPLES (RAG KNOWLEDGE):\n" + "\n".join([
            f"- User Request: \"{ex['natural_query']}\"\n  Target SQL: {ex['sql_query']}\n  Explanation: {ex.get('explanation', '')}"
            for ex in golden_examples
        ])

    glossary_section = ""
    if glossary_terms:
        glossary_section = "\n\nBUSINESS GLOSSARY & FORMULA DEFINITIONS (RAG KNOWLEDGE):\n" + "\n".join([
            f"- Term: {gt['term']}\n  Formula/Rule: {gt.get('formula') or gt.get('definition')}"
            for gt in glossary_terms
        ])

    system_prompt = f"""You are an expert SAP Datasphere Analytical & Relational SQL Query Planner.
Translate the user's natural language request into an exact ANSI SQL query joining the views on foreign key / relation columns.

TARGET VIEWS METADATA (Zero Transactional Data Leakage):
Space ID: {space_id}
Views:
{chr(10).join(schema_desc)}

{sel_str}

DETECTED FOREIGN KEY / JOIN KEYS:
{chr(10).join(fk_desc) if fk_desc else "No explicit FK detected, match common column names"}
{few_shot_section}
{glossary_section}

USER PROMPT: "{user_prompt}"

INSTRUCTIONS:
1. Generate valid ANSI SQL with JOINs, WHERE, GROUP BY, or ORDER BY as requested.
2. Output valid JSON:
{{
  "sql_query": "<executable ANSI SQL query across the views>",
  "$filter": "<primary view OData filter or null>",
  "$orderby": "<primary view OData orderby or null>",
  "$top": <integer limit or null>,
  "group_by": "<dimension column or null>",
  "target_metric": "<metric column or null>",
  "explanation": "<brief summary of join and operations>"
}}"""

    if gem_key:
        parsed = await call_gemini_api(system_prompt, gem_key)
        if parsed and parsed.get("sql_query"):
            logger.info(f"Gemini generated multi-view SQL: {parsed.get('sql_query')}")
            return parsed

    if oa_key:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                payload = {
                    "model": "gpt-4o",
                    "messages": [{"role": "system", "content": system_prompt}],
                    "response_format": {"type": "json_object"}
                }
                res = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {oa_key}", "Content-Type": "application/json"},
                    json=payload
                )
                if res.status_code == 200:
                    data = res.json()
                    parsed = extract_json_from_text(data["choices"][0]["message"]["content"])
                    if parsed and parsed.get("sql_query"):
                        return parsed
        except Exception as e:
            logger.error(f"OpenAI error: {e}")

    return parse_conversational_sql_or_metadata(user_prompt, view_schemas, foreign_keys, selected_fields)

# Fetch dataset for a single view with pagination & selective columns
async def fetch_single_view_data(
    view_name: str,
    space_id: str,
    bearer_token: str,
    base_host: str,
    params: Dict[str, str],
    user_email: Optional[str] = None,
    max_pages: int = 5
) -> Dict[str, Any]:
    raw_endpoints = [
        f"{base_host}/api/v1/datasphere/consumption/relational/{space_id}/{view_name}/{view_name}",
        f"{base_host}/api/v1/datasphere/consumption/relational/{space_id}/{view_name}",
        f"{base_host}/api/v1/datasphere/consumption/relational/{space_id}/{view_name.upper()}/{view_name.upper()}",
        f"{base_host}/api/v1/datasphere/consumption/relational/{space_id}/{view_name.upper()}",
        f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id}/{view_name}/{view_name}",
        f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id}/{view_name}",
        f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id}/{view_name.upper()}/{view_name.upper()}",
        f"{base_host}/api/v1/datasphere/consumption/analytical/{space_id}/{view_name.upper()}"
    ]

    cached_url = view_working_endpoint_cache.get(f"{space_id}/{view_name}")
    candidate_endpoints = []
    if cached_url:
        candidate_endpoints.append(cached_url)

    for ep in raw_endpoints:
        if ep not in candidate_endpoints:
            candidate_endpoints.append(ep)

    headers_no_dac = {
        "Authorization": f"Bearer {bearer_token}",
        "Accept": "application/json",
        "User-Agent": "Datasphere-Agent/5.0"
    }

    header_attempts = []
    if user_email:
        headers_with_dac = {
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "application/json",
            "SAP-User-Context": f"email={user_email}",
            "User-Agent": "Datasphere-Agent/5.0"
        }
        header_attempts.append((headers_with_dac, "DAC"))
    header_attempts.append((headers_no_dac, "Direct"))

    raw_data = []
    executed_url = ""
    error_logs = []
    success = False

    async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
        for ep in candidate_endpoints:
            for active_headers, h_mode in header_attempts:
                for active_params, p_mode in [(params, "query-params"), ({}, "all-records")]:
                    try:
                        res = await client.get(ep, headers=active_headers, params=active_params)
                        if res.status_code == 200:
                            payload = res.json()
                            val = payload.get("value")
                            if val is None:
                                val = payload.get("d", {}).get("results", payload if isinstance(payload, list) else [])

                            is_service_doc = (
                                isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict) and
                                ("name" in val[0] and "url" in val[0] and len(val[0].keys()) <= 3)
                            )
                            if is_service_doc:
                                entity_url = f"{ep.rstrip('/')}/{val[0].get('url', view_name)}"
                                res_e = await client.get(entity_url, headers=active_headers, params=active_params)
                                if res_e.status_code != 200 and active_params:
                                    res_e = await client.get(entity_url, headers=active_headers)

                                if res_e.status_code == 200:
                                    p_e = res_e.json()
                                    val = p_e.get("value", p_e.get("d", {}).get("results", []))
                                    next_link = p_e.get("@odata.nextLink") or p_e.get("__next")
                                    executed_url = entity_url
                                else:
                                    err_txt = f"[{h_mode}] Drilldown HTTP {res_e.status_code} from {entity_url}"
                                    error_logs.append(err_txt)
                                    val = None
                            else:
                                next_link = payload.get("@odata.nextLink") or payload.get("__next")
                                executed_url = ep

                            if isinstance(val, list) and ((len(val) > 0 and not is_service_doc) or (is_service_doc and executed_url == entity_url and len(val) > 0)):
                                raw_data = val
                                success = True
                                view_working_endpoint_cache[f"{space_id}/{view_name}"] = executed_url
                                
                                page_count = 1
                                current_page_url = executed_url
                                while next_link and len(raw_data) < 200000 and page_count < max_pages:
                                    try:
                                        p_url = resolve_next_link(current_page_url, str(next_link))
                                        res_p = await client.get(p_url, headers=active_headers)
                                        if res_p.status_code == 200:
                                            p_data = res_p.json()
                                            p_rows = p_data.get("value", p_data.get("d", {}).get("results", []))
                                            if not p_rows:
                                                break
                                            raw_data.extend(p_rows)
                                            current_page_url = p_url
                                            next_link = p_data.get("@odata.nextLink") or p_data.get("__next")
                                            page_count += 1
                                        else:
                                            break
                                    except Exception:
                                        break

                                logger.info(f"Retrieved {len(raw_data)} rows for '{view_name}' across {page_count} pages.")
                                break
                            elif isinstance(val, list) and len(val) == 0 and p_mode == "all-records":
                                raw_data = []
                                success = True
                                executed_url = ep
                                view_working_endpoint_cache[f"{space_id}/{view_name}"] = executed_url
                                break
                            elif isinstance(val, dict):
                                raw_data = [val]
                                executed_url = ep
                                success = True
                                view_working_endpoint_cache[f"{space_id}/{view_name}"] = executed_url
                                break
                        else:
                            error_logs.append(f"[{h_mode}] HTTP {res.status_code} on {ep}")
                    except Exception as e:
                        error_logs.append(f"[{h_mode}] Error on {ep}: {str(e)}")

                    if success:
                        break
                if success:
                    break
            if success:
                break

    return {
        "view_name": view_name,
        "success": success,
        "data": raw_data,
        "executed_url": executed_url,
        "error_logs": error_logs
    }

@app.get("/api/info")
@app.get("/health")
async def get_info():
    return {
        "status": "online",
        "space_id": DATASPHERE_SPACE_ID,
        "token_url": DATASPHERE_TOKEN_URL,
        "base_url": DATASPHERE_API_URL
    }

# Endpoint to fetch all views in space catalog
@app.get("/api/views/catalog")
async def get_space_catalog(space_id: Optional[str] = None):
    target_space = space_id.strip() if space_id else DATASPHERE_SPACE_ID
    bearer_token = await get_datasphere_token()
    base_host = get_tenant_base_host(DATASPHERE_API_URL)
    views = await discover_space_views(base_host, target_space, bearer_token)
    return {
        "space_id": target_space,
        "total_views": len(views),
        "views": views
    }

# Endpoint to fetch column schemas and foreign keys for selected views
@app.post("/api/views/schema")
async def get_views_schema(req: SchemaRequest):
    space_id = req.space_id.strip() if req.space_id else DATASPHERE_SPACE_ID
    bearer_token = await get_datasphere_token()
    base_host = get_tenant_base_host(DATASPHERE_API_URL)

    clean_views = [v.strip() for v in req.view_names if v.strip()]
    if not clean_views:
        clean_views = await discover_space_views(base_host, space_id, bearer_token)

    tasks = [probe_view_columns(base_host, space_id, v, bearer_token) for v in clean_views]
    results = await asyncio.gather(*tasks)

    schemas: Dict[str, List[str]] = {}
    for idx, v in enumerate(clean_views):
        schemas[v] = results[idx] if results[idx] else []

    foreign_keys = detect_foreign_key_relations(schemas)

    return {
        "space_id": space_id,
        "views": schemas,
        "foreign_keys": foreign_keys
    }

class SaveSessionRequest(BaseModel):
    session_id: str
    title: str
    space_id: Optional[str] = "RADH_S3P"
    views: Optional[List[str]] = []
    messages: List[Dict[str, Any]] = []

class RenameSessionRequest(BaseModel):
    title: str

# Endpoints for ChatGPT / Gemini style Chat Session History Management
@app.get("/api/sessions")
async def get_sessions():
    sessions = get_all_chat_sessions_from_db()
    return {"sessions": sessions}

@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    sessions = get_all_chat_sessions_from_db()
    for s in sessions:
        if s["id"] == session_id:
            return {"session": s}
    raise HTTPException(status_code=404, detail="Session not found")

@app.post("/api/sessions")
async def save_session(req: SaveSessionRequest):
    save_chat_session_to_db(req.session_id, req.title, req.space_id or DATASPHERE_SPACE_ID, req.views or [], req.messages)
    return {"status": "success", "session_id": req.session_id}

@app.put("/api/sessions/{session_id}/rename")
async def rename_session(session_id: str, req: RenameSessionRequest):
    new_title = req.title.strip()
    if USE_SUPABASE:
        try:
            url = f"{SUPABASE_URL}/rest/v1/chat_sessions?id=eq.{session_id}"
            headers = {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json"
            }
            payload = {
                "title": new_title,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            }
            async with httpx.AsyncClient(timeout=6.0) as client:
                await client.patch(url, headers=headers, json=payload)
        except Exception as e:
            logger.warning(f"Supabase rename error: {e}")

    try:
        conn = sqlite3.connect(SESSION_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE chat_sessions SET title = ?, updated_at = ? WHERE session_id = ?", (new_title, time.time(), session_id))
        conn.commit()
        conn.close()
        return {"status": "success", "session_id": session_id, "title": new_title}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    delete_chat_session_from_db(session_id)
    if session_id in conversation_sessions:
        del conversation_sessions[session_id]
    return {"status": "deleted", "session_id": session_id}

@app.delete("/api/sessions")
async def clear_all_sessions():
    clear_all_chat_sessions_from_db()
    conversation_sessions.clear()
    return {"status": "cleared"}

# ---------------------------------------------------------------------------
# RAG (Retrieval-Augmented Generation) Endpoints
# ---------------------------------------------------------------------------

class GoldenSQLRequest(BaseModel):
    natural_query: str
    sql_query: str
    space_id: Optional[str] = DATASPHERE_SPACE_ID
    target_views: Optional[List[str]] = None
    odata_filter: Optional[str] = None
    explanation: Optional[str] = ""

class GlossaryRequest(BaseModel):
    term: str
    definition: str
    formula: Optional[str] = None
    synonyms: Optional[str] = None

class DocumentRequest(BaseModel):
    title: str
    content: str
    category: Optional[str] = "general"

class RAGSearchRequest(BaseModel):
    query: str
    space_id: Optional[str] = None
    top_k: Optional[int] = 3

@app.get("/api/rag/stats")
async def get_rag_statistics():
    """Get statistics for the RAG vector and knowledge store."""
    return rag_service.get_rag_stats()

@app.post("/api/rag/search")
async def rag_unified_search(req: RAGSearchRequest):
    """Test and inspect semantic RAG retrieval across all knowledge collections."""
    gem_key = GEMINI_API_KEY or (API_KEY if API_KEY and not API_KEY.startswith("sk-") else "")
    oa_key = OPENAI_API_KEY or (API_KEY if API_KEY and API_KEY.startswith("sk-") else "")
    views = await rag_service.retrieve_relevant_views_rag(req.query, space_id=req.space_id, top_k=req.top_k or 3, gemini_key=gem_key, openai_key=oa_key)
    golden = await rag_service.retrieve_golden_sql_examples(req.query, space_id=req.space_id, top_k=req.top_k or 2, gemini_key=gem_key, openai_key=oa_key)
    glossary = await rag_service.retrieve_glossary_terms(req.query, top_k=req.top_k or 2, gemini_key=gem_key, openai_key=oa_key)
    docs = await rag_service.retrieve_context_documents(req.query, top_k=req.top_k or 2, gemini_key=gem_key, openai_key=oa_key)
    return {
        "query": req.query,
        "views": [{"view_name": v[0], "score": round(v[1], 4), "columns": v[2]} for v in views],
        "golden_sql": golden,
        "glossary": glossary,
        "documents": docs
    }

@app.post("/api/rag/golden-sql")
async def add_golden_query(req: GoldenSQLRequest):
    """Add a verified Natural Query -> SQL mapping to RAG store."""
    gem_key = GEMINI_API_KEY or (API_KEY if API_KEY and not API_KEY.startswith("sk-") else "")
    oa_key = OPENAI_API_KEY or (API_KEY if API_KEY and API_KEY.startswith("sk-") else "")
    q_id = await rag_service.add_golden_sql_query(
        natural_query=req.natural_query,
        sql_query=req.sql_query,
        space_id=req.space_id or DATASPHERE_SPACE_ID,
        target_views=req.target_views,
        odata_filter=req.odata_filter,
        explanation=req.explanation or "",
        gemini_key=gem_key,
        openai_key=oa_key
    )
    return {"status": "saved", "id": q_id}

@app.post("/api/rag/glossary")
async def add_glossary_definition(req: GlossaryRequest):
    """Add a business glossary or metric formula definition to RAG store."""
    gem_key = GEMINI_API_KEY or (API_KEY if API_KEY and not API_KEY.startswith("sk-") else "")
    oa_key = OPENAI_API_KEY or (API_KEY if API_KEY and API_KEY.startswith("sk-") else "")
    t_id = await rag_service.add_glossary_term(
        term=req.term,
        definition=req.definition,
        formula=req.formula,
        synonyms=req.synonyms,
        gemini_key=gem_key,
        openai_key=oa_key
    )
    return {"status": "saved", "id": t_id}

@app.post("/api/rag/documents")
async def add_context_doc(req: DocumentRequest):
    """Add an unstructured enterprise context document to RAG store."""
    gem_key = GEMINI_API_KEY or (API_KEY if API_KEY and not API_KEY.startswith("sk-") else "")
    oa_key = OPENAI_API_KEY or (API_KEY if API_KEY and API_KEY.startswith("sk-") else "")
    d_id = await rag_service.add_context_document(
        title=req.title,
        content=req.content,
        category=req.category or "general",
        gemini_key=gem_key,
        openai_key=oa_key
    )
    return {"status": "saved", "id": d_id}

@app.post("/api/rag/index-views")
async def index_all_space_views(space_id: Optional[str] = None):
    """Index all views and probed columns in the given space into RAG."""
    target_space = space_id or DATASPHERE_SPACE_ID
    base_host = get_tenant_base_host(DATASPHERE_API_URL)
    bearer_token = await get_datasphere_token()
    discovered = await discover_space_views(base_host, target_space, bearer_token)
    
    gem_key = GEMINI_API_KEY or (API_KEY if API_KEY and not API_KEY.startswith("sk-") else "")
    oa_key = OPENAI_API_KEY or (API_KEY if API_KEY and API_KEY.startswith("sk-") else "")
    
    indexed_count = 0
    for v in discovered:
        cols = await probe_view_columns(base_host, target_space, v, bearer_token)
        if cols:
            ok = await rag_service.index_view_metadata(target_space, v, cols, gemini_key=gem_key, openai_key=oa_key)
            if ok:
                indexed_count += 1
                
    return {
        "status": "completed",
        "space_id": target_space,
        "total_discovered": len(discovered),
        "indexed_count": indexed_count
    }

@app.post("/api/analytics", response_model=QueryResponse)
async def execute_analytics(request: QueryRequest):
    raw_prompt = request.user_prompt.strip()
    if (raw_prompt.startswith('"') and raw_prompt.endswith('"')) or (raw_prompt.startswith("'") and raw_prompt.endswith("'")):
        inner = raw_prompt[1:-1].strip()
        if inner:
            raw_prompt = inner
    space_id = request.space_id.strip() if request.space_id else DATASPHERE_SPACE_ID
    user_email = request.user_email.strip() if request.user_email else None
    session_id = request.session_id or str(uuid.uuid4())
    selected_fields = [f.strip() for f in request.selected_fields if f.strip()] if request.selected_fields else []
    base_host = get_tenant_base_host(DATASPHERE_API_URL)

    try:
        # 1. Resolve Conversational Multi-Turn Follow-Ups locally (Zero Data Leakage)
        user_prompt, conv_context = resolve_conversational_follow_up(raw_prompt, session_id)

        # 2. Acquire Bearer token
        bearer_token = await get_datasphere_token()

        # 3. Parse or Auto-Discover Views by Space ID
        requested_views = []
        if request.view_names:
            for v in request.view_names:
                if v and v.strip():
                    for sub_v in v.replace(";", ",").split(","):
                        if sub_v.strip() and sub_v.strip() not in requested_views:
                            requested_views.append(sub_v.strip())
        elif request.view_name:
            for sub_v in request.view_name.replace(";", ",").split(","):
                if sub_v.strip() and sub_v.strip() not in requested_views:
                    requested_views.append(sub_v.strip())

        # If views are not explicitly specified, auto-discover from Space
        if not requested_views:
            discovered_catalog = await discover_space_views(base_host, space_id, bearer_token, search_hint=raw_prompt)

            # 1. Pattern matching across all views in space (e.g. "where view name has agent" -> AGENT_KPI, AGENT_KPI1, Agent_Test_Table_New_V)
            pattern_matches = find_views_matching_pattern(raw_prompt, discovered_catalog) if discovered_catalog else []
            if pattern_matches:
                requested_views = pattern_matches
                logger.info(f"Pattern matched View(s) in Space [{space_id}]: {requested_views}")

            # 2. Explicit exact view name in prompt if no pattern matched
            if not requested_views:
                prompt_extracted_views = extract_view_names_from_prompt(raw_prompt)
                if prompt_extracted_views:
                    for ev in prompt_extracted_views:
                        # Check if ev matches any discovered view case-insensitively
                        matched_ev = next((v for v in discovered_catalog if v.lower() == ev.lower()), ev)
                        if matched_ev not in requested_views:
                            requested_views.append(matched_ev)
                            logger.info(f"Extracted View '{matched_ev}' directly from prompt text.")

            # 3. Conversational context inherit previous views
            if not requested_views and conv_context and conv_context.get("previous_views"):
                requested_views = conv_context["previous_views"]

            # 4. RAG-based Semantic View Vector Retrieval
            if not requested_views:
                gem_key = GEMINI_API_KEY or (API_KEY if API_KEY and not API_KEY.startswith("sk-") else "")
                oa_key = OPENAI_API_KEY or (API_KEY if API_KEY and API_KEY.startswith("sk-") else "")
                rag_results = await rag_service.retrieve_relevant_views_rag(raw_prompt, space_id=space_id, top_k=3, gemini_key=gem_key, openai_key=oa_key)
                if rag_results:
                    rag_views = [r[0] for r in rag_results if r[1] > 0.15]
                    if rag_views:
                        requested_views = rag_views
                        logger.info(f"RAG semantically routed View(s) in Space [{space_id}]: {requested_views}")

            # 5. Semantic / Column-level routing fallback
            if not requested_views and discovered_catalog:
                requested_views = await route_prompt_to_views(raw_prompt, space_id, discovered_catalog, base_host, bearer_token)
                logger.info(f"Semantically routed View(s) in Space [{space_id}]: {requested_views}")

        if not requested_views:
            return QueryResponse(
                session_id=session_id,
                query_blueprint={"explanation": f"No view found matching '{raw_prompt}'."},
                retrieved_view_metadata={"space_id": space_id, "columns": []},
                execution_info={
                    "datasphere_endpoint": base_host,
                    "space_id": space_id,
                    "view_name": "None",
                    "live_mode": False,
                    "total_records": 0,
                    "odata_parameters": {}
                },
                kpi_summary=KPISummary(
                    direct_answer=f"Could not find any live view in Space '{space_id}' matching '{raw_prompt}'. Please check the view name or enter it in the left sidebar.",
                    target_metric=None,
                    cards=[
                        KPICard(label="Status", value=0, formatted_value="View Not Found", metric_type="count"),
                        KPICard(label="Active Space", value=space_id, formatted_value=space_id, metric_type="text")
                    ]
                ),
                data=[]
            )

        # Deduplicate requested views case-insensitively so casing variations (e.g. Agent_kpi vs AGENT_KPI) are treated as 1 view
        requested_views = deduplicate_view_names(requested_views)

        # Register discovered/requested views in space cache for fast subsequent routing
        if space_id not in space_views_cache:
            space_views_cache[space_id] = []
        for rv in requested_views:
            if not any(x.lower() == rv.lower() for x in space_views_cache[space_id]):
                space_views_cache[space_id].append(rv)

        logger.info(f"Executing Query on View(s) [{', '.join(requested_views)}] in Space [{space_id}] for: '{user_prompt}'")

        # 4. Discover live columns for all selected views concurrently
        probe_tasks = [probe_view_columns(base_host, space_id, v, bearer_token) for v in requested_views]
        probe_results = await asyncio.gather(*probe_tasks)
        
        view_schemas: Dict[str, List[str]] = {}
        for idx, v in enumerate(requested_views):
            view_schemas[v] = probe_results[idx] if probe_results[idx] else []

        # Prioritize driving view (t1) based on column & keyword relevance in user prompt
        if len(requested_views) > 1:
            def score_view_relevance(v_name: str, cols: List[str]) -> int:
                score = 0
                v_clean = v_name.lower().replace("_", "")
                p_clean = user_prompt.lower().replace("_", "")
                if v_name.lower() in user_prompt.lower() or v_clean in p_clean:
                    score += 100
                for term in re.findall(r'[a-zA-Z0-9]{3,}', user_prompt.lower()):
                    if term in v_clean:
                        score += 30
                for col in cols:
                    c_clean = col.lower().replace("_", "")
                    if col.lower() in user_prompt.lower() or (len(c_clean) >= 3 and c_clean in p_clean):
                        score += 25
                return score

            requested_views.sort(key=lambda v: score_view_relevance(v, view_schemas.get(v, [])), reverse=True)
            view_schemas = {v: view_schemas.get(v, []) for v in requested_views}

        # 5. Detect Foreign Keys between views
        foreign_keys = detect_foreign_key_relations(view_schemas)

        # 6. Generate Query Blueprint (LLM / Compiler receives ONLY metadata)
        blueprint = await generate_odata_query_blueprint(user_prompt, view_schemas, space_id, foreign_keys, selected_fields)

        # 7. Fetch live data for views concurrently (Retrieve all pages for complete 25k-30k+ analytical datasets)
        max_pages = 100

        fetch_tasks = []
        for v in requested_views:
            v_params: Dict[str, str] = {}
            if selected_fields:
                v_cols = view_schemas.get(v, [])
                sel_for_v = [c for c in selected_fields if c in v_cols]
                for r in foreign_keys:
                    if r["view_a"] == v or r["view_b"] == v:
                        for k in r["join_keys"]:
                            jk = k["col1"] if r["view_a"] == v else k["col2"]
                            if jk in v_cols and jk not in sel_for_v:
                                sel_for_v.append(jk)
                if sel_for_v:
                    v_params["$select"] = ",".join(sel_for_v)

            if blueprint.get("$filter") and v == requested_views[0]:
                v_params["$filter"] = str(blueprint["$filter"])

            fetch_tasks.append(
                fetch_single_view_data(
                    view_name=v,
                    space_id=space_id,
                    bearer_token=bearer_token,
                    base_host=base_host,
                    params=v_params,
                    user_email=user_email,
                    max_pages=max_pages
                )
            )

        fetch_results = await asyncio.gather(*fetch_tasks)

        multi_table_data: Dict[str, List[Dict[str, Any]]] = {}
        executed_urls: Dict[str, str] = {}
        for res in fetch_results:
            v = res["view_name"]
            if res["success"]:
                multi_table_data[v] = res["data"]
                executed_urls[v] = res["executed_url"]
                if res["data"] and isinstance(res["data"][0], dict):
                    cached_cols = view_columns_cache.get(f"{space_id}/{v}", [])
                    new_cols = list(res["data"][0].keys())
                    if not selected_fields or len(new_cols) >= len(cached_cols):
                        view_columns_cache[f"{space_id}/{v}"] = new_cols
                        view_schemas[v] = new_cols

        if not multi_table_data:
            return QueryResponse(
                session_id=session_id,
                query_blueprint={"explanation": f"Unable to access data from view(s): {', '.join(requested_views)} in space '{space_id}'."},
                retrieved_view_metadata={"space_id": space_id, "columns": []},
                execution_info={
                    "datasphere_endpoint": base_host,
                    "space_id": space_id,
                    "view_name": ", ".join(requested_views),
                    "live_mode": False,
                    "total_records": 0,
                    "odata_parameters": {}
                },
                kpi_summary=KPISummary(
                    direct_answer=f"Could not retrieve records from view(s): {', '.join(requested_views)} in Space '{space_id}'. Please verify the view name and ensure it is exposed for consumption in Datasphere.",
                    target_metric=None,
                    cards=[
                        KPICard(label="Status", value=0, formatted_value="No Records Found", metric_type="count"),
                        KPICard(label="Target Views", value=len(requested_views), formatted_value=str(len(requested_views)), metric_type="count")
                    ]
                ),
                data=[]
            )

        # 8. Handle Table Metadata / Schema Intent
        is_table_info = blueprint.get("is_table_info", False)
        if is_table_info or any(k in user_prompt.lower() for k in ["describe", "table info", "what columns", "column description", "columns description", "schema"]):
            all_col_rows = []
            for v in requested_views:
                v_data = multi_table_data.get(v, [])
                t_meta = generate_table_info_and_descriptions(v_data, v, space_id)
                for c in t_meta["columns"]:
                    all_col_rows.append({
                        "VIEW_NAME": v,
                        "COLUMN_NAME": c["column_name"],
                        "FRIENDLY_NAME": c["friendly_name"],
                        "DATA_TYPE": c["data_type"],
                        "DISTINCT_COUNT": c["distinct_count"],
                        "SAMPLE_VALUES": ", ".join(str(s) for s in c["sample_values"][:3]),
                        "DESCRIPTION": c["description"]
                    })

            return QueryResponse(
                session_id=session_id,
                query_blueprint=blueprint,
                retrieved_view_metadata={
                    "views": requested_views,
                    "space_id": space_id,
                    "columns": all_col_rows,
                    "foreign_keys": foreign_keys
                },
                execution_info={
                    "datasphere_endpoint": executed_urls.get(requested_views[0], base_host),
                    "space_id": space_id,
                    "view_name": ", ".join(requested_views),
                    "views_auto_discovered": len(request.view_names) == 0,
                    "odata_parameters": {},
                    "live_mode": True,
                    "total_records": sum(len(d) for d in multi_table_data.values())
                },
                kpi_summary=KPISummary(
                    direct_answer=f"Displaying schema metadata across {len(requested_views)} view(s).",
                    target_metric="COLUMNS",
                    cards=[
                        KPICard(label="Discovered Views", value=len(requested_views), formatted_value=str(len(requested_views)), metric_type="count"),
                        KPICard(label="Total Columns", value=len(all_col_rows), formatted_value=str(len(all_col_rows)), metric_type="count"),
                        KPICard(label="Foreign Keys", value=len(foreign_keys), formatted_value=str(len(foreign_keys)), metric_type="count")
                    ]
                ),
                data=all_col_rows
            )

        # 9. Execute Relational SQL Query in SQLite
        sql_to_run = blueprint.get("sql_query", f'SELECT * FROM t1')
        final_data = execute_multi_table_sql(
            multi_table_data, 
            sql_to_run, 
            selected_fields, 
            structured_conditions=blueprint.get("structured_conditions"),
            view_schemas=view_schemas
        )

        # 10. Update Conversational Session Context (Stored privately on server)
        conversation_sessions[session_id] = {
            "last_prompt": raw_prompt,
            "last_views": requested_views,
            "last_data": final_data[:20],  # Keep sample keys for follow-up resolution
            "last_sql": sql_to_run,
            "updated_at": time.time()
        }

        # 11. Compute KPI Summary
        direct_ans = f"Returned {len(final_data):,} records from [{', '.join(requested_views)}]."
        if conv_context and conv_context.get("referenced_entity"):
            direct_ans = f"Details for {conv_context['referenced_entity']}: {len(final_data):,} records analyzed."

        cards = [
            KPICard(label="Combined Records", value=len(final_data), formatted_value=f"{len(final_data):,}", metric_type="count")
        ]

        if len(requested_views) > 1:
            for v_name, v_rows in multi_table_data.items():
                cards.append(KPICard(
                    label=f"{v_name}",
                    value=len(v_rows),
                    formatted_value=f"{len(v_rows):,} rows",
                    metric_type="count"
                ))

        if final_data and len(final_data) > 0:
            sample = final_data[0]
            num_keys = [k for k, v in sample.items() if isinstance(v, (int, float))]
            if num_keys:
                top_metric = num_keys[0]
                m_sum = sum(float(r.get(top_metric, 0) or 0) for r in final_data)
                m_avg = m_sum / len(final_data) if len(final_data) > 0 else 0
                cards.append(KPICard(label=f"Total {top_metric}", value=m_sum, formatted_value=f"{m_sum:,.2f}" if abs(m_sum - round(m_sum)) > 0.01 else f"{int(m_sum):,}", metric_type="sum"))
                cards.append(KPICard(label=f"Avg {top_metric}", value=m_avg, formatted_value=f"{m_avg:,.2f}", metric_type="avg"))

        if len(requested_views) > 1 and foreign_keys:
            cards.append(KPICard(label="Relational Joins", value=len(foreign_keys), formatted_value=str(len(foreign_keys)), metric_type="count"))

        kpi_summary = KPISummary(
            direct_answer=direct_ans,
            target_metric=blueprint.get("target_metric"),
            cards=cards
        )

        return QueryResponse(
            session_id=session_id,
            query_blueprint=blueprint,
            retrieved_view_metadata={
                "views": requested_views,
                "space_id": space_id,
                "foreign_keys": foreign_keys,
                "selected_fields": selected_fields,
                "description": f"Relational dataset across {len(requested_views)} views",
                "columns": list(final_data[0].keys()) if final_data else []
            },
            execution_info={
                "datasphere_endpoint": executed_urls.get(requested_views[0], base_host),
                "space_id": space_id,
                "view_name": ", ".join(requested_views),
                "views_joined": requested_views,
                "views_auto_discovered": len(request.view_names) == 0,
                "conversational_follow_up": bool(conv_context),
                "foreign_key_relations": foreign_keys,
                "odata_parameters": {},
                "live_mode": True,
                "total_records": len(final_data)
            },
            kpi_summary=kpi_summary,
            data=final_data,
            views_data=multi_table_data
        )
    except HTTPException as he:
        logger.warning(f"HTTPException in execute_analytics: {he.detail}")
        return QueryResponse(
            session_id=session_id,
            query_blueprint={"explanation": str(he.detail)},
            retrieved_view_metadata={"space_id": space_id, "columns": []},
            execution_info={
                "datasphere_endpoint": base_host,
                "space_id": space_id,
                "view_name": "None",
                "live_mode": False,
                "total_records": 0,
                "odata_parameters": {}
            },
            kpi_summary=KPISummary(
                direct_answer=f"Could not complete query: {he.detail}",
                target_metric=None,
                cards=[
                    KPICard(label="Error Notice", value=0, formatted_value="Check Connection", metric_type="count"),
                    KPICard(label="Space ID", value=space_id, formatted_value=space_id, metric_type="text")
                ]
            ),
            data=[]
        )
    except Exception as e:
        logger.exception(f"Unhandled error in execute_analytics: {e}")
        return QueryResponse(
            session_id=session_id,
            query_blueprint={"explanation": f"Notice: {str(e)}"},
            retrieved_view_metadata={"space_id": space_id, "columns": []},
            execution_info={
                "datasphere_endpoint": base_host,
                "space_id": space_id,
                "view_name": "None",
                "live_mode": False,
                "total_records": 0,
                "odata_parameters": {}
            },
            kpi_summary=KPISummary(
                direct_answer=f"Could not complete query: {str(e)}",
                target_metric=None,
                cards=[
                    KPICard(label="Error Notice", value=0, formatted_value="Check Connection", metric_type="count"),
                    KPICard(label="Space ID", value=space_id, formatted_value=space_id, metric_type="text")
                ]
            ),
            data=[]
        )

if __name__ == "__main__":
    logger.info("Starting SAP Datasphere Conversational AI backend on http://0.0.0.0:9050")
    uvicorn.run(app, host="0.0.0.0", port=9050)
