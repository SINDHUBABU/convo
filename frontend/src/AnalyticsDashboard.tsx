import React, { useState, useEffect, useMemo, useRef, useCallback } from 'react';
import {
  Send,
  ShieldCheck,
  BarChart3,
  Table as TableIcon,
  Sparkles,
  Lock,
  RefreshCw,
  Download,
  CheckCircle2,
  Layers,
  ChevronDown,
  ChevronUp,
  Search,
  Copy,
  Trash2,
  ChevronLeft,
  ChevronRight,
  Plus,
  X,
  Link2,
  SlidersHorizontal,
  Check,
  Tag,
  MessageSquare,
  Bot,
  User,
  PanelLeftClose,
  PanelLeft,
  ArrowUp,
  Sliders,
  Compass,
  CheckCheck,
  Database,
  Terminal,
  Cpu,
  Zap,
  Activity,
  ArrowRight,
  Filter,
  Eye,
  FileCode2,
  LayoutDashboard,
  Pencil,
  History,
  Clock,
  MessageSquarePlus
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

// ==============================================================================
// 1. DATA CONTRACTS & TYPE DEFINITIONS
// ==============================================================================

/**
 * Represents a detected relational foreign key / common join key
 * between two SAP Datasphere analytical views.
 */
interface ForeignKeyRelation {
  view_a: string;
  view_b: string;
  join_keys: Array<{
    col1: string;
    col2: string;
    key_name: string;
  }>;
}

/**
 * Complete response payload structure returned by the /api/analytics backend endpoint.
 * Contains query blueprints, column schemas, OData execution logs, KPI cards, and rows.
 */
interface AnalyticsResponse {
  session_id: string;
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
    views_auto_discovered?: boolean;
    conversational_follow_up?: boolean;
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
  views_data?: Record<string, Record<string, any>[]>;
}

/**
 * Individual analytical execution message entry stored in the active session history.
 */
interface ChatMessage {
  id: string;
  sender: 'user' | 'agent';
  prompt: string;
  timestamp: string;
  response?: AnalyticsResponse;
  error?: string;
}

/**
 * Persisted session history item representing a saved analytical workflow.
 */
interface SessionHistoryItem {
  id: string;
  title: string;
  timestamp: string;
  space_id?: string;
  views?: string[];
  created_at?: number;
  updated_at?: number;
  messages: ChatMessage[];
}

const LOCAL_STORAGE_SESSIONS_KEY = 'datasphere_chat_sessions_v2';

function getSessionTimeBucket(item: SessionHistoryItem): string {
  const ts = item.updated_at ? item.updated_at * 1000 : (item.created_at ? item.created_at * 1000 : null);
  const date = ts ? new Date(ts) : new Date(item.timestamp);
  if (isNaN(date.getTime())) return 'Previous Sessions';
  
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const startOfYesterday = startOfToday - 24 * 60 * 60 * 1000;
  const startOf7DaysAgo = startOfToday - 7 * 24 * 60 * 60 * 1000;
  const startOf30DaysAgo = startOfToday - 30 * 24 * 60 * 60 * 1000;

  const itemTime = date.getTime();
  if (itemTime >= startOfToday) {
    return 'Today';
  } else if (itemTime >= startOfYesterday) {
    return 'Yesterday';
  } else if (itemTime >= startOf7DaysAgo) {
    return 'Previous 7 Days';
  } else if (itemTime >= startOf30DaysAgo) {
    return 'Previous 30 Days';
  } else {
    return 'Older';
  }
}

// ==============================================================================
// 2. MAIN COMPONENT: AnalyticsDashboard
// ==============================================================================

export default function AnalyticsDashboard() {
  // ---------------------------------------------------------------------------
  // Core UI & Environment States
  // ---------------------------------------------------------------------------
  const [prompt, setPrompt] = useState<string>("");
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(true);
  const [spaceId, setSpaceId] = useState<string>("RADH_S3P");
  const [viewsList, setViewsList] = useState<string[]>([]);
  const [newViewInput, setNewViewInput] = useState<string>("");
  const [sessionId, setSessionId] = useState<string>(() => `session_${Date.now()}`);
  
  // ---------------------------------------------------------------------------
  // Field / Column Selection & Schema Probing States
  // ---------------------------------------------------------------------------
  const [availableSchemaByView, setAvailableSchemaByView] = useState<Record<string, string[]>>({});
  const [selectedFields, setSelectedFields] = useState<string[]>([]);
  const [showFieldSelectorModal, setShowFieldSelectorModal] = useState<boolean>(false);
  const [fieldSearch, setFieldSearch] = useState<string>("");
  const [fetchingSchema, setFetchingSchema] = useState<boolean>(false);

  // ---------------------------------------------------------------------------
  // Execution History & Loading States (ChatGPT & Gemini Persistent History)
  // ---------------------------------------------------------------------------
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [sessionsHistory, setSessionsHistory] = useState<SessionHistoryItem[]>(() => {
    try {
      const stored = localStorage.getItem(LOCAL_STORAGE_SESSIONS_KEY);
      return stored ? JSON.parse(stored) : [];
    } catch {
      return [];
    }
  });
  const [sessionSearch, setSessionSearch] = useState<string>("");
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(false);
  const [copiedNotification, setCopiedNotification] = useState<string | null>(null);

  // ---------------------------------------------------------------------------
  // In-Card View States (Tab switching, search filters, pagination per message card)
  // ---------------------------------------------------------------------------
  const [activeTabByMsg, setActiveTabByMsg] = useState<Record<string, 'table' | 'chart' | 'sql'>>({});
  const [chartTypeByMsg, setChartTypeByMsg] = useState<Record<string, 'bar' | 'line' | 'area'>>({});
  const [tableSearchByMsg, setTableSearchByMsg] = useState<Record<string, string>>({});
  const [pageByMsg, setPageByMsg] = useState<Record<string, number>>({});
  const [pageSizeByMsg, setPageSizeByMsg] = useState<Record<string, number>>({});
  const [selectedViewByMsg, setSelectedViewByMsg] = useState<Record<string, string>>({});

  const chatEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // ---------------------------------------------------------------------------
  // Step 1: Initialize server configuration and load persisted chat sessions from server
  // ---------------------------------------------------------------------------
  useEffect(() => {
    fetch('/api/info')
      .then((res) => res.json())
      .then((data) => {
        if (data.space_id) setSpaceId(data.space_id);
      })
      .catch((err) => console.warn('Could not fetch server info:', err));

    // Fetch saved sessions from backend DB
    fetch('/api/sessions')
      .then(res => res.json())
      .then(data => {
        if (data.sessions && Array.isArray(data.sessions) && data.sessions.length > 0) {
          setSessionsHistory(prev => {
            // Merge server sessions with local storage sessions
            const serverMap = new Map<string, SessionHistoryItem>(data.sessions.map((s: SessionHistoryItem) => [s.id, s]));
            prev.forEach(item => {
              if (!serverMap.has(item.id)) {
                serverMap.set(item.id, item);
              }
            });
            const merged = Array.from(serverMap.values()).sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
            try {
              localStorage.setItem(LOCAL_STORAGE_SESSIONS_KEY, JSON.stringify(merged));
            } catch {}
            return merged;
          });
        }
      })
      .catch(err => console.warn('Could not sync sessions from backend:', err));
  }, []);

  // ---------------------------------------------------------------------------
  // Step 2: Probes live column schemas for all selected views from Datasphere
  // ---------------------------------------------------------------------------
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

  // Re-fetch schemas whenever viewsList or spaceId changes
  useEffect(() => {
    if (viewsList.length > 0) {
      fetchSchemasForViews(viewsList);
    }
  }, [viewsList, spaceId]);

  // Open field selector modal and trigger probe if missing
  const openFieldSelectorModal = () => {
    setShowFieldSelectorModal(true);
    if (viewsList.length > 0) {
      const hasMissing = viewsList.some(v => !availableSchemaByView[v] || availableSchemaByView[v].length === 0);
      if (hasMissing || Object.keys(availableSchemaByView).length === 0) {
        fetchSchemasForViews(viewsList);
      }
    }
  };

  // ---------------------------------------------------------------------------
  // View Addition & Removal Handlers
  // ---------------------------------------------------------------------------
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

  // Case-insensitive column retrieval helper
  const getColsForView = useCallback((vName: string): string[] => {
    if (!vName) return [];
    if (availableSchemaByView[vName] && availableSchemaByView[vName].length > 0) {
      return availableSchemaByView[vName];
    }
    const lower = vName.toLowerCase().trim();
    for (const [k, v] of Object.entries(availableSchemaByView)) {
      if (k.toLowerCase().trim() === lower && v.length > 0) {
        return v;
      }
    }
    return [];
  }, [availableSchemaByView]);

  // ---------------------------------------------------------------------------
  // Dynamic Schema Computation (Common linking keys vs View-specific columns)
  // ---------------------------------------------------------------------------
  const commonFields = useMemo(() => {
    if (viewsList.length <= 1) return [];
    const firstCols = getColsForView(viewsList[0]);
    if (firstCols.length === 0) return [];
    return firstCols.filter(col => {
      const colLower = col.toLowerCase().trim();
      return viewsList.slice(1).every(otherView => {
        const otherCols = getColsForView(otherView);
        return otherCols.some(oc => oc.toLowerCase().trim() === colLower);
      });
    });
  }, [viewsList, getColsForView]);

  const viewSpecificFields = useMemo(() => {
    const commonSet = new Set(commonFields.map(c => c.toLowerCase().trim()));
    const map: Record<string, string[]> = {};
    viewsList.forEach(v => {
      const cols = getColsForView(v);
      map[v] = cols.filter(c => !commonSet.has(c.toLowerCase().trim()));
    });
    return map;
  }, [viewsList, commonFields, getColsForView]);

  const allAvailableColumns = useMemo(() => {
    const set = new Set<string>();
    Object.values(availableSchemaByView).forEach(cols => {
      cols.forEach(c => set.add(c));
    });
    return Array.from(set);
  }, [availableSchemaByView]);

  const groupedSessions = useMemo(() => {
    const query = sessionSearch.trim().toLowerCase();
    const filtered = sessionsHistory.filter(s => {
      if (!query) return true;
      const titleMatch = (s.title || '').toLowerCase().includes(query);
      const msgMatch = s.messages?.some(m => m.prompt?.toLowerCase().includes(query));
      return titleMatch || msgMatch;
    });

    const groups: Record<string, SessionHistoryItem[]> = {
      'Today': [],
      'Yesterday': [],
      'Previous 7 Days': [],
      'Previous 30 Days': [],
      'Older': []
    };

    filtered.forEach(s => {
      const bucket = getSessionTimeBucket(s);
      if (!groups[bucket]) groups[bucket] = [];
      groups[bucket].push(s);
    });

    return groups;
  }, [sessionsHistory, sessionSearch]);

  const toggleField = (field: string) => {
    setSelectedFields(prev => {
      if (prev.includes(field)) {
        return prev.filter(f => f !== field);
      } else {
        return [...prev, field];
      }
    });
  };

  const selectGroupFields = (fieldsToAdd: string[]) => {
    setSelectedFields(prev => {
      const set = new Set([...prev, ...fieldsToAdd]);
      return Array.from(set);
    });
  };

  const selectAllFields = () => {
    setSelectedFields(allAvailableColumns);
  };

  const clearAllFields = () => {
    setSelectedFields([]);
  };

  // ---------------------------------------------------------------------------
  // Step 3: Query Execution Handler (Sends Prompt & Selected Fields to Backend)
  // ---------------------------------------------------------------------------
  const handleQuerySubmit = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    const queryText = prompt.trim();
    if (!queryText || loading) return;

    const userMessageId = `user_${Date.now()}`;
    const agentMessageId = `agent_${Date.now()}`;
    const timestamp = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

    const newUserMsg: ChatMessage = {
      id: userMessageId,
      sender: 'user',
      prompt: queryText,
      timestamp
    };

    setChatMessages(prev => [...prev, newUserMsg]);
    setPrompt("");
    setLoading(true);

    try {
      const response = await fetch('/api/analytics', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_prompt: queryText,
          space_id: spaceId,
          view_names: viewsList.length > 0 ? viewsList : undefined,
          session_id: sessionId,
          selected_fields: selectedFields.length > 0 ? selectedFields : undefined
        })
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      const data: AnalyticsResponse = await response.json();

      const newAgentMsg: ChatMessage = {
        id: agentMessageId,
        sender: 'agent',
        prompt: queryText,
        timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
        response: data
      };

      setChatMessages(prev => {
        const updated = [...prev, newAgentMsg];
        setSessionsHistory(prevHist => {
          const existingIdx = prevHist.findIndex(h => h.id === sessionId);
          const existingTitle = existingIdx >= 0 ? prevHist[existingIdx].title : null;
          
          // Generate clean natural title from user prompt if new
          const generatedTitle = existingTitle || queryText
            .replace(/^(?:get\s+me\s+(?:the\s+)?data\s+where|select|show|give\s+me|display)\s+/i, '')
            .trim()
            .slice(0, 42) + (queryText.length > 42 ? '...' : '');

          const historyItem: SessionHistoryItem = {
            id: sessionId,
            title: generatedTitle || `Analytics Session`,
            timestamp: new Date().toLocaleDateString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }),
            space_id: spaceId,
            views: viewsList,
            created_at: existingIdx >= 0 ? prevHist[existingIdx].created_at || (Date.now() / 1000) : (Date.now() / 1000),
            updated_at: Date.now() / 1000,
            messages: updated
          };

          let next: SessionHistoryItem[];
          if (existingIdx >= 0) {
            next = [...prevHist];
            next[existingIdx] = historyItem;
            // Move updated session to top
            const item = next.splice(existingIdx, 1)[0];
            next.unshift(item);
          } else {
            next = [historyItem, ...prevHist];
          }

          // Persist to localStorage
          try {
            localStorage.setItem(LOCAL_STORAGE_SESSIONS_KEY, JSON.stringify(next));
          } catch {}

          // Persist to Backend SQLite DB
          fetch('/api/sessions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              session_id: historyItem.id,
              title: historyItem.title,
              space_id: historyItem.space_id,
              views: historyItem.views,
              messages: historyItem.messages
            })
          }).catch(err => console.warn('Could not save session to backend:', err));

          return next;
        });
        return updated;
      });

    } catch (err: any) {
      const errorAgentMsg: ChatMessage = {
        id: agentMessageId,
        sender: 'agent',
        prompt: queryText,
        timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
        error: err.message || 'Failed to process analytical query'
      };
      setChatMessages(prev => [...prev, errorAgentMsg]);
    } finally {
      setLoading(false);
      setTimeout(() => {
        chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
      }, 100);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleQuerySubmit();
    }
  };

  const handleStartNewChat = () => {
    setChatMessages([]);
    setSessionId(`session_${Date.now()}`);
    setPrompt("");
    setEditingSessionId(null);
  };

  const handleRestoreSession = (session: SessionHistoryItem) => {
    setSessionId(session.id);
    setChatMessages(session.messages || []);
    if (session.views && session.views.length > 0) {
      setViewsList(session.views);
    }
    if (session.space_id) {
      setSpaceId(session.space_id);
    }
    setEditingSessionId(null);
  };

  const handleStartRename = (e: React.MouseEvent, session: SessionHistoryItem) => {
    e.stopPropagation();
    setEditingSessionId(session.id);
    setEditingTitle(session.title);
  };

  const handleSaveRename = (e: React.FormEvent | React.MouseEvent, targetSessionId: string) => {
    e.stopPropagation();
    if (e) (e as any).preventDefault?.();
    const cleanTitle = editingTitle.trim() || 'Untitled Session';
    
    setSessionsHistory(prev => {
      const next = prev.map(s => s.id === targetSessionId ? { ...s, title: cleanTitle, updated_at: Date.now() / 1000 } : s);
      try {
        localStorage.setItem(LOCAL_STORAGE_SESSIONS_KEY, JSON.stringify(next));
      } catch {}
      return next;
    });

    fetch(`/api/sessions/${targetSessionId}/rename`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: cleanTitle })
    }).catch(err => console.warn('Could not rename on backend:', err));

    setEditingSessionId(null);
  };

  const handleDeleteSession = (e: React.MouseEvent, targetSessionId: string) => {
    e.stopPropagation();
    setSessionsHistory(prev => {
      const next = prev.filter(s => s.id !== targetSessionId);
      try {
        localStorage.setItem(LOCAL_STORAGE_SESSIONS_KEY, JSON.stringify(next));
      } catch {}
      return next;
    });

    fetch(`/api/sessions/${targetSessionId}`, {
      method: 'DELETE'
    }).catch(err => console.warn('Could not delete session on backend:', err));

    if (sessionId === targetSessionId) {
      handleStartNewChat();
    }
  };

  const handleClearAllSessions = () => {
    if (window.confirm("Are you sure you want to clear all chat sessions?")) {
      setSessionsHistory([]);
      try {
        localStorage.removeItem(LOCAL_STORAGE_SESSIONS_KEY);
      } catch {}
      fetch('/api/sessions', { method: 'DELETE' }).catch(err => console.warn('Could not clear backend sessions:', err));
      handleStartNewChat();
    }
  };

  const copyToClipboard = (text: any) => {
    const stringified = typeof text === 'string' ? text : JSON.stringify(text, null, 2);
    navigator.clipboard.writeText(stringified);
    setCopiedNotification("Copied to clipboard");
    setTimeout(() => setCopiedNotification(null), 2500);
  };

  const downloadCSV = (dataRows: Record<string, any>[], viewNameLabel?: string) => {
    if (!dataRows || dataRows.length === 0) return;
    const headers = Object.keys(dataRows[0]);
    const csvContent = [
      headers.join(','),
      ...dataRows.map(row => headers.map(h => `"${String(row[h] ?? '').replace(/"/g, '""')}"`).join(','))
    ].join('\n');

    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.setAttribute('href', url);
    const filename = `${viewNameLabel || viewsList.join('_') || 'datasphere_export'}_${Date.now()}.csv`;
    link.setAttribute('download', filename);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  return (
    <div className="flex h-screen w-screen bg-[#0b1120] text-slate-100 overflow-hidden font-sans">
      
      {/* GLOBAL NOTIFICATION POPUP */}
      {copiedNotification && (
        <div className="fixed top-5 right-5 z-50 bg-slate-900/90 text-white px-4 py-2 rounded-xl text-xs font-semibold shadow-2xl border border-blue-500/40 flex items-center gap-2 animate-in fade-in slide-in-from-top-3 backdrop-blur-md">
          <CheckCircle2 className="w-4 h-4 text-emerald-400" />
          <span>{copiedNotification}</span>
        </div>
      )}

      {/* LEFT NAVIGATION WORKBENCH DRAWER */}
      <aside className={`transition-all duration-300 ease-in-out border-r border-slate-800/80 bg-[#0f172a] flex flex-col z-30 shrink-0 ${
        sidebarOpen ? 'w-80' : 'w-0 -translate-x-full overflow-hidden'
      }`}>
        
        {/* Workspace Brand Header */}
        <div className="p-4 border-b border-slate-800/80 flex items-center justify-between bg-slate-950/60">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-tr from-blue-600 to-indigo-600 flex items-center justify-center text-white shadow-md shadow-blue-500/20">
              <Cpu className="w-4 h-4" />
            </div>
            <div>
              <h1 className="font-bold text-xs tracking-wider uppercase text-slate-200 flex items-center gap-1.5">
                <span>Datasphere Studio</span>
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
              </h1>
              <span className="text-[10px] text-slate-400 font-mono">RADH_S3P Analytics</span>
            </div>
          </div>
          <button
            type="button"
            onClick={() => setSidebarOpen(false)}
            className="text-slate-400 hover:text-white p-1.5 rounded-lg hover:bg-slate-800/60 transition-colors cursor-pointer"
            title="Collapse sidebar"
          >
            <PanelLeftClose className="w-4 h-4" />
          </button>
        </div>

        {/* Space Configuration Card */}
        <div className="p-3.5 border-b border-slate-800/60 bg-slate-900/40">
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1">
              <Database className="w-3 h-3 text-blue-400" />
              <span>Datasphere Space</span>
            </span>
            <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
              Live Connected
            </span>
          </div>
          <div className="relative">
            <input
              type="text"
              value={spaceId}
              onChange={(e) => setSpaceId(e.target.value.toUpperCase())}
              placeholder="e.g. RADH_S3P"
              className="w-full bg-slate-950/80 border border-slate-700/80 rounded-lg px-3 py-1.5 text-xs text-slate-100 font-mono font-semibold focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
            />
          </div>
        </div>

        {/* Views & Columns Studio */}
        <div className="p-3.5 border-b border-slate-800/60 space-y-3 bg-slate-900/20">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1">
              <Layers className="w-3 h-3 text-indigo-400" />
              <span>Datasphere Views</span>
            </span>
            <span className="text-[10px] text-slate-400 font-mono">
              {viewsList.length} Active
            </span>
          </div>

          <div className="flex gap-1.5">
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
              placeholder="Add View (e.g. ZSL_FA...)"
              className="flex-1 bg-slate-950/80 border border-slate-700/80 rounded-lg px-2.5 py-1.5 text-xs text-slate-100 font-mono focus:outline-none focus:border-blue-500"
            />
            <button
              type="button"
              onClick={() => addView()}
              className="px-2.5 py-1.5 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-xs font-semibold cursor-pointer transition-colors shadow-sm"
              title="Add view"
            >
              <Plus className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* Active View Chips */}
          <div className="space-y-1.5 max-h-36 overflow-y-auto pr-1">
            {viewsList.length === 0 ? (
              <div className="p-2.5 rounded-lg border border-dashed border-slate-800 text-center text-slate-500 text-[11px]">
                No views selected. Auto-discovery active.
              </div>
            ) : (
              viewsList.map((v) => {
                const cols = availableSchemaByView[v] || [];
                return (
                  <div
                    key={v}
                    className="p-2 rounded-lg bg-slate-950/60 border border-slate-800 flex items-center justify-between group hover:border-slate-700 transition-colors"
                  >
                    <div className="flex items-center gap-2 truncate">
                      <TableIcon className="w-3.5 h-3.5 text-blue-400 shrink-0" />
                      <div className="truncate">
                        <div className="text-xs font-mono font-semibold text-slate-200 truncate">{v}</div>
                        <div className="text-[10px] text-slate-400 font-mono">{cols.length > 0 ? `${cols.length} cols` : 'Probing schema...'}</div>
                      </div>
                    </div>
                    <button
                      type="button"
                      onClick={() => removeView(v)}
                      className="text-slate-500 hover:text-red-400 p-1 rounded transition-colors cursor-pointer"
                    >
                      <X className="w-3 h-3" />
                    </button>
                  </div>
                );
              })
            )}
          </div>

          {/* Field Selection Launcher Button */}
          {viewsList.length > 0 && (
            <button
              type="button"
              onClick={openFieldSelectorModal}
              className="w-full py-2 px-3 bg-gradient-to-r from-slate-800 to-slate-800/80 hover:from-slate-700 hover:to-slate-700/80 border border-slate-700 rounded-xl text-xs font-semibold text-slate-200 flex items-center justify-between cursor-pointer transition-all shadow-sm"
            >
              <div className="flex items-center gap-2">
                <SlidersHorizontal className="w-3.5 h-3.5 text-blue-400" />
                <span>Field Selection</span>
              </div>
              <span className="px-2 py-0.5 rounded-full bg-blue-500/20 text-blue-300 font-mono text-[10px] font-bold border border-blue-500/30">
                {selectedFields.length > 0 ? `${selectedFields.length} Fields` : 'All Fields'}
              </span>
            </button>
          )}
        </div>

        {/* ChatGPT & Gemini Style Query Sessions History Drawer */}
        <div className="flex-1 overflow-y-auto p-3.5 space-y-3 flex flex-col min-h-0">
          
          {/* New Chat Primary Action Button */}
          <button
            type="button"
            onClick={handleStartNewChat}
            className="w-full py-2.5 px-3 bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-500 hover:to-indigo-500 text-white rounded-xl text-xs font-semibold flex items-center justify-between cursor-pointer transition-all shadow-md shadow-blue-600/20 group"
          >
            <div className="flex items-center gap-2">
              <MessageSquarePlus className="w-4 h-4 text-blue-100 group-hover:scale-110 transition-transform" />
              <span>New Analysis Chat</span>
            </div>
            <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-white/20 text-white font-bold">
              +
            </span>
          </button>

          {/* Session Search Bar */}
          {sessionsHistory.length > 0 && (
            <div className="relative">
              <Search className="w-3.5 h-3.5 text-slate-500 absolute left-2.5 top-2.5" />
              <input
                type="text"
                value={sessionSearch}
                onChange={(e) => setSessionSearch(e.target.value)}
                placeholder="Search past sessions..."
                className="w-full bg-slate-950/70 border border-slate-800 rounded-lg pl-8 pr-7 py-1.5 text-xs text-slate-200 font-mono placeholder:text-slate-500 focus:outline-none focus:border-blue-500"
              />
              {sessionSearch && (
                <button
                  type="button"
                  onClick={() => setSessionSearch("")}
                  className="absolute right-2 top-2 text-slate-500 hover:text-slate-300 cursor-pointer"
                >
                  <X className="w-3.5 h-3.5" />
                </button>
              )}
            </div>
          )}

          {/* Chronologically Grouped Sessions List */}
          <div className="flex-1 overflow-y-auto space-y-3 pr-0.5 min-h-0">
            {sessionsHistory.length === 0 ? (
              <div className="p-4 rounded-xl border border-dashed border-slate-800/80 text-center space-y-1 bg-slate-950/20">
                <History className="w-5 h-5 text-slate-600 mx-auto mb-1" />
                <div className="text-slate-400 font-semibold text-xs">No chat history yet</div>
                <div className="text-slate-500 text-[10px]">Your analytical sessions and queries will be saved here automatically.</div>
              </div>
            ) : Object.values(groupedSessions).every(arr => arr.length === 0) ? (
              <div className="text-slate-500 text-xs px-2 py-4 italic text-center">
                No sessions matching "{sessionSearch}"
              </div>
            ) : (
              Object.entries(groupedSessions).map(([bucketLabel, items]) => {
                if (items.length === 0) return null;
                return (
                  <div key={bucketLabel} className="space-y-1">
                    <div className="px-1.5 py-0.5 text-[10px] font-bold text-slate-500 uppercase tracking-wider flex items-center gap-1.5">
                      <Clock className="w-3 h-3 text-slate-500" />
                      <span>{bucketLabel}</span>
                    </div>

                    <div className="space-y-1">
                      {items.map((s) => {
                        const isActive = s.id === sessionId;
                        const isEditing = editingSessionId === s.id;

                        return (
                          <div
                            key={s.id}
                            onClick={() => !isEditing && handleRestoreSession(s)}
                            className={`group relative w-full p-2.5 rounded-xl text-xs transition-all cursor-pointer border flex flex-col justify-between gap-1 ${
                              isActive
                                ? 'bg-blue-600/15 border-blue-500/50 text-blue-100 shadow-sm shadow-blue-500/10'
                                : 'bg-slate-950/40 border-slate-800/70 text-slate-300 hover:bg-slate-800/60 hover:text-white hover:border-slate-700'
                            }`}
                          >
                            {/* Title / Inline Rename Editor */}
                            <div className="flex items-center justify-between gap-2">
                              <div className="flex items-center gap-2 truncate flex-1 min-w-0">
                                <MessageSquare className={`w-3.5 h-3.5 shrink-0 ${isActive ? 'text-blue-400' : 'text-slate-500 group-hover:text-slate-300'}`} />
                                
                                {isEditing ? (
                                  <form
                                    onSubmit={(e) => handleSaveRename(e, s.id)}
                                    onClick={(e) => e.stopPropagation()}
                                    className="flex items-center gap-1 flex-1"
                                  >
                                    <input
                                      type="text"
                                      value={editingTitle}
                                      onChange={(e) => setEditingTitle(e.target.value)}
                                      autoFocus
                                      className="w-full bg-slate-900 border border-blue-500 rounded px-1.5 py-0.5 text-xs text-white focus:outline-none"
                                      onKeyDown={(e) => {
                                        if (e.key === 'Escape') setEditingSessionId(null);
                                      }}
                                    />
                                    <button
                                      type="button"
                                      onClick={(e) => handleSaveRename(e, s.id)}
                                      className="p-1 rounded bg-blue-600 hover:bg-blue-500 text-white cursor-pointer"
                                      title="Save"
                                    >
                                      <Check className="w-3 h-3" />
                                    </button>
                                    <button
                                      type="button"
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        setEditingSessionId(null);
                                      }}
                                      className="p-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 cursor-pointer"
                                      title="Cancel"
                                    >
                                      <X className="w-3 h-3" />
                                    </button>
                                  </form>
                                ) : (
                                  <span className={`font-semibold truncate text-xs ${isActive ? 'text-blue-200' : 'text-slate-200'}`} title={s.title}>
                                    {s.title}
                                  </span>
                                )}
                              </div>

                              {/* Hover Action Icons: Rename & Delete */}
                              {!isEditing && (
                                <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity shrink-0">
                                  <button
                                    type="button"
                                    onClick={(e) => handleStartRename(e, s)}
                                    className="p-1 rounded text-slate-400 hover:text-blue-400 hover:bg-slate-800/80 transition-colors cursor-pointer"
                                    title="Rename chat"
                                  >
                                    <Pencil className="w-3 h-3" />
                                  </button>
                                  <button
                                    type="button"
                                    onClick={(e) => handleDeleteSession(e, s.id)}
                                    className="p-1 rounded text-slate-400 hover:text-red-400 hover:bg-slate-800/80 transition-colors cursor-pointer"
                                    title="Delete chat"
                                  >
                                    <Trash2 className="w-3 h-3" />
                                  </button>
                                </div>
                              )}
                            </div>

                            {/* Session Meta Subtitle */}
                            {!isEditing && (
                              <div className="flex items-center justify-between text-[10px] text-slate-500 font-mono pt-0.5">
                                <span>{s.messages?.length || 0} step{(s.messages?.length || 0) !== 1 ? 's' : ''}</span>
                                <span>{s.timestamp}</span>
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  </div>
                );
              })
            )}
          </div>

          {/* Clear All Sessions Option */}
          {sessionsHistory.length > 0 && (
            <div className="pt-2 border-t border-slate-800/60 flex items-center justify-between">
              <button
                type="button"
                onClick={handleClearAllSessions}
                className="text-[11px] text-slate-500 hover:text-red-400 flex items-center gap-1.5 transition-colors cursor-pointer px-1 py-0.5 rounded hover:bg-slate-800/40"
              >
                <Trash2 className="w-3 h-3" />
                <span>Clear all chats</span>
              </button>
              <span className="text-[10px] text-slate-600 font-mono">
                {sessionsHistory.length} saved
              </span>
            </div>
          )}

        </div>

        {/* Security / Compliance Badge Footer */}
        <div className="p-3 border-t border-slate-800/80 bg-slate-950/80 space-y-1">
          <div className="flex items-center gap-2 text-[11px] font-medium text-emerald-400">
            <ShieldCheck className="w-3.5 h-3.5 shrink-0" />
            <span>Zero LLM Data Leakage Active</span>
          </div>
          <div className="flex items-center gap-2 text-[10px] text-slate-400 font-mono">
            <Lock className="w-3 h-3 shrink-0 text-blue-400" />
            <span>OAuth 2.0 • Row-Level DAC Enforced</span>
          </div>
        </div>

      </aside>

      {/* MAIN WORKBENCH CANVAS */}
      <main className="flex-1 flex flex-col h-full bg-[#f8fafc] text-slate-900 overflow-hidden relative">
        
        {/* TOP ENTERPRISE HEADER BAR */}
        <header className="h-14 border-b border-slate-200/90 bg-white flex items-center justify-between px-5 sticky top-0 z-20 shadow-xs">
          <div className="flex items-center gap-3">
            {!sidebarOpen && (
              <button
                type="button"
                onClick={() => setSidebarOpen(true)}
                className="p-1.5 rounded-lg text-slate-600 hover:text-slate-900 hover:bg-slate-100 transition-colors cursor-pointer"
                title="Open Studio Panel"
              >
                <PanelLeft className="w-4 h-4" />
              </button>
            )}

            <div className="flex items-center gap-2.5">
              <div className="w-7 h-7 rounded-lg bg-blue-600 text-white flex items-center justify-center font-bold text-xs shadow-sm">
                SAP
              </div>
              <div>
                <span className="font-bold text-sm text-slate-900 tracking-tight">Datasphere Analytics Intelligence Studio</span>
                <span className="hidden sm:inline-block ml-2 text-xs font-mono px-2 py-0.5 rounded-full bg-slate-100 text-slate-600 border border-slate-200">
                  Space: {spaceId}
                </span>
              </div>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {chatMessages.length > 0 && (
              <button
                type="button"
                onClick={handleStartNewChat}
                className="px-3 py-1.5 text-xs font-semibold text-slate-600 hover:text-slate-900 hover:bg-slate-100 rounded-lg transition-colors flex items-center gap-1.5 cursor-pointer border border-slate-200"
              >
                <RefreshCw className="w-3.5 h-3.5" />
                <span>New Session</span>
              </button>
            )}
          </div>
        </header>

        {/* WORKBENCH RESULTS STREAM */}
        <div className="flex-1 overflow-y-auto px-4 md:px-8 py-6 space-y-6 studio-grid">
          
          {/* Analytical Execution Stream Cards */}
          <div className="max-w-5xl mx-auto space-y-6">
            {chatMessages.length === 0 ? (
              <div className="min-h-[35vh] flex flex-col items-center justify-center text-center p-8 bg-white/70 rounded-2xl border border-slate-200 space-y-4">
                <div className="w-12 h-12 rounded-2xl bg-gradient-to-tr from-blue-600 to-indigo-600 text-white flex items-center justify-center shadow-lg shadow-blue-500/20">
                  <LayoutDashboard className="w-6 h-6" />
                </div>
                <div className="space-y-1">
                  <h3 className="text-lg font-bold text-slate-800">Ready for Relational Analytics</h3>
                  <p className="text-xs text-slate-500 max-w-md">
                    Execute relational multi-view queries, filter metrics, and generate ANSI SQL on SAP Datasphere views in space <span className="font-mono font-bold text-slate-700">{spaceId}</span>.
                  </p>
                </div>
              </div>
            ) : (
              chatMessages.map((msg) => {
                const isUser = msg.sender === 'user';
                if (isUser) return null; // We render user prompt inside the Execution Card header!

                const currentTab = activeTabByMsg[msg.id] || 'table';
                const currentChartType = chartTypeByMsg[msg.id] || 'bar';
                const searchTxt = (tableSearchByMsg[msg.id] || '').toLowerCase();
                const page = pageByMsg[msg.id] || 1;
                const pageSize = pageSizeByMsg[msg.id] || 25;

                const viewsDataMap = msg.response?.views_data || {};
                const viewKeys = Object.keys(viewsDataMap);
                const hasMultipleViews = viewKeys.length > 1;
                const activeViewKey = selectedViewByMsg[msg.id] || '__combined__';

                const responseData = (activeViewKey === '__combined__' || !viewsDataMap[activeViewKey])
                  ? (msg.response?.data || [])
                  : (viewsDataMap[activeViewKey] || []);

                const filteredRows = responseData.filter(row => {
                  if (!searchTxt) return true;
                  return Object.values(row).some(v => v !== null && v !== undefined && String(v).toLowerCase().includes(searchTxt));
                });

                const totalPages = Math.ceil(filteredRows.length / (pageSize === -1 ? 1 : pageSize)) || 1;
                const paginatedRows = pageSize === -1 ? filteredRows : filteredRows.slice((page - 1) * pageSize, page * pageSize);
                const tableHeaders = responseData.length > 0 ? Object.keys(responseData[0]) : [];
                const numericalCols = responseData.length > 0 ? Object.keys(responseData[0]).filter(k => typeof responseData[0][k] === 'number') : [];
                const strCols = responseData.length > 0 ? Object.keys(responseData[0]).filter(k => typeof responseData[0][k] === 'string' && !k.toLowerCase().includes('id')) : [];
                const xAxisKey = strCols.length > 0 ? strCols[0] : (tableHeaders[0] || '');

                return (
                  <div key={msg.id} className="bg-white rounded-2xl border border-slate-200/90 shadow-sm overflow-hidden space-y-0">
                    
                    {/* EXECUTION CARD HEADER */}
                    <div className="px-5 py-3.5 bg-gradient-to-r from-slate-900 via-slate-900 to-indigo-950 text-white flex flex-wrap items-center justify-between gap-3">
                      <div className="flex items-center gap-3">
                        <div className="w-7 h-7 rounded-lg bg-blue-500/20 text-blue-400 border border-blue-500/30 flex items-center justify-center font-mono text-xs font-bold">
                          SQL
                        </div>
                        <div>
                          <div className="text-xs font-bold text-slate-100 flex items-center gap-2">
                            <span>"{msg.prompt}"</span>
                          </div>
                          <div className="text-[10px] text-slate-400 font-mono flex items-center gap-2 mt-0.5">
                            <span>Space: {msg.response?.retrieved_view_metadata.space_id || spaceId}</span>
                            <span>•</span>
                            <span>{msg.response?.data?.length || 0} records</span>
                            <span>•</span>
                            <span>{msg.timestamp}</span>
                          </div>
                        </div>
                      </div>

                      <div className="flex items-center gap-2">
                        {msg.response?.retrieved_view_metadata.views?.map(v => (
                          <span key={v} className="px-2 py-0.5 rounded bg-slate-800 text-blue-300 border border-slate-700 font-mono text-[10px] font-semibold">
                            {v}
                          </span>
                        ))}
                      </div>
                    </div>

                    {/* EXECUTIVE SUMMARY BANNER */}
                    <div className="p-4 bg-blue-50/50 border-b border-blue-100/80 flex items-start gap-3">
                      <Sparkles className="w-4 h-4 text-blue-600 mt-0.5 shrink-0" />
                      <div className="text-xs font-medium text-slate-800 leading-relaxed">
                        {msg.response?.kpi_summary?.direct_answer || msg.error || 'Query executed successfully.'}
                      </div>
                    </div>

                    <div className="p-5 space-y-4">
                      
                      {/* GENERATED SQL QUERY STUDIO (DEEP SYNTAX INSPECTOR) */}
                      {msg.response?.query_blueprint?.sql_query && (
                        <div className="rounded-xl border border-slate-800 bg-[#0a0f1d] overflow-hidden text-xs shadow-inner">
                          <div className="px-3.5 py-2 bg-slate-950 border-b border-slate-800 flex items-center justify-between">
                            <span className="font-bold text-[11px] text-emerald-400 flex items-center gap-1.5 font-mono">
                              <FileCode2 className="w-3.5 h-3.5 text-emerald-400" />
                              <span>Compiled Relational SQL Blueprint</span>
                            </span>
                            <button
                              type="button"
                              onClick={() => copyToClipboard(msg.response?.query_blueprint?.sql_query)}
                              className="text-[11px] text-slate-400 hover:text-white flex items-center gap-1 cursor-pointer transition-colors px-2 py-0.5 rounded hover:bg-slate-800"
                              title="Copy SQL"
                            >
                              <Copy className="w-3 h-3" />
                              <span>Copy SQL</span>
                            </button>
                          </div>
                          <div className="p-3.5 font-mono text-[11px] text-slate-200 bg-[#0a0f1d] whitespace-pre-wrap overflow-x-auto leading-relaxed selection:bg-blue-600">
                            {msg.response.query_blueprint.sql_query}
                          </div>
                        </div>
                      )}

                      {/* KPI SCORECARDS GRID */}
                      {msg.response?.kpi_summary?.cards && msg.response.kpi_summary.cards.length > 0 && (
                        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 pt-1">
                          {msg.response.kpi_summary.cards.map((c, cIdx) => (
                            <div key={cIdx} className="p-3.5 rounded-xl bg-gradient-to-br from-white to-slate-50 border border-slate-200/90 shadow-2xs space-y-1">
                              <div className="text-[11px] font-semibold text-slate-500 truncate uppercase tracking-wider">{c.label}</div>
                              <div className="text-lg font-bold font-mono text-slate-900">{c.formatted_value}</div>
                            </div>
                          ))}
                        </div>
                      )}

                      {/* DATA MATRIX & VISUAL WORKBENCH */}
                      {msg.response && msg.response.data && msg.response.data.length > 0 && (
                        <div className="border border-slate-200 rounded-xl overflow-hidden bg-white shadow-xs space-y-0 mt-2">
                          
                          {/* Workspace Bar */}
                          <div className="px-4 py-2.5 bg-slate-50 border-b border-slate-200 flex flex-wrap items-center justify-between gap-3">
                            
                            {/* View Switcher Tabs (Combined vs Individual) */}
                            {hasMultipleViews ? (
                              <div className="flex items-center gap-1 bg-slate-200/80 p-0.5 rounded-lg">
                                <button
                                  type="button"
                                  onClick={() => {
                                    setSelectedViewByMsg(prev => ({ ...prev, [msg.id]: '__combined__' }));
                                    setPageByMsg(prev => ({ ...prev, [msg.id]: 1 }));
                                    setTableSearchByMsg(prev => ({ ...prev, [msg.id]: '' }));
                                  }}
                                  className={`px-2.5 py-1 rounded-md text-xs font-bold transition-all cursor-pointer ${
                                    activeViewKey === '__combined__' ? 'bg-white text-slate-900 shadow-2xs' : 'text-slate-600 hover:text-slate-900'
                                  }`}
                                >
                                  Combined ({msg.response?.data?.length || 0})
                                </button>
                                {viewKeys.map(vName => (
                                  <button
                                    key={vName}
                                    type="button"
                                    onClick={() => {
                                      setSelectedViewByMsg(prev => ({ ...prev, [msg.id]: vName }));
                                      setPageByMsg(prev => ({ ...prev, [msg.id]: 1 }));
                                      setTableSearchByMsg(prev => ({ ...prev, [msg.id]: '' }));
                                    }}
                                    className={`px-2.5 py-1 rounded-md text-xs font-bold transition-all cursor-pointer font-mono ${
                                      activeViewKey === vName ? 'bg-white text-slate-900 shadow-2xs' : 'text-slate-600 hover:text-slate-900'
                                    }`}
                                  >
                                    {vName} ({viewsDataMap[vName]?.length || 0})
                                  </button>
                                ))}
                              </div>
                            ) : (
                              <div className="flex items-center gap-1.5 text-xs font-bold text-slate-700 font-mono">
                                <TableIcon className="w-3.5 h-3.5 text-blue-600" />
                                <span>Dataset Records ({filteredRows.length})</span>
                              </div>
                            )}

                            {/* View format toggles (Table / Chart) & Actions */}
                            <div className="flex items-center gap-2">
                              <div className="flex items-center gap-1 bg-slate-200/80 p-0.5 rounded-lg">
                                <button
                                  type="button"
                                  onClick={() => setActiveTabByMsg(prev => ({ ...prev, [msg.id]: 'table' }))}
                                  className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-bold transition-all cursor-pointer ${
                                    currentTab === 'table' ? 'bg-white text-slate-900 shadow-2xs' : 'text-slate-600'
                                  }`}
                                >
                                  <TableIcon className="w-3.5 h-3.5" /> Table
                                </button>
                                <button
                                  type="button"
                                  onClick={() => setActiveTabByMsg(prev => ({ ...prev, [msg.id]: 'chart' }))}
                                  className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-bold transition-all cursor-pointer ${
                                    currentTab === 'chart' ? 'bg-white text-slate-900 shadow-2xs' : 'text-slate-600'
                                  }`}
                                >
                                  <BarChart3 className="w-3.5 h-3.5" /> Chart
                                </button>
                              </div>

                              <button
                                type="button"
                                onClick={() => downloadCSV(responseData, activeViewKey !== '__combined__' ? activeViewKey : undefined)}
                                className="px-2.5 py-1 bg-white hover:bg-slate-100 border border-slate-200 rounded-lg text-xs font-semibold text-slate-700 flex items-center gap-1 cursor-pointer transition-colors shadow-2xs"
                                title="Export CSV"
                              >
                                <Download className="w-3.5 h-3.5 text-blue-600" /> CSV
                              </button>
                            </div>

                          </div>

                          {/* TABLE MATRIX */}
                          {currentTab === 'table' && (
                            <div>
                              <div className="p-2.5 px-3.5 border-b border-slate-100 flex items-center justify-between gap-3 bg-white">
                                <div className="relative flex-1 max-w-sm">
                                  <Search className="w-3.5 h-3.5 text-slate-400 absolute left-2.5 top-2.5" />
                                  <input
                                    type="text"
                                    value={tableSearchByMsg[msg.id] || ''}
                                    onChange={(e) => {
                                      const val = e.target.value;
                                      setTableSearchByMsg(prev => ({ ...prev, [msg.id]: val }));
                                      setPageByMsg(prev => ({ ...prev, [msg.id]: 1 }));
                                    }}
                                    placeholder="Filter in columns..."
                                    className="w-full bg-slate-50 border border-slate-200 rounded-lg pl-8 pr-2.5 py-1.5 text-xs text-slate-800 font-mono focus:outline-none focus:bg-white focus:border-blue-500"
                                  />
                                </div>

                                <div className="flex items-center gap-2 text-xs text-slate-500 font-mono">
                                  <span>Rows per page:</span>
                                  <select
                                    value={pageSize}
                                    onChange={(e) => {
                                      setPageSizeByMsg(prev => ({ ...prev, [msg.id]: Number(e.target.value) }));
                                      setPageByMsg(prev => ({ ...prev, [msg.id]: 1 }));
                                    }}
                                    className="bg-white border border-slate-200 rounded-lg px-2 py-1 text-xs text-slate-700 font-mono"
                                  >
                                    <option value={10}>10</option>
                                    <option value={25}>25</option>
                                    <option value={50}>50</option>
                                    <option value={-1}>All</option>
                                  </select>
                                </div>
                              </div>

                              <div className="overflow-x-auto max-h-80">
                                <table className="w-full text-left text-xs border-collapse font-mono">
                                  <thead className="bg-slate-100/80 text-slate-700 font-bold border-b border-slate-200 sticky top-0 z-10">
                                    <tr>
                                      <th className="px-3 py-2 w-10 text-center text-slate-400 font-sans border-r border-slate-200 text-[10px]">#</th>
                                      {tableHeaders.map((hdr) => (
                                        <th key={hdr} className="px-3.5 py-2 whitespace-nowrap border-r border-slate-200 last:border-r-0 text-[11px] tracking-tight">{hdr}</th>
                                      ))}
                                    </tr>
                                  </thead>
                                  <tbody className="divide-y divide-slate-100 bg-white">
                                    {paginatedRows.map((row, rIdx) => (
                                      <tr key={rIdx} className="hover:bg-blue-50/40 transition-colors">
                                        <td className="px-3 py-2 text-center text-slate-400 font-sans text-[10px] border-r border-slate-100">
                                          {pageSize === -1 ? rIdx + 1 : (page - 1) * pageSize + rIdx + 1}
                                        </td>
                                        {tableHeaders.map((hdr) => {
                                          const val = row[hdr];
                                          const isNum = typeof val === 'number';
                                          const isBreach = String(val).toUpperCase() === 'X' || (hdr.toLowerCase().includes('breach') && val);
                                          return (
                                            <td key={hdr} className={`px-3.5 py-2 whitespace-nowrap border-r border-slate-100 last:border-r-0 text-xs ${
                                              isNum ? 'text-blue-700 font-semibold text-right' : 'text-slate-800'
                                            }`}>
                                              {isBreach ? (
                                                <span className="px-2 py-0.5 rounded-full bg-red-100 text-red-700 font-bold text-[10px] border border-red-200">
                                                  {String(val)}
                                                </span>
                                              ) : isNum ? (
                                                val.toLocaleString()
                                              ) : (
                                                val !== null && val !== undefined ? String(val) : '-'
                                              )}
                                            </td>
                                          );
                                        })}
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>

                              {/* Pagination Bar */}
                              {pageSize !== -1 && filteredRows.length > pageSize && (
                                <div className="p-2.5 px-4 bg-slate-50 border-t border-slate-200 flex items-center justify-between text-xs text-slate-500 font-mono">
                                  <span>Page {page} of {totalPages} ({filteredRows.length} total rows)</span>
                                  <div className="flex items-center gap-1.5">
                                    <button
                                      type="button"
                                      onClick={() => setPageByMsg(prev => ({ ...prev, [msg.id]: Math.max(1, page - 1) }))}
                                      disabled={page === 1}
                                      className="px-2.5 py-1 rounded-lg bg-white border border-slate-200 disabled:opacity-40 text-slate-700 hover:bg-slate-100 cursor-pointer text-xs font-semibold"
                                    >
                                      Prev
                                    </button>
                                    <button
                                      type="button"
                                      onClick={() => setPageByMsg(prev => ({ ...prev, [msg.id]: Math.min(totalPages, page + 1) }))}
                                      disabled={page === totalPages}
                                      className="px-2.5 py-1 rounded-lg bg-white border border-slate-200 disabled:opacity-40 text-slate-700 hover:bg-slate-100 cursor-pointer text-xs font-semibold"
                                    >
                                      Next
                                    </button>
                                  </div>
                                </div>
                              )}
                            </div>
                          )}

                          {/* CHART STUDIO */}
                          {currentTab === 'chart' && (
                            <div className="p-5 h-72 w-full bg-white">
                              <ResponsiveContainer width="100%" height="100%">
                                {currentChartType === 'bar' ? (
                                  <BarChart data={responseData.slice(0, 35)} margin={{ top: 10, right: 20, left: 10, bottom: 25 }}>
                                    <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                                    <XAxis dataKey={xAxisKey} stroke="#94a3b8" tick={{ fontSize: 10 }} angle={-20} textAnchor="end" />
                                    <YAxis stroke="#94a3b8" tick={{ fontSize: 10 }} />
                                    <Tooltip contentStyle={{ backgroundColor: '#0f172a', borderColor: '#334155', borderRadius: '0.75rem', fontSize: '12px', color: '#fff' }} />
                                    {numericalCols.slice(0, 2).map((col, idx) => (
                                      <Bar key={col} dataKey={col} fill={idx === 0 ? '#2563eb' : '#4f46e5'} radius={[4, 4, 0, 0]} />
                                    ))}
                                  </BarChart>
                                ) : currentChartType === 'line' ? (
                                  <LineChart data={responseData.slice(0, 35)} margin={{ top: 10, right: 20, left: 10, bottom: 25 }}>
                                    <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                                    <XAxis dataKey={xAxisKey} stroke="#94a3b8" tick={{ fontSize: 10 }} angle={-20} textAnchor="end" />
                                    <YAxis stroke="#94a3b8" tick={{ fontSize: 10 }} />
                                    <Tooltip contentStyle={{ backgroundColor: '#0f172a', borderColor: '#334155', borderRadius: '0.75rem', fontSize: '12px', color: '#fff' }} />
                                    {numericalCols.slice(0, 2).map((col, idx) => (
                                      <Line key={col} type="monotone" dataKey={col} stroke={idx === 0 ? '#2563eb' : '#10b981'} strokeWidth={2.5} dot={{ r: 3 }} />
                                    ))}
                                  </LineChart>
                                ) : (
                                  <AreaChart data={responseData.slice(0, 35)} margin={{ top: 10, right: 20, left: 10, bottom: 25 }}>
                                    <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                                    <XAxis dataKey={xAxisKey} stroke="#94a3b8" tick={{ fontSize: 10 }} angle={-20} textAnchor="end" />
                                    <YAxis stroke="#94a3b8" tick={{ fontSize: 10 }} />
                                    <Tooltip contentStyle={{ backgroundColor: '#0f172a', borderColor: '#334155', borderRadius: '0.75rem', fontSize: '12px', color: '#fff' }} />
                                    {numericalCols.slice(0, 1).map((col) => (
                                      <Area key={col} type="monotone" dataKey={col} stroke="#2563eb" fillOpacity={0.3} fill="#2563eb" />
                                    ))}
                                  </AreaChart>
                                )}
                              </ResponsiveContainer>
                            </div>
                          )}

                        </div>
                      )}

                    </div>

                  </div>
                );
              })
            )}

            {/* PROCESSING GLOW INDICATOR */}
            {loading && (
              <div className="p-4 rounded-2xl bg-white border border-blue-200 shadow-md flex items-center justify-between animate-pulse">
                <div className="flex items-center gap-3">
                  <div className="w-8 h-8 rounded-xl bg-blue-600 text-white flex items-center justify-center">
                    <RefreshCw className="w-4 h-4 animate-spin" />
                  </div>
                  <div>
                    <div className="text-xs font-bold text-slate-800">Processing Relational Intelligence</div>
                    <div className="text-[11px] text-slate-500 font-mono">Extracting conditions & querying SAP Datasphere views...</div>
                  </div>
                </div>
                <span className="text-xs font-mono font-bold text-blue-600 bg-blue-50 px-3 py-1 rounded-full border border-blue-200">
                  Executing
                </span>
              </div>
            )}

            <div ref={chatEndRef} />
          </div>

        </div>

        {/* BOTTOM COMMAND CONSOLE DOCK */}
        <div className="border-t border-slate-200/90 bg-white px-4 md:px-8 py-3.5 shrink-0 z-20 shadow-lg">
          <div className="max-w-5xl mx-auto space-y-2.5">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Terminal className="w-3.5 h-3.5 text-blue-600" />
                <span className="font-bold text-[11px] uppercase tracking-wider text-slate-700">Analytics Command Console</span>
              </div>
              <div className="flex items-center gap-2 text-[11px] text-slate-500 font-mono">
                <span>Active Space:</span>
                <span className="font-bold text-slate-800 bg-slate-100 px-1.5 py-0.5 rounded border border-slate-200">{spaceId}</span>
              </div>
            </div>

            <form onSubmit={handleQuerySubmit} className="space-y-2">
              <div className="relative rounded-xl border border-slate-300 focus-within:border-blue-600 focus-within:ring-2 focus-within:ring-blue-100 bg-slate-50/70 transition-all">
                <textarea
                  ref={textareaRef}
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  onKeyDown={handleKeyDown}
                  rows={2}
                  placeholder="Enter analytical prompt (e.g. get me the total netrevenue for the materialnumber MAT100054)..."
                  className="w-full bg-transparent resize-none p-3 text-sm text-slate-900 focus:outline-none placeholder:text-slate-400 font-medium leading-relaxed"
                />
              </div>

              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex flex-wrap items-center gap-2">
                  {viewsList.length > 0 ? (
                    viewsList.map(v => (
                      <span key={v} className="px-2 py-0.5 rounded-lg bg-blue-50 border border-blue-200 text-blue-700 text-[11px] font-mono font-semibold flex items-center gap-1">
                        <TableIcon className="w-3 h-3" />
                        {v}
                      </span>
                    ))
                  ) : (
                    <span className="text-[11px] text-slate-400 italic">No views locked (Auto-routing enabled)</span>
                  )}
                  {selectedFields.length > 0 && (
                    <span className="px-2 py-0.5 rounded-lg bg-emerald-50 border border-emerald-200 text-emerald-700 text-[11px] font-mono font-semibold">
                      {selectedFields.length} Fields Selected
                    </span>
                  )}
                </div>

                <button
                  type="submit"
                  disabled={!prompt.trim() || loading}
                  className={`px-4 py-2 rounded-xl font-bold text-xs flex items-center gap-2 transition-all cursor-pointer shadow-md ${
                    prompt.trim() && !loading
                      ? 'bg-gradient-to-r from-blue-600 to-indigo-600 text-white hover:from-blue-700 hover:to-indigo-700 shadow-blue-500/25'
                      : 'bg-slate-200 text-slate-400 cursor-not-allowed shadow-none'
                  }`}
                >
                  {loading ? (
                    <>
                      <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                      <span>Compiling Relational SQL...</span>
                    </>
                  ) : (
                    <>
                      <Zap className="w-3.5 h-3.5" />
                      <span>Execute Query</span>
                      <span className="text-[10px] font-mono opacity-80 pl-1 border-l border-white/30">↵ Enter</span>
                    </>
                  )}
                </button>
              </div>
            </form>
          </div>
        </div>

      </main>

      {/* FIELD SELECTOR STUDIO MODAL */}
      {showFieldSelectorModal && (
        <div className="fixed inset-0 z-50 bg-slate-950/70 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl border border-slate-200 max-w-xl w-full max-h-[80vh] flex flex-col overflow-hidden animate-in fade-in zoom-in-95 duration-150 text-slate-900">
            
            <div className="p-4 border-b border-slate-100 flex items-center justify-between bg-slate-50">
              <div className="flex items-center gap-2">
                <SlidersHorizontal className="w-4 h-4 text-blue-600" />
                <h3 className="font-bold text-sm text-slate-900">Select Specific View Fields</h3>
                {fetchingSchema && (
                  <span className="flex items-center gap-1 text-[11px] text-blue-600 font-medium bg-blue-50 px-2 py-0.5 rounded-full border border-blue-200">
                    <RefreshCw className="w-3 h-3 animate-spin" />
                    Loading Schema...
                  </span>
                )}
              </div>
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => fetchSchemasForViews(viewsList)}
                  disabled={fetchingSchema || viewsList.length === 0}
                  className="p-1.5 text-slate-500 hover:text-slate-800 rounded-lg hover:bg-slate-200 cursor-pointer transition-colors"
                  title="Refresh Schema"
                >
                  <RefreshCw className={`w-3.5 h-3.5 ${fetchingSchema ? 'animate-spin text-blue-600' : ''}`} />
                </button>
                <button
                  type="button"
                  onClick={() => setShowFieldSelectorModal(false)}
                  className="text-slate-400 hover:text-slate-700 p-1.5 rounded-md cursor-pointer transition-colors"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            </div>

            <div className="p-3 border-b border-slate-100 flex items-center justify-between gap-2 bg-white">
              <div className="relative flex-1">
                <Search className="w-3.5 h-3.5 text-slate-400 absolute left-2.5 top-2.5" />
                <input
                  type="text"
                  value={fieldSearch}
                  onChange={(e) => setFieldSearch(e.target.value)}
                  placeholder="Filter fields across views..."
                  className="w-full bg-slate-50 border border-slate-200 rounded-lg pl-8 pr-3 py-1.5 text-xs text-slate-800 font-mono focus:outline-none focus:bg-white focus:border-blue-500"
                />
              </div>
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={selectAllFields}
                  className="px-2.5 py-1 bg-slate-100 hover:bg-slate-200 text-slate-700 rounded-lg text-[11px] font-semibold cursor-pointer transition-colors"
                >
                  Select All
                </button>
                <button
                  type="button"
                  onClick={clearAllFields}
                  className="px-2.5 py-1 bg-slate-100 hover:bg-slate-200 text-slate-700 rounded-lg text-[11px] font-semibold cursor-pointer transition-colors"
                >
                  Clear All
                </button>
              </div>
            </div>

            <div className="flex-1 overflow-y-auto p-4 space-y-4 min-h-[160px]">
              {fetchingSchema ? (
                <div className="flex flex-col items-center justify-center py-10 space-y-3">
                  <div className="p-3 rounded-full bg-blue-50 border border-blue-100 text-blue-600">
                    <RefreshCw className="w-6 h-6 animate-spin" />
                  </div>
                  <div className="text-center space-y-0.5">
                    <p className="text-xs font-semibold text-slate-800">Discovering Field Schema</p>
                    <p className="text-[11px] text-slate-500">Querying live metadata from SAP Datasphere space...</p>
                  </div>
                </div>
              ) : viewsList.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-10 space-y-2 text-center text-slate-500">
                  <SlidersHorizontal className="w-8 h-8 text-slate-300 stroke-1" />
                  <p className="text-xs font-medium text-slate-600">No Views Selected</p>
                  <p className="text-[11px] text-slate-400 max-w-xs">Add one or more view names in the left sidebar to discover and select specific columns.</p>
                </div>
              ) : (
                <>
                  {viewsList.length > 1 && commonFields.length > 0 && (
                    <div className="p-3.5 rounded-xl bg-emerald-50/80 border border-emerald-200 space-y-2.5">
                      <div className="flex items-center justify-between text-xs font-bold text-emerald-950">
                        <span className="flex items-center gap-1.5">
                          <Link2 className="w-3.5 h-3.5 text-emerald-600" />
                          Common Linking Fields ({commonFields.length})
                        </span>
                        <button
                          type="button"
                          onClick={() => selectGroupFields(commonFields)}
                          className="text-[11px] text-emerald-800 font-semibold hover:underline cursor-pointer"
                        >
                          Select All Common
                        </button>
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {commonFields.filter(c => c.toLowerCase().includes(fieldSearch.toLowerCase())).map((col) => (
                          <button
                            key={col}
                            type="button"
                            onClick={() => toggleField(col)}
                            className={`px-2.5 py-1 rounded-lg border text-xs font-mono cursor-pointer flex items-center gap-1.5 transition-colors ${
                              selectedFields.includes(col)
                                ? 'bg-emerald-700 text-white border-emerald-700 font-bold shadow-xs'
                                : 'bg-white border-emerald-200 text-emerald-900 hover:bg-emerald-100'
                            }`}
                          >
                            {selectedFields.includes(col) ? <Check className="w-3 h-3" /> : <Plus className="w-3 h-3 text-emerald-500" />}
                            <span>{col}</span>
                          </button>
                        ))}
                      </div>
                    </div>
                  )}

                  {viewsList.map((viewName) => {
                    const cols = viewSpecificFields[viewName] || [];
                    return (
                      <div key={viewName} className="p-3.5 rounded-xl bg-slate-50 border border-slate-200 space-y-2.5">
                        <div className="flex items-center justify-between text-xs font-bold text-slate-800">
                          <span className="flex items-center gap-1.5">
                            <Tag className="w-3.5 h-3.5 text-blue-600" />
                            {viewName} {viewsList.length > 1 ? 'Unique Fields' : 'Fields'} ({cols.length})
                          </span>
                          {cols.length > 0 && (
                            <button
                              type="button"
                              onClick={() => selectGroupFields(cols)}
                              className="text-[11px] text-blue-600 font-semibold hover:underline cursor-pointer"
                            >
                              Select All
                            </button>
                          )}
                        </div>

                        {cols.length === 0 ? (
                          <p className="text-[11px] text-slate-400 italic">No columns detected for this view yet.</p>
                        ) : (
                          <div className="flex flex-wrap gap-1.5">
                            {cols.filter(c => c.toLowerCase().includes(fieldSearch.toLowerCase())).map((col) => (
                              <button
                                key={col}
                                type="button"
                                onClick={() => toggleField(col)}
                                className={`px-2.5 py-1 rounded-lg border text-xs font-mono cursor-pointer flex items-center gap-1.5 transition-colors ${
                                  selectedFields.includes(col)
                                    ? 'bg-slate-900 text-white border-slate-900 font-bold shadow-xs'
                                    : 'bg-white border-slate-200 text-slate-700 hover:bg-slate-100'
                                }`}
                              >
                                {selectedFields.includes(col) ? <Check className="w-3 h-3" /> : <Plus className="w-3 h-3 text-slate-400" />}
                                <span>{col}</span>
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </>
              )}
            </div>

            <div className="p-3 border-t border-slate-100 bg-slate-50 flex items-center justify-between">
              <span className="text-xs text-slate-500 font-mono">
                {selectedFields.length > 0 ? `${selectedFields.length} fields active` : 'All fields selected'}
              </span>
              <button
                type="button"
                onClick={() => setShowFieldSelectorModal(false)}
                className="px-4 py-1.5 bg-blue-600 hover:bg-blue-700 text-white rounded-xl text-xs font-bold cursor-pointer transition-colors shadow-sm"
              >
                Apply & Close
              </button>
            </div>

          </div>
        </div>
      )}

    </div>
  );
}
