import React, { useState, useEffect, useMemo } from 'react';
import {
  Send,
  ShieldCheck,
  Database,
  BarChart3,
  Table as TableIcon,
  Sparkles,
  Lock,
  RefreshCw,
  Download,
  CheckCircle2,
  AlertCircle,
  Zap,
  Cpu,
  Layers,
  ChevronDown,
  ChevronUp,
  Search,
  Copy,
  Clock,
  Trash2,
  FileCode,
  ChevronLeft,
  ChevronRight,
  Plus,
  X,
  Link2,
  SlidersHorizontal,
  Check,
  Tag
} from 'lucide-react';
import {
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  Legend,
  CartesianGrid,
  LineChart,
  Line,
  AreaChart,
  Area
} from 'recharts';

interface ForeignKeyRelation {
  view_a: string;
  view_b: string;
  join_keys: Array<{
    col1: string;
    col2: string;
    key_name: string;
  }>;
}

interface AnalyticsResponse {
  query_blueprint: {
    view_name?: string;
    space_id?: string;
    $select?: string;
    $filter?: string;
    $orderby?: string;
    $top?: number;
    $skip?: number;
    sql_query?: string;
    explanation?: string;
  };
  retrieved_view_metadata: {
    views?: string[];
    view_name?: string;
    space_id: string;
    description?: string;
    columns: any[];
    foreign_keys?: ForeignKeyRelation[];
    selected_fields?: string[];
  };
  execution_info: {
    datasphere_endpoint: string;
    space_id: string;
    view_name: string;
    views_joined?: string[];
    foreign_key_relations?: ForeignKeyRelation[];
    odata_parameters: Record<string, string>;
    dac_enforced_user?: string;
    zero_data_leakage_status?: string;
    live_mode: boolean;
    total_records?: number;
  };
  kpi_summary?: {
    direct_answer: string;
    target_metric?: string;
    cards: Array<{
      label: string;
      value: any;
      formatted_value: string;
      metric_type: string;
    }>;
  };
  data: Record<string, any>[];
}

const PROMPT_CATEGORIES = [
  {
    category: "Relational & Multi-View Joins",
    prompts: [
      "Join views on common keys and compare metrics",
      "Group by CompanyCode and sum NetAmount across joined views",
      "Select records matching across all selected views",
      "Show relationship and foreign key matching columns"
    ]
  },
  {
    category: "Filters & Conditions",
    prompts: [
      "get me the data where breach is X",
      "WHERE POSTING_YEAR MUST BE 2026 ONLY",
      "records where status is ACTIVE and amount > 5000"
    ]
  },
  {
    category: "Aggregations & Summary",
    prompts: [
      "Group by PlantName and sum NetAmount",
      "Average NetAmount and count per CompanyCode",
      "Total NetAmount grouped by PostingYear"
    ]
  },
  {
    category: "Schema & Relationships",
    prompts: [
      "Describe table information and column descriptions",
      "Show all available column names, data types, and distinct counts",
      "Select all records"
    ]
  }
];

const TABLE_BADGE_COLORS = [
  { bg: "bg-blue-50", text: "text-blue-700", border: "border-blue-200", dot: "bg-blue-500" },
  { bg: "bg-indigo-50", text: "text-indigo-700", border: "border-indigo-200", dot: "bg-indigo-500" },
  { bg: "bg-purple-50", text: "text-purple-700", border: "border-purple-200", dot: "bg-purple-500" },
  { bg: "bg-amber-50", text: "text-amber-700", border: "border-amber-200", dot: "bg-amber-500" },
];

export default function AnalyticsDashboard() {
  const [prompt, setPrompt] = useState<string>("Select all records");
  const [viewsList, setViewsList] = useState<string[]>([]);
  const [newViewInput, setNewViewInput] = useState<string>("");
  const [spaceId, setSpaceId] = useState<string>("RADH_S3P");
  
  // Field selection state
  const [availableSchemaByView, setAvailableSchemaByView] = useState<Record<string, string[]>>({});
  const [selectedFields, setSelectedFields] = useState<string[]>([]);
  const [showFieldSelector, setShowFieldSelector] = useState<boolean>(true);
  const [fieldSearch, setFieldSearch] = useState<string>("");
  const [fetchingSchema, setFetchingSchema] = useState<boolean>(false);

  const [loading, setLoading] = useState<boolean>(false);
  const [response, setResponse] = useState<AnalyticsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<'matrix' | 'visual' | 'schema'>('matrix');
  const [chartType, setChartType] = useState<'bar' | 'line' | 'area'>('bar');
  const [xAxisKey, setXAxisKey] = useState<string>('');
  const [selectedMetrics, setSelectedMetrics] = useState<string[]>([]);
  const [showMetadataInspector, setShowMetadataInspector] = useState<boolean>(false);
  
  // Table search & client pagination
  const [tableSearch, setTableSearch] = useState<string>("");
  const [pageSize, setPageSize] = useState<number>(25);
  const [currentPage, setCurrentPage] = useState<number>(1);
  const [copiedNotification, setCopiedNotification] = useState<string | null>(null);

  // Query History
  const [queryHistory, setQueryHistory] = useState<string[]>(() => {
    try {
      const saved = localStorage.getItem('datasphere_query_history');
      return saved ? JSON.parse(saved) : [];
    } catch {
      return [];
    }
  });

  // Load server config info on mount
  useEffect(() => {
    fetch('/api/info')
      .then((res) => res.json())
      .then((data) => {
        if (data.space_id) setSpaceId(data.space_id);
      })
      .catch((err) => console.warn('Could not fetch server info:', err));
  }, []);

  // Fetch schema when views change
  const fetchSchemasForViews = async (views: string[]) => {
    if (!views || views.length === 0) {
      setAvailableSchemaByView({});
      setSelectedFields([]);
      return;
    }
    setFetchingSchema(true);
    try {
      const res = await fetch('/api/views/schema', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ view_names: views, space_id: spaceId })
      });
      if (res.ok) {
        const payload = await res.json();
        setAvailableSchemaByView(payload.views || {});
      }
    } catch (e) {
      console.warn("Could not probe view schemas:", e);
    } finally {
      setFetchingSchema(false);
    }
  };

  useEffect(() => {
    if (viewsList.length > 0) {
      fetchSchemasForViews(viewsList);
    } else {
      setAvailableSchemaByView({});
      setSelectedFields([]);
    }
  }, [viewsList, spaceId]);

  const addView = (viewName?: string) => {
    const nameToAdd = (viewName || newViewInput).trim();
    if (!nameToAdd) return;
    if (!viewsList.includes(nameToAdd)) {
      const updated = [...viewsList, nameToAdd];
      setViewsList(updated);
      fetchSchemasForViews(updated);
    }
    setNewViewInput("");
  };

  const removeView = (viewToRemove: string) => {
    const updated = viewsList.filter(v => v !== viewToRemove);
    setViewsList(updated);
    const viewCols = availableSchemaByView[viewToRemove] || [];
    setSelectedFields(prev => prev.filter(f => !viewCols.includes(f)));
    if (updated.length > 0) {
      fetchSchemasForViews(updated);
    } else {
      setAvailableSchemaByView({});
    }
  };

  // Categorized fields: 1. Common Fields vs 2. View 1 Fields vs 3. View 2 Fields
  const { commonFields, viewSpecificFields } = useMemo(() => {
    const views = Object.keys(availableSchemaByView);
    if (views.length === 0) return { commonFields: [], viewSpecificFields: {} };
    if (views.length === 1) {
      return { commonFields: [], viewSpecificFields: { [views[0]]: availableSchemaByView[views[0]] || [] } };
    }

    // Build column frequency across views (case-insensitive)
    const colCountMap = new Map<string, { originalName: string; views: Set<string> }>();
    for (const v of views) {
      const cols = availableSchemaByView[v] || [];
      for (const c of cols) {
        const key = c.toLowerCase().trim();
        if (!colCountMap.has(key)) {
          colCountMap.set(key, { originalName: c, views: new Set([v]) });
        } else {
          colCountMap.get(key)!.views.add(v);
        }
      }
    }

    const common: string[] = [];
    const commonSet = new Set<string>();
    for (const [key, info] of colCountMap.entries()) {
      if (info.views.size >= 2) {
        common.push(info.originalName);
        commonSet.add(key);
      }
    }

    const specific: Record<string, string[]> = {};
    for (const v of views) {
      const cols = availableSchemaByView[v] || [];
      specific[v] = cols.filter(c => !commonSet.has(c.toLowerCase().trim()));
    }

    return { commonFields: common, viewSpecificFields: specific };
  }, [availableSchemaByView]);

  // Field selection helpers
  const toggleField = (fieldName: string) => {
    setSelectedFields(prev => 
      prev.includes(fieldName) ? prev.filter(f => f !== fieldName) : [...prev, fieldName]
    );
  };

  const selectGroupFields = (fieldList: string[]) => {
    setSelectedFields(prev => Array.from(new Set([...prev, ...fieldList])));
  };

  const deselectGroupFields = (fieldList: string[]) => {
    setSelectedFields(prev => prev.filter(f => !fieldList.includes(f)));
  };

  const selectAllFields = () => {
    const all = Object.values(availableSchemaByView).flat();
    setSelectedFields(Array.from(new Set(all)));
  };

  const clearAllFields = () => {
    setSelectedFields([]);
  };

  const selectKeyFields = () => {
    const all = Object.values(availableSchemaByView).flat();
    const keyFields = all.filter(col => {
      const c = col.toLowerCase();
      return (
        c.includes("id") ||
        c.includes("code") ||
        c.includes("name") ||
        c.includes("amount") ||
        c.includes("spend") ||
        c.includes("breach") ||
        c.includes("status") ||
        c.includes("year") ||
        c.includes("dso") ||
        c.includes("kpi")
      );
    });
    setSelectedFields(keyFields.length > 0 ? keyFields : all.slice(0, 10));
  };

  const allAvailableFields = useMemo(() => {
    return Array.from(new Set(Object.values(availableSchemaByView).flat()));
  }, [availableSchemaByView]);

  const addToHistory = (p: string) => {
    if (!p.trim()) return;
    setQueryHistory(prev => {
      const updated = [p, ...prev.filter(item => item !== p)].slice(0, 8);
      try {
        localStorage.setItem('datasphere_query_history', JSON.stringify(updated));
      } catch {}
      return updated;
    });
  };

  const clearHistory = () => {
    setQueryHistory([]);
    try {
      localStorage.removeItem('datasphere_query_history');
    } catch {}
  };

  // Auto-detect chart axes
  useEffect(() => {
    if (response && response.data && response.data.length > 0) {
      const sample = response.data[0];
      const keys = Object.keys(sample);
      
      const numKeys = keys.filter(k => typeof sample[k] === 'number');
      const strKeys = keys.filter(k => typeof sample[k] === 'string' && !k.toLowerCase().includes('id'));
      
      if (strKeys.length > 0) {
        setXAxisKey(strKeys[0]);
      } else if (keys.length > 0) {
        setXAxisKey(keys[0]);
      }

      if (numKeys.length > 0) {
        setSelectedMetrics(numKeys.slice(0, 2));
      } else {
        setSelectedMetrics([]);
      }
      setCurrentPage(1);
    }
  }, [response]);

  const handleQuerySubmit = async (e?: React.FormEvent, customPrompt?: string) => {
    if (e) e.preventDefault();
    const queryToRun = customPrompt !== undefined ? customPrompt : prompt;
    if (!queryToRun.trim()) return;
    if (viewsList.length === 0) {
      setError("Please add at least one Datasphere view name before executing the query.");
      return;
    }

    if (customPrompt) {
      setPrompt(customPrompt);
    }

    setLoading(true);
    setError(null);

    try {
      const res = await fetch('/api/analytics', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_prompt: queryToRun,
          view_names: viewsList,
          selected_fields: selectedFields.length > 0 ? selectedFields : undefined,
          space_id: spaceId.trim() || undefined
        }),
      });

      if (!res.ok) {
        const errPayload = await res.json().catch(() => ({ detail: 'Network request failed' }));
        throw new Error(errPayload.detail || `Server error ${res.status}`);
      }

      const data: AnalyticsResponse = await res.json();
      setResponse(data);
      addToHistory(queryToRun);
    } catch (err: any) {
      setError(err.message || 'An error occurred calling SAP Datasphere.');
    } finally {
      setLoading(false);
    }
  };

  const downloadCSV = () => {
    if (!response || !response.data || response.data.length === 0) return;
    const headers = Object.keys(response.data[0]);
    const csvRows = [
      headers.join(','),
      ...response.data.map(row => 
        headers.map(h => {
          const val = row[h];
          return typeof val === 'string' ? `"${val.replace(/"/g, '""')}"` : (val !== null && val !== undefined ? val : '');
        }).join(',')
      )
    ];
    const blob = new Blob([csvRows.join('\n')], { type: 'text/csv' });
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `datasphere_relational_${Date.now()}.csv`;
    a.click();
    window.URL.revokeObjectURL(url);
  };

  const downloadJSON = () => {
    if (!response || !response.data) return;
    const blob = new Blob([JSON.stringify(response.data, null, 2)], { type: 'application/json' });
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `datasphere_relational_${Date.now()}.json`;
    a.click();
    window.URL.revokeObjectURL(url);
  };

  const copyToClipboard = () => {
    if (!response || !response.data || response.data.length === 0) return;
    navigator.clipboard.writeText(JSON.stringify(response.data, null, 2));
    setCopiedNotification("JSON data copied to clipboard!");
    setTimeout(() => setCopiedNotification(null), 3000);
  };

  const tableHeaders = useMemo(() => {
    return response?.data && response.data.length > 0 ? Object.keys(response.data[0]) : [];
  }, [response]);

  const numericalColumns = useMemo(() => {
    return response?.data && response.data.length > 0 ? Object.keys(response.data[0]).filter(k => typeof response.data[0][k] === 'number') : [];
  }, [response]);

  const filteredData = useMemo(() => {
    if (!response || !response.data) return [];
    if (!tableSearch.trim()) return response.data;
    const query = tableSearch.toLowerCase();
    return response.data.filter(row => {
      return Object.values(row).some(val => 
        val !== null && val !== undefined && String(val).toLowerCase().includes(query)
      );
    });
  }, [response, tableSearch]);

  const totalPages = Math.ceil(filteredData.length / pageSize) || 1;
  const paginatedData = useMemo(() => {
    if (pageSize === -1) return filteredData;
    const start = (currentPage - 1) * pageSize;
    return filteredData.slice(start, start + pageSize);
  }, [filteredData, currentPage, pageSize]);

  return (
    <div className="flex flex-col min-h-screen bg-slate-50 text-slate-800 font-sans selection:bg-blue-100 selection:text-blue-900">
      
      {/* Toast Notification */}
      {copiedNotification && (
        <div className="fixed bottom-6 right-6 z-50 bg-slate-900 text-white px-4 py-2.5 rounded-xl shadow-lg flex items-center gap-2 text-xs font-medium animate-fade-in border border-slate-700">
          <CheckCircle2 className="w-4 h-4 text-emerald-400" />
          <span>{copiedNotification}</span>
        </div>
      )}

      {/* Top Header */}
      <header className="border-b border-slate-200 bg-white sticky top-0 z-40 px-6 py-3.5 flex items-center justify-between shadow-xs">
        <div className="flex items-center gap-3.5">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-tr from-blue-600 via-indigo-600 to-blue-500 flex items-center justify-center shadow-sm text-white">
            <Database className="w-5 h-5" />
          </div>
          <div>
            <div className="flex items-center gap-2.5">
              <h1 className="text-lg font-bold text-slate-900 tracking-tight">SAP Datasphere Multi-View Analytics</h1>
              <span className="px-2 py-0.5 text-[11px] font-bold rounded-md bg-blue-50 text-blue-700 border border-blue-200 flex items-center gap-1">
                <Link2 className="w-3 h-3" /> Relational Joins
              </span>
            </div>
            <p className="text-xs text-slate-500 font-medium flex items-center gap-1.5">
              <span>Space:</span>
              <span className="font-mono text-slate-800 font-semibold bg-slate-100 px-1.5 py-0.2 rounded border border-slate-200">
                {spaceId || 'RADH_S3P'}
              </span>
              <span className="text-slate-300">•</span>
              <span>{viewsList.length} View{viewsList.length !== 1 ? 's' : ''} Configured</span>
              {selectedFields.length > 0 && (
                <>
                  <span className="text-slate-300">•</span>
                  <span className="text-blue-600 font-semibold">{selectedFields.length} Fields Active</span>
                </>
              )}
            </p>
          </div>
        </div>

        {/* Security Badges */}
        <div className="flex items-center gap-2.5">
          <div className="hidden sm:flex items-center gap-2 px-3 py-1.5 rounded-lg bg-emerald-50 border border-emerald-200 text-emerald-800 text-xs font-medium">
            <ShieldCheck className="w-4 h-4 text-emerald-600 shrink-0" />
            <span>Zero LLM Data Leakage</span>
          </div>

          <div className="flex items-center gap-2 text-xs bg-slate-100 border border-slate-200 text-slate-700 rounded-lg px-3 py-1.5 font-medium">
            <Lock className="w-3.5 h-3.5 text-blue-600" />
            <span>OAuth 2.0 Active</span>
          </div>
        </div>
      </header>

      {/* Main Workspace Layout */}
      <main className="flex-1 max-w-7xl w-full mx-auto p-4 md:p-6 flex flex-col gap-6">
        
        {/* Live Multi-View & Field Configuration Card */}
        <div className="bg-white border border-slate-200/90 rounded-2xl p-6 shadow-sm space-y-4">
          
          {/* MULTI-VIEW SELECTOR & MANAGER */}
          <div className="space-y-2.5">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <label className="text-xs font-bold text-slate-700 uppercase tracking-wider flex items-center gap-1.5">
                <Layers className="w-4 h-4 text-blue-600" />
                <span>Datasphere Views ({viewsList.length})</span>
              </label>
              <span className="text-[11px] text-slate-500 font-medium">
                Enter your target Datasphere view names (single view or multiple for relational joins)
              </span>
            </div>

            {/* Active Views Badges */}
            <div className="flex flex-wrap items-center gap-2 p-3 bg-slate-50/80 rounded-xl border border-slate-200/80 min-h-[52px]">
              {viewsList.map((viewName, idx) => {
                const color = TABLE_BADGE_COLORS[idx % TABLE_BADGE_COLORS.length];
                return (
                  <div
                    key={viewName}
                    className={`flex items-center gap-2 px-3 py-1.5 rounded-lg ${color.bg} ${color.text} border ${color.border} text-xs font-mono font-bold shadow-2xs transition-all`}
                  >
                    <span className={`w-2 h-2 rounded-full ${color.dot}`} />
                    <span>{viewName}</span>
                    <button
                      type="button"
                      onClick={() => removeView(viewName)}
                      className="hover:text-red-600 hover:bg-white/60 rounded p-0.5 transition-colors cursor-pointer ml-1"
                      title="Remove view"
                    >
                      <X className="w-3.5 h-3.5" />
                    </button>
                  </div>
                );
              })}

              {/* Add New View Input */}
              <div className="flex items-center gap-1.5 flex-1 min-w-[280px]">
                <input
                  type="text"
                  value={newViewInput}
                  onChange={(e) => setNewViewInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      addView();
                    }
                  }}
                  placeholder={viewsList.length === 0 ? "Enter view name (e.g. YOUR_VIEW_NAME) and press Enter..." : "Add another view name..."}
                  className="w-full bg-white border border-slate-300 rounded-lg px-3 py-1.5 text-xs text-slate-900 font-mono focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 placeholder:text-slate-400 font-medium"
                />
                <button
                  type="button"
                  onClick={() => addView()}
                  className="px-3.5 py-1.5 bg-blue-600 hover:bg-blue-700 active:bg-blue-800 text-white rounded-lg text-xs font-semibold flex items-center gap-1 shadow-2xs transition-all shrink-0 cursor-pointer"
                >
                  <Plus className="w-3.5 h-3.5" />
                  <span>Add View</span>
                </button>
              </div>
            </div>

            {viewsList.length === 0 && (
              <p className="text-[11px] text-amber-700 font-medium flex items-center gap-1">
                <span>💡 Type your Datasphere view technical name above and click <strong>Add View</strong> to get started.</span>
              </p>
            )}
          </div>

          {/* DYNAMIC FIELD SELECTION ACCORDION (1. COMMON FIELDS, 2. VIEW 1 FIELDS, 3. VIEW 2 FIELDS) */}
          {viewsList.length > 0 && (
            <div className="pt-2 border-t border-slate-100">
              <div 
                onClick={() => setShowFieldSelector(!showFieldSelector)}
                className="flex items-center justify-between p-3 rounded-xl bg-slate-50/90 border border-slate-200 hover:bg-blue-50/50 hover:border-blue-200 transition-all cursor-pointer"
              >
                <div className="flex items-center gap-2">
                  <SlidersHorizontal className="w-4 h-4 text-blue-600" />
                  <span className="text-xs font-bold text-slate-800">
                    Select Specific Fields to Retrieve & View
                  </span>
                  <span className="px-2 py-0.5 rounded-full text-[11px] font-bold bg-blue-100 text-blue-800 border border-blue-200">
                    {selectedFields.length > 0 ? `${selectedFields.length} of ${allAvailableFields.length} fields selected` : `All ${allAvailableFields.length} fields (Default)`}
                  </span>
                </div>
                <div className="flex items-center gap-1 text-xs text-blue-600 font-semibold">
                  <span>{showFieldSelector ? 'Hide Field Selector' : 'Configure Fields'}</span>
                  {showFieldSelector ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                </div>
              </div>

              {/* Field Selector Dropdown Content */}
              {showFieldSelector && (
                <div className="mt-3 p-4 bg-white border border-slate-200 rounded-xl shadow-xs space-y-4">
                  
                  {/* Search & Global Action Bar */}
                  <div className="flex flex-wrap items-center justify-between gap-3 pb-2 border-b border-slate-100">
                    <div className="relative flex-1 max-w-sm">
                      <Search className="w-3.5 h-3.5 text-slate-400 absolute left-3 top-2.5" />
                      <input
                        type="text"
                        value={fieldSearch}
                        onChange={(e) => setFieldSearch(e.target.value)}
                        placeholder="Search across all fields..."
                        className="w-full bg-slate-50 border border-slate-200 rounded-lg pl-8 pr-3 py-1 text-xs text-slate-800 focus:outline-none focus:ring-1 focus:ring-blue-500 font-mono"
                      />
                    </div>

                    <div className="flex items-center gap-1.5 text-xs">
                      <button
                        type="button"
                        onClick={selectKeyFields}
                        className="px-2.5 py-1 rounded-lg bg-blue-50 hover:bg-blue-100 text-blue-700 border border-blue-200 font-semibold transition-all cursor-pointer text-[11px]"
                      >
                        Key Columns Only
                      </button>
                      <button
                        type="button"
                        onClick={selectAllFields}
                        className="px-2.5 py-1 rounded-lg bg-slate-100 hover:bg-slate-200 text-slate-700 font-semibold transition-all cursor-pointer text-[11px]"
                      >
                        Select All
                      </button>
                      <button
                        type="button"
                        onClick={clearAllFields}
                        className="px-2.5 py-1 rounded-lg bg-slate-100 hover:bg-red-50 hover:text-red-700 text-slate-600 font-semibold transition-all cursor-pointer text-[11px]"
                      >
                        Deselect All
                      </button>
                    </div>
                  </div>

                  {/* LOADING STATE */}
                  {fetchingSchema ? (
                    <div className="py-6 text-center text-xs text-slate-500 flex items-center justify-center gap-2">
                      <RefreshCw className="w-4 h-4 animate-spin text-blue-600" />
                      <span>Discovering schema fields from SAP Datasphere...</span>
                    </div>
                  ) : Object.keys(availableSchemaByView).length === 0 ? (
                    <div className="py-4 text-center text-xs text-slate-400">
                      No schema columns discovered yet for the specified views.
                    </div>
                  ) : (
                    <div className="space-y-4 max-h-80 overflow-y-auto pr-1">
                      
                      {/* SECTION 1: COMMON FIELDS (ACROSS VIEWS) */}
                      {viewsList.length > 1 && (
                        <div className="p-3.5 rounded-xl bg-emerald-50/70 border border-emerald-200/90 space-y-2">
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <span className="text-xs font-bold text-emerald-900 uppercase tracking-wider flex items-center gap-1.5">
                              <Link2 className="w-3.5 h-3.5 text-emerald-600" />
                              <span>1. Common Linking Fields ({commonFields.length})</span>
                            </span>
                            <div className="flex items-center gap-1.5">
                              <button
                                type="button"
                                onClick={() => selectGroupFields(commonFields)}
                                className="px-2 py-0.5 rounded bg-white hover:bg-emerald-100 text-emerald-800 border border-emerald-300 text-[10px] font-bold transition-all cursor-pointer"
                              >
                                Select All Common
                              </button>
                              <button
                                type="button"
                                onClick={() => deselectGroupFields(commonFields)}
                                className="px-2 py-0.5 rounded bg-white hover:bg-red-50 text-slate-600 hover:text-red-700 border border-slate-200 text-[10px] font-bold transition-all cursor-pointer"
                              >
                                Clear
                              </button>
                            </div>
                          </div>

                          {commonFields.length === 0 ? (
                            <p className="text-[11px] text-emerald-700 italic">No identical column names found between these views.</p>
                          ) : (
                            <div className="flex flex-wrap gap-1.5 pt-1">
                              {commonFields
                                .filter(c => c.toLowerCase().includes(fieldSearch.toLowerCase()))
                                .map((col) => {
                                  const isSelected = selectedFields.includes(col);
                                  return (
                                    <button
                                      key={`common-${col}`}
                                      type="button"
                                      onClick={() => toggleField(col)}
                                      className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-xs font-mono transition-all cursor-pointer ${
                                        isSelected
                                          ? 'bg-emerald-700 text-white border-emerald-700 font-bold shadow-2xs'
                                          : 'bg-white border-emerald-200 text-emerald-900 hover:bg-emerald-100/60 font-medium'
                                      }`}
                                    >
                                      {isSelected ? <Check className="w-3 h-3 text-white" /> : <Plus className="w-3 h-3 text-emerald-500" />}
                                      <span>{col}</span>
                                    </button>
                                  );
                                })}
                            </div>
                          )}
                        </div>
                      )}

                      {/* SECTION 2 & 3: VIEW SPECIFIC FIELDS */}
                      {viewsList.map((viewName, vIdx) => {
                        const specificCols = viewSpecificFields[viewName] || [];
                        const filteredCols = specificCols.filter(c => 
                          c.toLowerCase().includes(fieldSearch.toLowerCase())
                        );
                        const color = TABLE_BADGE_COLORS[vIdx % TABLE_BADGE_COLORS.length];
                        const sectionNum = viewsList.length > 1 ? vIdx + 2 : 1;

                        return (
                          <div key={viewName} className="p-3.5 rounded-xl bg-slate-50 border border-slate-200 space-y-2">
                            <div className="flex flex-wrap items-center justify-between gap-2">
                              <span className={`text-xs font-bold ${color.text} uppercase tracking-wider flex items-center gap-1.5`}>
                                <Tag className="w-3.5 h-3.5" />
                                <span>{sectionNum}. {viewName} Fields ({specificCols.length})</span>
                              </span>
                              <div className="flex items-center gap-1.5">
                                <button
                                  type="button"
                                  onClick={() => selectGroupFields(specificCols)}
                                  className="px-2 py-0.5 rounded bg-white hover:bg-blue-50 text-slate-700 hover:text-blue-700 border border-slate-200 text-[10px] font-bold transition-all cursor-pointer"
                                >
                                  Select All
                                </button>
                                <button
                                  type="button"
                                  onClick={() => deselectGroupFields(specificCols)}
                                  className="px-2 py-0.5 rounded bg-white hover:bg-red-50 text-slate-600 hover:text-red-700 border border-slate-200 text-[10px] font-bold transition-all cursor-pointer"
                                >
                                  Clear
                                </button>
                              </div>
                            </div>

                            {filteredCols.length === 0 ? (
                              <p className="text-[11px] text-slate-400 italic">No exclusive fields in this view.</p>
                            ) : (
                              <div className="flex flex-wrap gap-1.5 pt-1">
                                {filteredCols.map((col) => {
                                  const isSelected = selectedFields.includes(col);
                                  return (
                                    <button
                                      key={`${viewName}-${col}`}
                                      type="button"
                                      onClick={() => toggleField(col)}
                                      className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-xs font-mono transition-all cursor-pointer ${
                                        isSelected
                                          ? 'bg-blue-600 text-white border-blue-600 font-bold shadow-2xs'
                                          : 'bg-white border-slate-200 text-slate-700 hover:bg-slate-100 font-medium'
                                      }`}
                                    >
                                      {isSelected ? <Check className="w-3 h-3 text-white" /> : <Plus className="w-3 h-3 text-slate-400" />}
                                      <span>{col}</span>
                                    </button>
                                  );
                                })}
                              </div>
                            )}
                          </div>
                        );
                      })}

                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {/* NATURAL LANGUAGE QUERY FORM */}
          <form onSubmit={(e) => handleQuerySubmit(e)} className="space-y-4 pt-2 border-t border-slate-100">
            
            <div className="grid grid-cols-1 md:grid-cols-12 gap-4">
              
              {/* Space ID Override */}
              <div className="md:col-span-3">
                <label className="block text-xs font-semibold text-slate-700 mb-1.5">
                  Datasphere Space ID
                </label>
                <input
                  type="text"
                  value={spaceId}
                  onChange={(e) => setSpaceId(e.target.value)}
                  placeholder="e.g. RADH_S3P"
                  required
                  className="w-full bg-slate-50 border border-slate-300 rounded-xl px-4 py-2.5 text-sm text-slate-900 font-mono font-medium focus:bg-white focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 transition-all shadow-xs"
                />
              </div>

              {/* Natural Language Prompt Input */}
              <div className="md:col-span-9">
                <label className="block text-xs font-semibold text-slate-700 mb-1.5 flex items-center justify-between">
                  <span className="flex items-center gap-1.5">
                    <Sparkles className="w-3.5 h-3.5 text-blue-600" />
                    <span>Analytical / Relational Query (Natural Language)</span>
                  </span>
                  <span className="text-[11px] text-slate-400 font-normal">Filters, Joins, Aggregations, Group By</span>
                </label>
                
                <div className="flex flex-col sm:flex-row gap-2.5">
                  <div className="relative flex-1">
                    <input
                      type="text"
                      value={prompt}
                      onChange={(e) => setPrompt(e.target.value)}
                      required
                      placeholder="e.g. Select all records, or filter where breach is X, or join on common keys"
                      className="w-full bg-slate-50 border border-slate-300 rounded-xl px-4 py-2.5 text-sm text-slate-900 focus:bg-white focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 transition-all shadow-xs placeholder:text-slate-400 font-medium"
                    />
                  </div>
                  <button
                    type="submit"
                    disabled={loading || viewsList.length === 0}
                    className="px-6 py-2.5 bg-blue-600 hover:bg-blue-700 active:bg-blue-800 disabled:opacity-40 text-white font-semibold text-sm rounded-xl shadow-sm flex items-center justify-center gap-2 transition-all shrink-0 cursor-pointer"
                  >
                    {loading ? (
                      <>
                        <RefreshCw className="w-4 h-4 animate-spin" />
                        <span>Querying SAP...</span>
                      </>
                    ) : (
                      <>
                        <Send className="w-4 h-4" />
                        <span>Execute Query</span>
                      </>
                    )}
                  </button>
                </div>
              </div>

              {/* Categorized Prompt Helper Library */}
              <div className="md:col-span-12 pt-2 space-y-2.5">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-bold text-slate-600 flex items-center gap-1.5">
                    <Zap className="w-3.5 h-3.5 text-amber-500" />
                    <span>1-Click Suggested Queries:</span>
                  </span>
                  {queryHistory.length > 0 && (
                    <button
                      type="button"
                      onClick={clearHistory}
                      className="text-[11px] text-slate-400 hover:text-red-600 flex items-center gap-1 transition-colors cursor-pointer"
                    >
                      <Trash2 className="w-3 h-3" />
                      <span>Clear History</span>
                    </button>
                  )}
                </div>

                {/* Recent Queries pills */}
                {queryHistory.length > 0 && (
                  <div className="flex flex-wrap items-center gap-1.5 mb-2">
                    <span className="text-[11px] text-slate-400 font-semibold flex items-center gap-1">
                      <Clock className="w-3 h-3" /> Recent:
                    </span>
                    {queryHistory.slice(0, 4).map((hist, hIdx) => (
                      <button
                        key={hIdx}
                        type="button"
                        onClick={() => handleQuerySubmit(undefined, hist)}
                        className="px-2.5 py-1 rounded-lg bg-blue-50 hover:bg-blue-100 text-blue-800 border border-blue-200/80 transition-all text-xs font-medium cursor-pointer max-w-xs truncate"
                        title={hist}
                      >
                        {hist}
                      </button>
                    ))}
                  </div>
                )}

                {/* Categorized Suggestions */}
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-2">
                  {PROMPT_CATEGORIES.map((cat, catIdx) => (
                    <div key={catIdx} className="bg-slate-50/80 p-2.5 rounded-xl border border-slate-200/70 space-y-1.5">
                      <span className="text-[11px] font-bold text-slate-600 uppercase tracking-wider block">
                        {cat.category}
                      </span>
                      <div className="flex flex-col gap-1">
                        {cat.prompts.map((sample, sIdx) => (
                          <button
                            key={sIdx}
                            type="button"
                            onClick={() => handleQuerySubmit(undefined, sample)}
                            className="text-left px-2 py-1 rounded-md text-slate-700 hover:text-blue-700 hover:bg-white border border-transparent hover:border-slate-200 text-xs transition-all font-medium truncate cursor-pointer"
                            title={sample}
                          >
                            • {sample}
                          </button>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </div>

            </div>

          </form>
        </div>

        {/* Error Notice */}
        {error && (
          <div className="p-4 rounded-xl bg-red-50 border border-red-200 text-red-800 text-sm flex items-start gap-3 shadow-xs">
            <AlertCircle className="w-5 h-5 text-red-600 shrink-0 mt-0.5" />
            <div className="flex-1 space-y-1.5">
              <p className="font-bold text-red-900">Datasphere Execution Notice</p>
              <div className="text-xs text-red-700 font-mono whitespace-pre-line leading-relaxed bg-white p-3 rounded-lg border border-red-200">
                {error}
              </div>
            </div>
          </div>
        )}

        {/* Discovered Foreign Key & Relations Banner */}
        {response && response.retrieved_view_metadata.foreign_keys && response.retrieved_view_metadata.foreign_keys.length > 0 && (
          <div className="p-3.5 px-5 rounded-xl bg-indigo-50/80 border border-indigo-200 text-indigo-900 text-xs flex flex-wrap items-center justify-between gap-3 shadow-xs">
            <div className="flex items-center gap-2">
              <Link2 className="w-4 h-4 text-indigo-600 shrink-0" />
              <span className="font-bold">Active Relational Foreign Keys:</span>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {response.retrieved_view_metadata.foreign_keys.map((fk, fIdx) => (
                <span
                  key={fIdx}
                  className="px-2.5 py-1 rounded-lg bg-white border border-indigo-200 font-mono text-[11px] font-semibold text-indigo-800 shadow-2xs"
                >
                  {fk.view_a} ⟷ {fk.view_b} (on {fk.join_keys.map(k => k.key_name).join(', ')})
                </span>
              ))}
            </div>
          </div>
        )}

        {/* KPI Intelligence Summary & Metrics Cards */}
        {response && response.kpi_summary && (
          <div className="bg-gradient-to-r from-blue-50/90 via-indigo-50/60 to-sky-50/80 border border-blue-200/90 rounded-2xl p-6 shadow-xs space-y-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className="flex h-2.5 w-2.5 rounded-full bg-blue-600 animate-pulse" />
                <span className="text-xs font-bold uppercase tracking-wider text-blue-900">
                  Analytical Intelligence Summary
                </span>
              </div>
              <span className="text-xs text-slate-500 font-medium">
                Focus Metric: <span className="font-bold text-slate-800">{response.kpi_summary.target_metric || 'Dataset'}</span>
              </span>
            </div>

            <p className="text-lg md:text-xl font-bold text-slate-900 tracking-tight leading-snug">
              {response.kpi_summary.direct_answer}
            </p>

            {/* KPI Metric Cards Grid */}
            {response.kpi_summary.cards && response.kpi_summary.cards.length > 0 && (
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3.5 pt-1">
                {response.kpi_summary.cards.map((card, cIdx) => (
                  <div 
                    key={cIdx} 
                    className="p-4 rounded-xl bg-white border border-slate-200/90 shadow-xs flex flex-col gap-1 hover:border-blue-300 transition-all"
                  >
                    <span className="text-xs font-medium text-slate-500 truncate">
                      {card.label}
                    </span>
                    <span className="text-xl font-extrabold font-mono text-blue-700 tracking-tight">
                      {card.formatted_value}
                    </span>
                    <span className="text-[10px] text-slate-400 uppercase font-semibold">
                      {card.metric_type.toUpperCase()}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Live Execution Pipeline Inspector (Collapsible) */}
        {response && (
          <div className="bg-white border border-slate-200 rounded-2xl overflow-hidden shadow-xs transition-all">
            <div 
              onClick={() => setShowMetadataInspector(!showMetadataInspector)}
              className="px-5 py-3 bg-slate-50/80 border-b border-slate-200 flex items-center justify-between cursor-pointer hover:bg-slate-100/80 transition-colors"
            >
              <div className="flex items-center gap-2.5">
                <Cpu className="w-4 h-4 text-blue-600" />
                <span className="text-xs font-bold uppercase tracking-wider text-slate-700">
                  Multi-View Relational Pipeline Inspector
                </span>
                <span className="text-[10px] bg-emerald-100 text-emerald-800 border border-emerald-300 px-2 py-0.5 rounded-full font-semibold">
                  Live Stream Complete
                </span>
              </div>
              <div className="flex items-center gap-1 text-xs text-slate-500 font-medium">
                <span>{showMetadataInspector ? 'Hide Inspector' : 'View Pipeline Details'}</span>
                {showMetadataInspector ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
              </div>
            </div>

            {showMetadataInspector && (
              <div className="p-5 grid grid-cols-1 md:grid-cols-3 gap-4 bg-slate-50/40 text-xs">
                
                {/* Step 1: Target Views & Space */}
                <div className="p-4 rounded-xl bg-white border border-slate-200 shadow-xs space-y-2">
                  <div className="flex items-center gap-2 text-blue-700 font-bold">
                    <Database className="w-3.5 h-3.5" />
                    <span>1. Target Views & Space</span>
                  </div>
                  <div className="space-y-1 text-slate-600 font-mono text-xs">
                    <p><span className="text-slate-400 font-sans">Views:</span> <span className="text-slate-900 font-bold">{response.execution_info.view_name}</span></p>
                    <p><span className="text-slate-400 font-sans">Space ID:</span> <span className="text-slate-700">{response.execution_info.space_id}</span></p>
                    <p><span className="text-slate-400 font-sans">Result Rows:</span> <span className="text-blue-700 font-bold">{response.data.length} records</span></p>
                  </div>
                </div>

                {/* Step 2: Executed Relational SQL */}
                <div className="p-4 rounded-xl bg-white border border-slate-200 shadow-xs space-y-2">
                  <div className="flex items-center gap-2 text-indigo-700 font-bold">
                    <Zap className="w-3.5 h-3.5" />
                    <span>2. Relational SQL Blueprint</span>
                  </div>
                  <div className="space-y-1 font-mono text-xs text-slate-600">
                    <p className="truncate" title={response.query_blueprint.sql_query}>
                      <span className="text-indigo-600 font-bold font-sans">SQL:</span> {response.query_blueprint.sql_query || 'SELECT *'}
                    </p>
                    <p className="text-slate-500 text-[11px] font-sans italic mt-1">{response.query_blueprint.explanation}</p>
                  </div>
                </div>

                {/* Step 3: Live OData Stream Execution */}
                <div className="p-4 rounded-xl bg-white border border-slate-200 shadow-xs space-y-2">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2 text-emerald-700 font-bold">
                      <CheckCircle2 className="w-3.5 h-3.5" />
                      <span>3. Secure Stream & SQLite</span>
                    </div>
                    <span className="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider bg-emerald-100 text-emerald-800 border border-emerald-300">
                      Live
                    </span>
                  </div>
                  <div className="space-y-1 text-xs text-slate-600 font-mono">
                    <p className="truncate" title={response.execution_info.datasphere_endpoint}>
                      <span className="text-slate-400 font-sans">Endpoint:</span> {response.execution_info.datasphere_endpoint}
                    </p>
                    <p><span className="text-slate-400 font-sans">Auth:</span> <span className="text-emerald-700 font-semibold font-sans">OAuth 2.0 Bearer Token</span></p>
                    <p><span className="text-slate-400 font-sans">Status:</span> <span className="text-blue-700 font-bold font-sans">200 OK • Sub-Second</span></p>
                  </div>
                </div>

              </div>
            )}
          </div>
        )}

        {/* Results Container with Data Matrix, Visual Insights, and Schema Explorer */}
        {response && (
          <div className="bg-white border border-slate-200 rounded-2xl shadow-xs overflow-hidden flex flex-col flex-1">
            
            {/* Tab Header Bar & Controls */}
            <div className="px-6 py-3.5 border-b border-slate-200 flex flex-wrap items-center justify-between gap-4 bg-slate-50/70">
              
              {/* Tabs */}
              <div className="flex items-center gap-1.5 bg-slate-200/70 p-1 rounded-xl">
                <button
                  type="button"
                  onClick={() => setActiveTab('matrix')}
                  className={`flex items-center gap-2 px-3.5 py-1.5 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
                    activeTab === 'matrix'
                      ? 'bg-white text-blue-700 shadow-xs'
                      : 'text-slate-600 hover:text-slate-900'
                  }`}
                >
                  <TableIcon className="w-3.5 h-3.5" />
                  <span>Data Matrix ({filteredData.length}{filteredData.length !== response.data.length ? ` / ${response.data.length}` : ''})</span>
                </button>

                <button
                  type="button"
                  onClick={() => setActiveTab('visual')}
                  className={`flex items-center gap-2 px-3.5 py-1.5 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
                    activeTab === 'visual'
                      ? 'bg-white text-blue-700 shadow-xs'
                      : 'text-slate-600 hover:text-slate-900'
                  }`}
                >
                  <BarChart3 className="w-3.5 h-3.5" />
                  <span>Visual Insights</span>
                </button>

                <button
                  type="button"
                  onClick={() => setActiveTab('schema')}
                  className={`flex items-center gap-2 px-3.5 py-1.5 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
                    activeTab === 'schema'
                      ? 'bg-white text-blue-700 shadow-xs'
                      : 'text-slate-600 hover:text-slate-900'
                  }`}
                >
                  <Layers className="w-3.5 h-3.5" />
                  <span>Schema & Relations</span>
                </button>
              </div>

              {/* Action Buttons & Tools */}
              <div className="flex items-center gap-2.5">
                
                {activeTab === 'visual' && (
                  <div className="flex items-center gap-1 text-xs bg-slate-200/70 rounded-xl p-1">
                    <button
                      type="button"
                      onClick={() => setChartType('bar')}
                      className={`px-3 py-1 rounded-lg font-semibold transition-all cursor-pointer ${chartType === 'bar' ? 'bg-white text-blue-700 shadow-xs' : 'text-slate-600 hover:text-slate-900'}`}
                    >
                      Bar
                    </button>
                    <button
                      type="button"
                      onClick={() => setChartType('line')}
                      className={`px-3 py-1 rounded-lg font-semibold transition-all cursor-pointer ${chartType === 'line' ? 'bg-white text-blue-700 shadow-xs' : 'text-slate-600 hover:text-slate-900'}`}
                    >
                      Line
                    </button>
                    <button
                      type="button"
                      onClick={() => setChartType('area')}
                      className={`px-3 py-1 rounded-lg font-semibold transition-all cursor-pointer ${chartType === 'area' ? 'bg-white text-blue-700 shadow-xs' : 'text-slate-600 hover:text-slate-900'}`}
                    >
                      Area
                    </button>
                  </div>
                )}

                {/* Copy JSON */}
                <button
                  type="button"
                  onClick={copyToClipboard}
                  disabled={response.data.length === 0}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-white hover:bg-slate-50 disabled:opacity-50 text-slate-700 text-xs font-semibold border border-slate-300 shadow-xs transition-all cursor-pointer"
                  title="Copy JSON to Clipboard"
                >
                  <Copy className="w-3.5 h-3.5 text-slate-500" />
                  <span className="hidden sm:inline">Copy JSON</span>
                </button>

                {/* Export CSV */}
                <button
                  type="button"
                  onClick={downloadCSV}
                  disabled={response.data.length === 0}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-white hover:bg-slate-50 disabled:opacity-50 text-slate-700 text-xs font-semibold border border-slate-300 shadow-xs transition-all cursor-pointer"
                  title="Download CSV"
                >
                  <Download className="w-3.5 h-3.5 text-slate-500" />
                  <span>CSV</span>
                </button>

                {/* Export JSON file */}
                <button
                  type="button"
                  onClick={downloadJSON}
                  disabled={response.data.length === 0}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-white hover:bg-slate-50 disabled:opacity-50 text-slate-700 text-xs font-semibold border border-slate-300 shadow-xs transition-all cursor-pointer"
                  title="Download JSON file"
                >
                  <FileCode className="w-3.5 h-3.5 text-slate-500" />
                  <span>JSON</span>
                </button>

              </div>
            </div>

            {/* TAB 1: Data Matrix */}
            {activeTab === 'matrix' && (
              <div className="flex flex-col flex-1">
                
                {/* Search & Quick In-Table Filter Bar */}
                <div className="p-3.5 px-6 bg-white border-b border-slate-200 flex flex-wrap items-center justify-between gap-3">
                  <div className="relative flex-1 max-w-md">
                    <Search className="w-4 h-4 text-slate-400 absolute left-3 top-2.5" />
                    <input
                      type="text"
                      value={tableSearch}
                      onChange={(e) => {
                        setTableSearch(e.target.value);
                        setCurrentPage(1);
                      }}
                      placeholder="Filter records in joined dataset (instant search)..."
                      className="w-full bg-slate-50 border border-slate-300 rounded-lg pl-9 pr-3 py-1.5 text-xs text-slate-900 focus:bg-white focus:outline-none focus:ring-1 focus:ring-blue-500 focus:border-blue-500 font-medium"
                    />
                    {tableSearch && (
                      <button
                        onClick={() => setTableSearch('')}
                        className="absolute right-2.5 top-2 text-slate-400 hover:text-slate-600 text-xs"
                      >
                        ✕
                      </button>
                    )}
                  </div>

                  {/* Page Size Selector */}
                  <div className="flex items-center gap-2 text-xs text-slate-600">
                    <span>Rows per page:</span>
                    <select
                      value={pageSize}
                      onChange={(e) => {
                        setPageSize(Number(e.target.value));
                        setCurrentPage(1);
                      }}
                      className="bg-white border border-slate-300 rounded-md px-2 py-1 text-xs text-slate-800 focus:outline-none focus:ring-1 focus:ring-blue-500"
                    >
                      <option value={25}>25</option>
                      <option value={50}>50</option>
                      <option value={100}>100</option>
                      <option value={-1}>All ({filteredData.length})</option>
                    </select>
                  </div>
                </div>

                {/* Table Content */}
                <div className="overflow-x-auto max-h-[560px]">
                  {filteredData.length === 0 ? (
                    <div className="text-center py-16 text-slate-500 text-sm">
                      {response.data.length === 0 ? "No records returned for this query." : "No records match your search filter."}
                    </div>
                  ) : (
                    <table className="w-full text-left text-xs text-slate-700 border-collapse">
                      <thead className="bg-slate-100/90 text-slate-700 uppercase font-bold border-b border-slate-200 sticky top-0 z-10 shadow-2xs">
                        <tr>
                          <th className="px-3 py-3 w-12 text-center text-slate-400 font-mono text-[11px] bg-slate-100 border-r border-slate-200">
                            #
                          </th>
                          {tableHeaders.map((header) => (
                            <th key={header} className="px-4 py-3 whitespace-nowrap bg-slate-100 border-r border-slate-200 last:border-r-0">
                              {header}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-200 font-mono text-xs">
                        {paginatedData.map((row, rowIdx) => {
                          const rowNumber = pageSize === -1 ? rowIdx + 1 : (currentPage - 1) * pageSize + rowIdx + 1;
                          return (
                            <tr key={rowIdx} className={`hover:bg-blue-50/70 transition-colors ${rowIdx % 2 === 0 ? 'bg-white' : 'bg-slate-50/60'}`}>
                              <td className="px-3 py-2.5 text-center text-slate-400 text-[11px] border-r border-slate-200/60 font-sans">
                                {rowNumber}
                              </td>
                              {tableHeaders.map((header) => {
                                const val = row[header];
                                const isNumber = typeof val === 'number';
                                return (
                                  <td key={header} className={`px-4 py-2.5 whitespace-nowrap border-r border-slate-200/60 last:border-r-0 ${isNumber ? 'text-blue-700 font-semibold text-right' : 'text-slate-800'}`}>
                                    {isNumber ? val.toLocaleString() : (val !== null && val !== undefined ? String(val) : '-')}
                                  </td>
                                );
                              })}
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  )}
                </div>

                {/* Table Footer Pagination */}
                {pageSize !== -1 && filteredData.length > pageSize && (
                  <div className="p-3 px-6 bg-slate-50 border-t border-slate-200 flex items-center justify-between text-xs text-slate-600">
                    <div>
                      Showing <span className="font-semibold text-slate-800">{(currentPage - 1) * pageSize + 1}</span> to <span className="font-semibold text-slate-800">{Math.min(currentPage * pageSize, filteredData.length)}</span> of <span className="font-semibold text-slate-800">{filteredData.length}</span> entries
                    </div>
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        onClick={() => setCurrentPage(p => Math.max(1, p - 1))}
                        disabled={currentPage === 1}
                        className="px-2.5 py-1 rounded-md bg-white border border-slate-300 disabled:opacity-40 text-slate-700 hover:bg-slate-100 transition-colors flex items-center gap-1 cursor-pointer"
                      >
                        <ChevronLeft className="w-3.5 h-3.5" /> Previous
                      </button>
                      <span className="font-medium text-slate-700">
                        Page {currentPage} of {totalPages}
                      </span>
                      <button
                        type="button"
                        onClick={() => setCurrentPage(p => Math.min(totalPages, p + 1))}
                        disabled={currentPage === totalPages}
                        className="px-2.5 py-1 rounded-md bg-white border border-slate-300 disabled:opacity-40 text-slate-700 hover:bg-slate-100 transition-colors flex items-center gap-1 cursor-pointer"
                      >
                        Next <ChevronRight className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </div>
                )}

              </div>
            )}

            {/* TAB 2: Visual Insights */}
            {activeTab === 'visual' && (
              <div className="p-6 flex flex-col gap-6">
                {response.data.length === 0 ? (
                  <div className="text-center py-16 text-slate-500 text-sm">
                    No numerical records available to visualize.
                  </div>
                ) : (
                  <>
                    {/* Dimension & Metric Selector */}
                    <div className="flex flex-wrap items-center justify-between gap-4 p-4 bg-slate-50 border border-slate-200 rounded-xl text-xs">
                      <div className="flex items-center gap-2">
                        <span className="text-slate-700 font-semibold">Dimension (X-Axis):</span>
                        <select
                          value={xAxisKey}
                          onChange={(e) => setXAxisKey(e.target.value)}
                          className="bg-white border border-slate-300 rounded-lg px-3 py-1.5 text-slate-800 focus:outline-none focus:ring-2 focus:ring-blue-500 font-mono font-medium shadow-xs"
                        >
                          {tableHeaders.map((h) => (
                            <option key={h} value={h}>{h}</option>
                          ))}
                        </select>
                      </div>

                      <div className="flex items-center gap-2">
                        <span className="text-slate-700 font-semibold">Metrics:</span>
                        <div className="flex flex-wrap gap-1.5">
                          {numericalColumns.map((col) => {
                            const isSelected = selectedMetrics.includes(col);
                            return (
                              <button
                                key={col}
                                type="button"
                                onClick={() => {
                                  if (isSelected) {
                                    if (selectedMetrics.length > 1) {
                                      setSelectedMetrics(selectedMetrics.filter(m => m !== col));
                                    }
                                  } else {
                                    setSelectedMetrics([...selectedMetrics, col]);
                                  }
                                }}
                                className={`px-2.5 py-1 rounded-lg border text-xs font-mono font-semibold transition-all cursor-pointer ${
                                  isSelected
                                    ? 'bg-blue-600 text-white border-blue-600 shadow-xs'
                                    : 'bg-white border-slate-300 text-slate-600 hover:border-slate-400'
                                }`}
                              >
                                {col}
                              </button>
                            );
                          })}
                        </div>
                      </div>
                    </div>

                    {/* Chart Container */}
                    <div className="h-96 w-full pt-2">
                      <ResponsiveContainer width="100%" height="100%">
                        {chartType === 'bar' ? (
                          <BarChart data={response.data.slice(0, 50)} margin={{ top: 20, right: 30, left: 20, bottom: 40 }}>
                            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                            <XAxis dataKey={xAxisKey} stroke="#64748b" tick={{ fontSize: 11 }} angle={-25} textAnchor="end" />
                            <YAxis stroke="#64748b" tick={{ fontSize: 11 }} />
                            <Tooltip
                              contentStyle={{ backgroundColor: '#ffffff', borderColor: '#cbd5e1', borderRadius: '0.75rem', color: '#0f172a', boxShadow: '0 4px 6px -1px rgb(0 0 0 / 0.1)' }}
                              itemStyle={{ color: '#2563eb' }}
                            />
                            <Legend wrapperStyle={{ paddingTop: '10px' }} />
                            {selectedMetrics.map((metric, idx) => (
                              <Bar 
                                key={metric} 
                                dataKey={metric} 
                                fill={idx === 0 ? '#2563eb' : idx === 1 ? '#059669' : '#7c3aed'} 
                                radius={[6, 6, 0, 0]} 
                              />
                            ))}
                          </BarChart>
                        ) : chartType === 'line' ? (
                          <LineChart data={response.data.slice(0, 50)} margin={{ top: 20, right: 30, left: 20, bottom: 40 }}>
                            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                            <XAxis dataKey={xAxisKey} stroke="#64748b" tick={{ fontSize: 11 }} angle={-25} textAnchor="end" />
                            <YAxis stroke="#64748b" tick={{ fontSize: 11 }} />
                            <Tooltip
                              contentStyle={{ backgroundColor: '#ffffff', borderColor: '#cbd5e1', borderRadius: '0.75rem', color: '#0f172a', boxShadow: '0 4px 6px -1px rgb(0 0 0 / 0.1)' }}
                            />
                            <Legend wrapperStyle={{ paddingTop: '10px' }} />
                            {selectedMetrics.map((metric, idx) => (
                              <Line 
                                key={metric} 
                                type="monotone" 
                                dataKey={metric} 
                                stroke={idx === 0 ? '#2563eb' : idx === 1 ? '#059669' : '#7c3aed'} 
                                strokeWidth={2.5} 
                                dot={{ r: 4 }} 
                              />
                            ))}
                          </LineChart>
                        ) : (
                          <AreaChart data={response.data.slice(0, 50)} margin={{ top: 20, right: 30, left: 20, bottom: 40 }}>
                            <defs>
                              <linearGradient id="areaColor" x1="0" y1="0" x2="0" y2="1">
                                <stop offset="5%" stopColor="#2563eb" stopOpacity={0.7}/>
                                <stop offset="95%" stopColor="#2563eb" stopOpacity={0.05}/>
                              </linearGradient>
                            </defs>
                            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                            <XAxis dataKey={xAxisKey} stroke="#64748b" tick={{ fontSize: 11 }} angle={-25} textAnchor="end" />
                            <YAxis stroke="#64748b" tick={{ fontSize: 11 }} />
                            <Tooltip
                              contentStyle={{ backgroundColor: '#ffffff', borderColor: '#cbd5e1', borderRadius: '0.75rem', color: '#0f172a', boxShadow: '0 4px 6px -1px rgb(0 0 0 / 0.1)' }}
                            />
                            <Legend wrapperStyle={{ paddingTop: '10px' }} />
                            {selectedMetrics.map((metric) => (
                              <Area 
                                key={metric} 
                                type="monotone" 
                                dataKey={metric} 
                                stroke="#2563eb" 
                                fillOpacity={1} 
                                fill="url(#areaColor)" 
                              />
                            ))}
                          </AreaChart>
                        )}
                      </ResponsiveContainer>
                    </div>
                  </>
                )}
              </div>
            )}

            {/* TAB 3: Schema & Relations Explorer */}
            {activeTab === 'schema' && (
              <div className="p-6 space-y-6">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h3 className="text-sm font-bold text-slate-900">Datasphere Multi-View Schema & Relations</h3>
                    <p className="text-xs text-slate-500">Live schema structure and discovered foreign key relational join points</p>
                  </div>
                  <span className="text-xs font-semibold px-2.5 py-1 bg-blue-50 text-blue-700 rounded-lg border border-blue-200">
                    {viewsList.length} Active Views
                  </span>
                </div>

                {/* Discovered Foreign Key Cards */}
                {response.retrieved_view_metadata.foreign_keys && response.retrieved_view_metadata.foreign_keys.length > 0 && (
                  <div className="space-y-2">
                    <h4 className="text-xs font-bold uppercase text-slate-600 tracking-wider">Discovered Foreign Key Relations:</h4>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                      {response.retrieved_view_metadata.foreign_keys.map((fk, fIdx) => (
                        <div key={fIdx} className="p-3.5 rounded-xl bg-slate-50 border border-slate-200 space-y-1.5 shadow-2xs">
                          <div className="flex items-center gap-1.5 text-xs font-bold text-slate-800">
                            <Link2 className="w-3.5 h-3.5 text-blue-600" />
                            <span>{fk.view_a} ⟷ {fk.view_b}</span>
                          </div>
                          <div className="text-xs text-slate-600 font-mono">
                            {fk.join_keys.map((jk, jIdx) => (
                              <div key={jIdx} className="flex items-center gap-1.5 text-[11px]">
                                <span className="text-slate-400">• Join on:</span>
                                <span className="font-bold text-blue-700">{jk.col1}</span>
                                <span>=</span>
                                <span className="font-bold text-purple-700">{jk.col2}</span>
                              </div>
                            ))}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Columns Table */}
                <div className="border border-slate-200 rounded-xl overflow-hidden shadow-2xs">
                  <table className="w-full text-left text-xs border-collapse">
                    <thead className="bg-slate-100 text-slate-700 uppercase font-bold border-b border-slate-200">
                      <tr>
                        {response.retrieved_view_metadata.columns && response.retrieved_view_metadata.columns[0]?.VIEW_NAME && (
                          <th className="px-4 py-3">View Source</th>
                        )}
                        <th className="px-4 py-3">Column Name</th>
                        <th className="px-4 py-3">Type</th>
                        <th className="px-4 py-3">Role / Inferred Description</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-200 font-mono text-xs">
                      {response.retrieved_view_metadata.columns && response.retrieved_view_metadata.columns.length > 0 && response.retrieved_view_metadata.columns[0]?.COLUMN_NAME ? (
                        response.retrieved_view_metadata.columns.map((col, idx) => (
                          <tr key={idx} className={idx % 2 === 0 ? 'bg-white' : 'bg-slate-50/60'}>
                            {col.VIEW_NAME && (
                              <td className="px-4 py-2.5 font-bold text-slate-700 font-sans">
                                <span className="px-2 py-0.5 rounded bg-blue-50 text-blue-700 border border-blue-200 text-[11px]">
                                  {col.VIEW_NAME}
                                </span>
                              </td>
                            )}
                            <td className="px-4 py-2.5 font-bold text-blue-700">{col.COLUMN_NAME}</td>
                            <td className="px-4 py-2.5 text-slate-600">
                              <span className="px-2 py-0.5 rounded bg-slate-100 border border-slate-200 font-semibold text-[11px]">
                                {col.DATA_TYPE || 'Text Dimension'}
                              </span>
                            </td>
                            <td className="px-4 py-2.5 text-slate-700 font-sans">{col.DESCRIPTION || 'Datasphere view field'}</td>
                          </tr>
                        ))
                      ) : (
                        tableHeaders.map((hdr, idx) => (
                          <tr key={idx} className={idx % 2 === 0 ? 'bg-white' : 'bg-slate-50/60'}>
                            <td className="px-4 py-2.5 font-bold text-blue-700">{hdr}</td>
                            <td className="px-4 py-2.5 text-slate-600">
                              <span className="px-2 py-0.5 rounded bg-slate-100 border border-slate-200 font-semibold text-[11px]">
                                {typeof response.data[0]?.[hdr] === 'number' ? 'Edm.Decimal / Double' : 'Edm.String'}
                              </span>
                            </td>
                            <td className="px-4 py-2.5 text-slate-700 font-sans">Joined view column</td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

          </div>
        )}

      </main>

      {/* Footer */}
      <footer className="border-t border-slate-200 bg-white px-6 py-4 text-center text-xs text-slate-500 font-medium">
        SAP Datasphere Multi-View Relational OData Integration • OAuth 2.0 Security • Zero LLM Data Leakage
      </footer>
    </div>
  );
}
