import os
import json
import logging
import time
import re
import asyncio
import sqlite3
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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
API_KEY = GEMINI_API_KEY or OPENAI_API_KEY

# Caches
cached_oauth_token: Optional[Dict[str, Any]] = None
view_columns_cache: Dict[str, List[str]] = {}
cached_gemini_working: Optional[bool] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting SAP Datasphere Direct Analytics Agent (with Multi-View Relational Joins & Field Selection)...")
    yield
    logger.info("Stopping Agent.")

app = FastAPI(
    title="SAP Datasphere Direct Analytics API",
    description="Intelligent Multi-View Relational AI Analytics Engine with Dynamic Field Selection",
    version="4.6.0",
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
    view_names: List[str] = Field(..., description="List of view names to discover schema for")
    space_id: Optional[str] = Field(None, description="Datasphere Space ID")

class QueryRequest(BaseModel):
    user_prompt: str = Field(..., description="Natural language request from user")
    user_email: Optional[str] = Field(None, description="Optional User email for DAC")
    view_name: Optional[str] = Field(None, description="Target view name (single view mode)")
    view_names: Optional[List[str]] = Field(default_factory=list, description="List of target view names for multi-view analytics")
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
    query_blueprint: Dict[str, Any]
    retrieved_view_metadata: Dict[str, Any]
    execution_info: Dict[str, Any]
    kpi_summary: Optional[KPISummary] = None
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
    parent_url = current_url.split("?")[0].rsplit("/", 1)[0]
    return f"{parent_url}/{next_link_str}"

def resolve_best_matching_column(target_col: str, available_columns: List[str]) -> Optional[str]:
    """Finds the best matching technical column name from available schema columns."""
    if not target_col or not available_columns:
        return None
    
    target_clean = target_col.strip().lower().replace("_", "").replace(" ", "")
    if not target_clean:
        return None

    # 1. Exact match (case-insensitive)
    for c in available_columns:
        if c.lower() == target_col.strip().lower():
            return c

    # 2. Exact match ignoring underscores and spaces
    for c in available_columns:
        if c.lower().replace("_", "").replace(" ", "") == target_clean:
            return c

    # 3. Substring match where target is contained in column
    for c in available_columns:
        c_clean = c.lower().replace("_", "").replace(" ", "")
        if target_clean in c_clean:
            return c

    # 4. Substring match where column is contained in target
    for c in available_columns:
        c_clean = c.lower().replace("_", "").replace(" ", "")
        if c_clean in target_clean:
            return c

    return None

def infer_column_sqlite_type(values: List[Any]) -> str:
    """Infers appropriate SQLite column type from non-null sample values."""
    non_nulls = [v for v in values if v is not None and str(v).strip() != ""]
    if not non_nulls:
        return "TEXT"
    
    is_int = True
    is_float = True
    for v in non_nulls[:100]:
        v_str = str(v).replace(",", "").strip()
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

def detect_foreign_key_relations(view_schemas: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    """Discovers shared/matching foreign key candidate columns between views."""
    relations = []
    view_names = list(view_schemas.keys())
    
    for i in range(len(view_names)):
        for j in range(i + 1, len(view_names)):
            v1 = view_names[i]
            v2 = view_names[j]
            cols1 = view_schemas[v1]
            cols2 = view_schemas[v2]
            
            matching_keys = []
            for c1 in cols1:
                c1_clean = c1.lower().replace("_", "").replace(" ", "")
                for c2 in cols2:
                    c2_clean = c2.lower().replace("_", "").replace(" ", "")
                    if c1.lower() == c2.lower() or c1_clean == c2_clean:
                        matching_keys.append({"col1": c1, "col2": c2, "key_name": c1})
                    elif (c1_clean in c2_clean or c2_clean in c1_clean) and any(
                        term in c1_clean for term in ["code", "id", "num", "key", "plant", "company", "year", "cust", "mat", "doc"]
                    ):
                        if not any(k["col1"] == c1 for k in matching_keys):
                            matching_keys.append({"col1": c1, "col2": c2, "key_name": f"{c1} ~ {c2}"})
            
            if matching_keys:
                relations.append({
                    "view_a": v1,
                    "view_b": v2,
                    "join_keys": matching_keys
                })
    return relations

def execute_multi_table_sql(
    multi_table_data: Dict[str, List[Dict[str, Any]]], 
    sql_query: str,
    selected_fields: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """Loads multiple datasets into SQLite in-memory tables and executes relational SQL JOIN queries."""
    if not multi_table_data:
        return []

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    table_index = 1
    for view_name, raw_data in multi_table_data.items():
        if not raw_data:
            continue
        
        cols = list(raw_data[0].keys())
        col_types = {}
        for c in cols:
            col_vals = [r.get(c) for r in raw_data[:200]]
            col_types[c] = infer_column_sqlite_type(col_vals)

        col_defs = ", ".join([f'"{c}" {col_types[c]}' for c in cols])
        
        table_aliases = [
            f'datasphere_table' if table_index == 1 else f'datasphere_table_{table_index}',
            f't{table_index}',
            f'table_{table_index}',
            f'"{view_name}"',
            f'v_{re.sub(r"[^a-zA-Z0-9_]", "_", view_name).lower()}'
        ]

        primary_name = f't{table_index}'
        cursor.execute(f'CREATE TABLE {primary_name} ({col_defs})')
        
        placeholders = ", ".join(["?"] * len(cols))
        insert_sql = f'INSERT INTO {primary_name} VALUES ({placeholders})'

        rows_to_insert = []
        for r in raw_data:
            row_vals = []
            for c in cols:
                v = r.get(c)
                if v is not None and col_types[c] in ("INTEGER", "REAL"):
                    try:
                        v_clean = str(v).replace(",", "").strip()
                        row_vals.append(int(float(v_clean)) if col_types[c] == "INTEGER" else float(v_clean))
                    except (ValueError, TypeError):
                        row_vals.append(v)
                else:
                    row_vals.append(v)
            rows_to_insert.append(row_vals)

        cursor.executemany(insert_sql, rows_to_insert)

        for alias in table_aliases:
            if alias != primary_name:
                try:
                    cursor.execute(f'CREATE VIEW {alias} AS SELECT * FROM {primary_name}')
                except Exception:
                    pass

        table_index += 1

    conn.commit()

    try:
        cursor.execute(sql_query)
        results = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        # If user explicitly selected a subset of fields and query is a SELECT * style, project only selected fields
        if selected_fields and len(selected_fields) > 0 and results and len(results) > 0:
            existing_cols = list(results[0].keys())
            matched_sel = [c for c in selected_fields if c in existing_cols]
            if matched_sel and len(matched_sel) < len(existing_cols) and not any(k in sql_query.lower() for k in ["group by", "sum(", "avg(", "count("]):
                results = [{k: r.get(k) for k in matched_sel} for r in results]

        logger.info(f"Executed Multi-Table In-Memory SQL: '{sql_query}' -> {len(results)} rows returned.")
        return results
    except Exception as sql_err:
        conn.close()
        logger.warning(f"SQL execution error on '{sql_query}': {sql_err}")
        for v, d in multi_table_data.items():
            if d:
                return d
        return []

def generate_table_info_and_descriptions(raw_data: List[Dict[str, Any]], view_name: str, space_id: str) -> Dict[str, Any]:
    """Generates comprehensive column descriptions, data types, distinct counts, and statistics for the dataset."""
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

def parse_conversational_sql_or_metadata(
    user_prompt: str, 
    view_schemas: Dict[str, List[str]], 
    foreign_keys: List[Dict[str, Any]],
    selected_fields: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Multi-Table aware natural language SQL compiler with automatic relational JOIN generation and field projection."""
    prompt_lower = user_prompt.lower().strip()
    view_names = list(view_schemas.keys())
    primary_view = view_names[0]
    all_cols = []
    for v, cols in view_schemas.items():
        all_cols.extend(cols)

    # 1. Detect Table Info / Description Intent
    info_keywords = [
        "describe", "description", "table info", "what columns", "columns description",
        "column description", "schema", "table information", "about the table",
        "tell me about", "what data is this", "summary of table", "fields", "list columns"
    ]
    if any(k in prompt_lower for k in info_keywords) and not any(k in prompt_lower for k in ["where", "filter", "top ", "sum ", "avg ", "join"]):
        return {
            "is_table_info": True,
            "sql_query": f"SELECT * FROM t1",
            "direct_answer": f"Displaying schema metadata and column descriptions for {', '.join(view_names)}.",
            "explanation": "Generated dataset profile and column descriptions"
        }

    # 2. Build multi-table JOIN clauses if multiple views exist
    join_clause = ""
    from_clause = f'FROM "{primary_view}" t1'
    
    if len(view_names) > 1:
        join_parts = []
        for idx, sec_view in enumerate(view_names[1:], start=2):
            t_alias = f"t{idx}"
            matched_rel = next((r for r in foreign_keys if (r["view_a"] == primary_view and r["view_b"] == sec_view) or (r["view_b"] == primary_view and r["view_a"] == sec_view)), None)
            
            if matched_rel and matched_rel["join_keys"]:
                first_key = matched_rel["join_keys"][0]
                k1 = first_key["col1"] if matched_rel["view_a"] == primary_view else first_key["col2"]
                k2 = first_key["col2"] if matched_rel["view_a"] == primary_view else first_key["col1"]
                join_parts.append(f'LEFT JOIN "{sec_view}" {t_alias} ON t1."{k1}" = {t_alias}."{k2}"')
            else:
                common_cols = [c for c in view_schemas[primary_view] if c in view_schemas[sec_view]]
                if common_cols:
                    join_parts.append(f'LEFT JOIN "{sec_view}" {t_alias} ON t1."{common_cols[0]}" = {t_alias}."{common_cols[0]}"')
                else:
                    join_parts.append(f'CROSS JOIN "{sec_view}" {t_alias}')
                    
        join_clause = " " + " ".join(join_parts)

    # 3. Build WHERE clauses
    where_clauses = []
    comp_pat = re.compile(
        r'([a-zA-Z0-9_]+)\s*(?:must\s+be|should\s+be|equals\s+to|equals|is\s+not|is|=|==|!=|<>|>=|<=|>|<|contains|like)\s*(?:\'([^\']+)\'|"([^"]+)"|([0-9a-zA-Z_\-]+))(?:\s+only)?',
        re.IGNORECASE
    )
    for m in comp_pat.finditer(user_prompt):
        raw_col = m.group(1)
        matched_c = resolve_best_matching_column(raw_col, all_cols)
        if matched_c:
            val = m.group(2) or m.group(3) or m.group(4)
            match_full = m.group(0).lower()
            is_ne = "not" in match_full or "!=" in match_full or "<>" in match_full
            is_gt = ">=" in match_full or ">" in match_full or "greater" in match_full
            is_lt = "<=" in match_full or "<" in match_full or "less" in match_full
            op = "!=" if is_ne else (">=" if is_gt else ("<=" if is_lt else "="))

            try:
                float(val)
                where_clauses.append(f'("{matched_c}" {op} \'{val}\' OR "{matched_c}" {op} {val})')
            except ValueError:
                where_clauses.append(f'"{matched_c}" {op} \'{val}\'')

    year_match = re.search(r'\b(?:year|posting_year)\s+(?:must\s+be\s+|is\s+)?(\d{4})\b', prompt_lower) or re.search(r'\b(\d{4})\s+(?:only|data)\b', prompt_lower)
    if year_match and not any("year" in c.lower() for c in where_clauses):
        target_year = year_match.group(1)
        year_col = resolve_best_matching_column("year", all_cols) or resolve_best_matching_column("posting_year", all_cols)
        if year_col:
            where_clauses.append(f'("{year_col}" = \'{target_year}\' OR "{year_col}" = {target_year})')

    breach_match = re.search(r'\b(?:breach|kpi_breach)\s+(?:is\s+|must\s+be\s+)?([xXyY10]|true|false)\b', prompt_lower)
    if breach_match and not any("breach" in c.lower() for c in where_clauses):
        b_val = breach_match.group(1).upper()
        breach_col = resolve_best_matching_column("breach", all_cols) or resolve_best_matching_column("kpi_breach", all_cols)
        if breach_col:
            where_clauses.append(f'("{breach_col}" = \'{b_val}\' OR "{breach_col}" = \'X\')')

    # 4. Detect Group By & Aggregations
    group_by_col = None
    group_pat = re.search(r'\b(?:group\s+by|grouped\s+by|per|breakdown\s+by|summarize\s+by|by)\s+[\'"]?([a-zA-Z0-9_\s]+)[\'"]?', prompt_lower)
    if group_pat:
        cand = group_pat.group(1).strip().strip("'\"")
        matched_g = resolve_best_matching_column(cand, all_cols)
        if matched_g:
            group_by_col = matched_g

    agg_type = None
    target_metric = None
    if re.search(r'\b(sum|total|add up)\b', prompt_lower):
        agg_type = "SUM"
    elif re.search(r'\b(avg|average|mean)\b', prompt_lower):
        agg_type = "AVG"
    elif re.search(r'\b(max|maximum|highest|peak)\b', prompt_lower):
        agg_type = "MAX"
    elif re.search(r'\b(min|minimum|lowest)\b', prompt_lower):
        agg_type = "MIN"
    elif re.search(r'\b(count|how many|number of)\b', prompt_lower):
        agg_type = "COUNT"

    for c in all_cols:
        if c.lower().replace("_", "") in prompt_lower.replace(" ", "").replace("_", ""):
            if c != group_by_col:
                target_metric = c
                break

    limit_n = None
    order_by_clause = ""
    top_match = re.search(r'\b(top|bottom|first|last|highest|lowest|limit)\s+(\d+)\b', prompt_lower)
    if top_match:
        limit_n = int(top_match.group(2))
        is_bottom = top_match.group(1).lower() in ("bottom", "last", "lowest")
        sort_metric = target_metric or next((c for c in all_cols if any(p in c.lower() for p in ["critical", "spend", "amount", "revenue", "count"])), all_cols[0])
        order_by_clause = f'ORDER BY "{sort_metric}" {"ASC" if is_bottom else "DESC"}'

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    limit_sql = f"LIMIT {limit_n}" if limit_n else ""

    # Selected columns projection
    select_projection = "*"
    if selected_fields and len(selected_fields) > 0:
        valid_fields = [f'"{f}"' for f in selected_fields if f in all_cols]
        if valid_fields:
            select_projection = ", ".join(valid_fields)

    if group_by_col:
        num_cols = [c for c in all_cols if c != group_by_col and any(p in c.lower() for p in ["critical", "spend", "amount", "revenue", "qty", "val", "dso", "kpi"])]
        if not num_cols:
            num_cols = [c for c in all_cols if c != group_by_col][:2]
        
        agg_selects = [f'"{group_by_col}"', 'COUNT(*) AS "RECORD_COUNT"']
        for nc in num_cols:
            agg_selects.append(f'SUM("{nc}") AS "{nc}"')
        
        sql_query = f'SELECT {", ".join(agg_selects)} {from_clause}{join_clause} {where_sql} GROUP BY "{group_by_col}" {order_by_clause} {limit_sql}'.strip()
    elif agg_type and target_metric:
        sql_query = f'SELECT {agg_type}("{target_metric}") AS "{agg_type}_{target_metric}", COUNT(*) AS "RECORD_COUNT" {from_clause}{join_clause} {where_sql}'.strip()
    else:
        sql_query = f'SELECT {select_projection} {from_clause}{join_clause} {where_sql} {order_by_clause} {limit_sql}'.strip()

    # OData Filter for primary view
    odata_filters = []
    if where_clauses:
        for m in comp_pat.finditer(user_prompt):
            raw_c = m.group(1)
            matched_c = resolve_best_matching_column(raw_c, view_schemas[primary_view])
            if matched_c:
                val = m.group(2) or m.group(3) or m.group(4)
                m_txt = m.group(0).lower()
                op = "ne" if ("not" in m_txt or "!=" in m_txt) else ("gt" if ">" in m_txt else ("lt" if "<" in m_txt else "eq"))
                try:
                    float(val)
                    odata_filters.append(f"{matched_c} {op} {val}")
                except ValueError:
                    odata_filters.append(f"{matched_c} {op} '{val}'")

    odata_filter_str = " and ".join(odata_filters) if odata_filters else None

    return {
        "is_table_info": False,
        "sql_query": sql_query,
        "$filter": odata_filter_str,
        "$top": limit_n,
        "group_by": group_by_col,
        "target_metric": target_metric,
        "explanation": f"Relational Multi-View SQL: {sql_query}"
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
    """Uses LLM or Local Multi-View Compiler to translate user prompts into multi-table relational SQL queries."""
    view_names = list(view_schemas.keys())
    schema_desc = []
    for v, cols in view_schemas.items():
        schema_desc.append(f"- View '{v}': [{', '.join(cols)}]")
    
    fk_desc = []
    for r in foreign_keys:
        for k in r["join_keys"]:
            fk_desc.append(f"- Relate '{r['view_a']}' and '{r['view_b']}' on '{k['col1']}' = '{k['col2']}'")

    sel_str = f"Selected Fields to Retrieve: [{', '.join(selected_fields)}]" if selected_fields else "All fields selected"

    system_prompt = f"""You are an expert SAP Datasphere Analytical & Relational SQL Query Planner.
Translate the user's natural language request into an exact ANSI SQL query joining the views on foreign key / relation columns.

TARGET VIEWS METADATA (Zero Transactional Data Leakage):
Space ID: {space_id}
Views:
{chr(10).join(schema_desc)}

{sel_str}

DETECTED FOREIGN KEY / JOIN KEYS:
{chr(10).join(fk_desc) if fk_desc else "No explicit FK detected, match common column names"}

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

    gem_key = GEMINI_API_KEY or (API_KEY if API_KEY and not API_KEY.startswith("sk-") else "")
    oa_key = OPENAI_API_KEY or (API_KEY if API_KEY and API_KEY.startswith("sk-") else "")

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

    # Fallback to local multi-view compiler
    return parse_conversational_sql_or_metadata(user_prompt, view_schemas, foreign_keys, selected_fields)

# Probing helper to discover schema columns of any view
async def probe_view_columns(base_host: str, space_id: str, view_name: str, bearer_token: str) -> List[str]:
    cache_key = f"{space_id}/{view_name}"
    if cache_key in view_columns_cache and view_columns_cache[cache_key]:
        return view_columns_cache[cache_key]

    headers = {"Authorization": f"Bearer {bearer_token}", "Accept": "application/json"}
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

    seen_urls = set()
    unique_test_urls = []
    for u in test_urls:
        if u not in seen_urls:
            seen_urls.add(u)
            unique_test_urls.append(u)

    async with httpx.AsyncClient(timeout=10.0) as client:
        for url in unique_test_urls:
            try:
                res = await client.get(url, headers=headers, params={"$top": "1"})
                if res.status_code == 200:
                    payload = res.json()
                    val = payload.get("value", payload.get("d", {}).get("results", []))
                    if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                        if "url" in val[0] and "name" in val[0] and len(val[0].keys()) <= 3:
                            entity_url = f"{url.rstrip('/')}/{val[0].get('url', view_name)}"
                            res_e = await client.get(entity_url, headers=headers, params={"$top": "1"})
                            if res_e.status_code == 200:
                                p_e = res_e.json()
                                val_e = p_e.get("value", p_e.get("d", {}).get("results", []))
                                if isinstance(val_e, list) and len(val_e) > 0 and isinstance(val_e[0], dict):
                                    cols = list(val_e[0].keys())
                                    view_columns_cache[cache_key] = cols
                                    logger.info(f"Discovered {len(cols)} columns for {cache_key}: {cols[:6]}...")
                                    return cols
                            continue
                        cols = list(val[0].keys())
                        view_columns_cache[cache_key] = cols
                        logger.info(f"Discovered {len(cols)} columns for {cache_key}: {cols[:6]}...")
                        return cols
            except Exception:
                pass
    return []

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

    candidate_endpoints = []
    for ep in raw_endpoints:
        if ep not in candidate_endpoints:
            candidate_endpoints.append(ep)

    headers_no_dac = {
        "Authorization": f"Bearer {bearer_token}",
        "Accept": "application/json",
        "User-Agent": "Datasphere-Agent/4.6"
    }

    header_attempts = []
    if user_email:
        headers_with_dac = {
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "application/json",
            "SAP-User-Context": f"email={user_email}",
            "User-Agent": "Datasphere-Agent/4.6"
        }
        header_attempts.append((headers_with_dac, "DAC"))
    header_attempts.append((headers_no_dac, "Direct"))

    raw_data = []
    executed_url = ""
    error_logs = []
    success = False

    async with httpx.AsyncClient(timeout=30.0) as client:
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

                            if isinstance(val, list) and (len(val) >= 0 and not is_service_doc or (is_service_doc and executed_url == entity_url)):
                                raw_data = val
                                success = True
                                
                                page_count = 1
                                current_page_url = executed_url
                                while next_link and len(raw_data) < 25000 and page_count < max_pages:
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
                            elif isinstance(val, dict):
                                raw_data = [val]
                                executed_url = ep
                                success = True
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

# Endpoint to fetch column schemas and foreign keys for selected views
@app.post("/api/views/schema")
async def get_views_schema(req: SchemaRequest):
    space_id = req.space_id.strip() if req.space_id else DATASPHERE_SPACE_ID
    clean_views = [v.strip() for v in req.view_names if v.strip()]
    if not clean_views:
        return {
            "space_id": space_id,
            "views": {},
            "foreign_keys": []
        }

    bearer_token = await get_datasphere_token()
    base_host = get_tenant_base_host(DATASPHERE_API_URL)

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

@app.post("/api/analytics", response_model=QueryResponse)
async def execute_analytics(request: QueryRequest):
    user_prompt = request.user_prompt.strip()
    space_id = request.space_id.strip() if request.space_id else DATASPHERE_SPACE_ID
    user_email = request.user_email.strip() if request.user_email else None
    selected_fields = [f.strip() for f in request.selected_fields if f.strip()] if request.selected_fields else []

    # Parse view names list
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

    if not requested_views:
        raise HTTPException(
            status_code=400,
            detail="Please provide at least one Datasphere view name to query."
        )

    logger.info(f"Querying Views [{', '.join(requested_views)}] (Selected Fields: {len(selected_fields)}) for: '{user_prompt}'")

    # 1. Fetch live Bearer token
    bearer_token = await get_datasphere_token()
    base_host = get_tenant_base_host(DATASPHERE_API_URL)

    # 2. Discover live columns for all views concurrently
    probe_tasks = [probe_view_columns(base_host, space_id, v, bearer_token) for v in requested_views]
    probe_results = await asyncio.gather(*probe_tasks)
    
    view_schemas: Dict[str, List[str]] = {}
    for idx, v in enumerate(requested_views):
        view_schemas[v] = probe_results[idx] if probe_results[idx] else []

    # 3. Detect candidate Foreign Keys / Join Keys between views
    foreign_keys = detect_foreign_key_relations(view_schemas)

    # 4. Generate query blueprint
    blueprint = await generate_odata_query_blueprint(user_prompt, view_schemas, space_id, foreign_keys, selected_fields)

    # 5. Fetch live data for all views concurrently with field selection optimization
    is_bulk = bool(re.search(r'\b(all|entire|export|download|every|full)\b', user_prompt.lower()))
    max_pages = 60 if is_bulk else 5

    fetch_tasks = []
    for v in requested_views:
        v_params: Dict[str, str] = {}
        # If user specified selected_fields, pass $select for this view (include join keys)
        if selected_fields:
            v_cols = view_schemas.get(v, [])
            sel_for_v = [c for c in selected_fields if c in v_cols]
            # Ensure foreign keys are included so SQLite can join
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
                view_columns_cache[f"{space_id}/{v}"] = list(res["data"][0].keys())
                view_schemas[v] = list(res["data"][0].keys())

    if not any(multi_table_data.values()):
        raise HTTPException(
            status_code=400,
            detail=f"Unable to access data from requested views: {', '.join(requested_views)} in space '{space_id}'."
        )

    # 6. Handle Table Metadata & Column Description Intent
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
                "odata_parameters": {},
                "live_mode": True,
                "total_records": sum(len(d) for d in multi_table_data.values())
            },
            kpi_summary=KPISummary(
                direct_answer=f"Displaying combined schema and column descriptions across {len(requested_views)} views.",
                target_metric="COLUMNS",
                cards=[
                    KPICard(label="Total Views", value=len(requested_views), formatted_value=str(len(requested_views)), metric_type="count"),
                    KPICard(label="Total Columns", value=len(all_col_rows), formatted_value=str(len(all_col_rows)), metric_type="count"),
                    KPICard(label="Foreign Key Relations", value=len(foreign_keys), formatted_value=str(len(foreign_keys)), metric_type="count")
                ]
            ),
            data=all_col_rows
        )

    # 7. Execute Relational SQL Query via In-Memory Multi-Table SQLite Engine
    sql_to_run = blueprint.get("sql_query", f'SELECT * FROM "{requested_views[0]}"')
    final_data = execute_multi_table_sql(multi_table_data, sql_to_run, selected_fields)

    # 8. Dynamically compute KPI Summary
    direct_ans = f"Returned {len(final_data):,} records across [{', '.join(requested_views)}]{f' (showing {len(selected_fields)} selected fields)' if selected_fields else ''}."
    cards = [
        KPICard(label="Matched Records", value=len(final_data), formatted_value=f"{len(final_data):,}", metric_type="count")
    ]

    if final_data and len(final_data) > 0:
        sample = final_data[0]
        num_keys = [k for k, v in sample.items() if isinstance(v, (int, float))]
        if num_keys:
            top_metric = num_keys[0]
            m_sum = sum(float(r.get(top_metric, 0) or 0) for r in final_data)
            m_avg = m_sum / len(final_data) if len(final_data) > 0 else 0
            cards.append(KPICard(label=f"Total {top_metric}", value=m_sum, formatted_value=f"{m_sum:,.2f}" if abs(m_sum - round(m_sum)) > 0.01 else f"{int(m_sum):,}", metric_type="sum"))
            cards.append(KPICard(label=f"Avg {top_metric}", value=m_avg, formatted_value=f"{m_avg:,.2f}", metric_type="avg"))

    if selected_fields:
        cards.append(KPICard(label="Active Selected Fields", value=len(selected_fields), formatted_value=str(len(selected_fields)), metric_type="count"))

    if foreign_keys:
        cards.append(KPICard(label="Active Relational Joins", value=len(foreign_keys), formatted_value=str(len(foreign_keys)), metric_type="count"))

    kpi_summary = KPISummary(
        direct_answer=direct_ans,
        target_metric=blueprint.get("target_metric"),
        cards=cards
    )

    return QueryResponse(
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
            "foreign_key_relations": foreign_keys,
            "odata_parameters": {},
            "live_mode": True,
            "total_records": len(final_data)
        },
        kpi_summary=kpi_summary,
        data=final_data
    )

if __name__ == "__main__":
    logger.info("Starting SAP Datasphere Direct Analytics backend on http://0.0.0.0:9050")
    uvicorn.run(app, host="0.0.0.0", port=9050)
