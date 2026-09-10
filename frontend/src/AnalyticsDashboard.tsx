import React, { useState, useEffect } from 'react';
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
  AlertTriangle,
  Zap,
  Cpu,
  Server
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

interface ColumnMeta {
  name: string;
  type: string;
  description: string;
}

interface AnalyticsResponse {
  query_blueprint: {
    view_name: string;
    space_id: string;
    $select?: string;
    $filter?: string;
    $orderby?: string;
    $top?: number;
    $skip?: number;
    explanation?: string;
  };
  retrieved_view_metadata: {
    view_name: string;
    space_id: string;
    description: string;
    columns: ColumnMeta[];
  };
  execution_info: {
    datasphere_endpoint: string;
    space_id: string;
    view_name: string;
    odata_parameters: Record<string, string>;
    dac_enforced_user: string;
    zero_data_leakage_status: string;
    live_mode: boolean;
    total_records?: number;
  };
  data: Record<string, any>[];
}

export default function AnalyticsDashboard() {
  const [prompt, setPrompt] = useState<string>("Select all records");
  const [targetView, setTargetView] = useState<string>("ZSL_FA_ORDERTOCASHCYCLE_KPI");
  const [spaceId, setSpaceId] = useState<string>("RADH_S3P");
  const [serverInfo, setServerInfo] = useState<{ space_id?: string; base_api_url?: string; status?: string } | null>(null);
  
  const [loading, setLoading] = useState<boolean>(false);
  const [response, setResponse] = useState<AnalyticsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<'visual' | 'matrix'>('matrix');
  const [chartType, setChartType] = useState<'bar' | 'line' | 'area'>('bar');
  const [xAxisKey, setXAxisKey] = useState<string>('');
  const [selectedMetrics, setSelectedMetrics] = useState<string[]>([]);
  const [showMetadataInspector, setShowMetadataInspector] = useState<boolean>(true);

  // Load server config info on mount
  useEffect(() => {
    fetch('/api/info')
      .then((res) => res.json())
      .then((data) => {
        setServerInfo(data);
        if (data.space_id) setSpaceId(data.space_id);
      })
      .catch((err) => console.warn('Could not fetch server info:', err));
  }, []);

  // Auto-detect categorical vs numerical fields when live data arrives
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
    }
  }, [response]);

  const handleQuerySubmit = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    if (!prompt.trim()) return;

    setLoading(true);
    setError(null);

    try {
      const res = await fetch('/api/analytics', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_prompt: prompt,
          view_name: targetView.trim() || undefined,
          space_id: spaceId.trim() || undefined
        }),
      });

      if (!res.ok) {
        const errPayload = await res.json().catch(() => ({ detail: 'Network request failed' }));
        throw new Error(errPayload.detail || `Server error ${res.status}`);
      }

      const data: AnalyticsResponse = await res.json();
      setResponse(data);
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
          return typeof val === 'string' ? `"${val.replace(/"/g, '""')}"` : val;
        }).join(',')
      )
    ];
    const blob = new Blob([csvRows.join('\n')], { type: 'text/csv' });
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `datasphere_${response.execution_info.view_name || 'dataset'}_${Date.now()}.csv`;
    a.click();
    window.URL.revokeObjectURL(url);
  };

  const tableHeaders = response?.data && response.data.length > 0 ? Object.keys(response.data[0]) : [];
  const numericalColumns = response?.data && response.data.length > 0 ? Object.keys(response.data[0]).filter(k => typeof response.data[0][k] === 'number') : [];

  return (
    <div className="flex flex-col min-h-screen bg-slate-950 text-slate-100">
      {/* Top Header */}
      <header className="border-b border-slate-800 bg-slate-900/80 backdrop-blur sticky top-0 z-40 px-6 py-3.5 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-tr from-blue-600 to-cyan-500 flex items-center justify-center shadow-lg shadow-blue-500/20 ring-1 ring-blue-400/30">
            <Database className="w-5 h-5 text-white" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-lg font-bold text-slate-100 tracking-tight">SAP Datasphere</h1>
              <span className="px-2 py-0.5 text-xs font-semibold rounded-md bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                Live OData v4
              </span>
            </div>
            <p className="text-xs text-slate-400 font-mono">
              Space: <span className="text-slate-200">{spaceId || 'Configuring'}</span>
            </p>
          </div>
        </div>

        {/* Security & Authentication Badges */}
        <div className="flex items-center gap-4">
          <div className="hidden md:flex items-center gap-2 px-3 py-1.5 rounded-lg bg-emerald-950/40 border border-emerald-500/30 text-emerald-400 text-xs font-medium">
            <ShieldCheck className="w-4 h-4 text-emerald-400 shrink-0" />
            <span>Zero LLM Data Leakage</span>
          </div>

          <div className="flex items-center gap-2 text-xs bg-slate-800/80 border border-slate-700/80 rounded-lg px-3 py-1.5">
            <Lock className="w-3.5 h-3.5 text-blue-400" />
            <span className="text-slate-400">Auth:</span>
            <span className="font-mono text-emerald-400 font-medium">OAuth 2.0 Active</span>
          </div>
        </div>
      </header>

      {/* Main Workspace Layout */}
      <main className="flex-1 max-w-7xl w-full mx-auto p-4 md:p-6 flex flex-col gap-6">
        
        {/* Live Query & Configuration Panel */}
        <div className="bg-slate-900/90 border border-slate-800 rounded-2xl p-5 shadow-xl shadow-black/40">
          <form onSubmit={handleQuerySubmit} className="space-y-4">
            
            <div className="grid grid-cols-1 md:grid-cols-12 gap-3">
              
              {/* Target View Name */}
              <div className="md:col-span-8">
                <label className="block text-xs font-medium text-slate-300 mb-1">
                  Target Datasphere View Name
                </label>
                <input
                  type="text"
                  value={targetView}
                  onChange={(e) => setTargetView(e.target.value)}
                  placeholder="e.g. ZSL_FA_ORDERTOCASHCYCLE_KPI or ZFI_FA_DSO_KPI1"
                  required
                  className="w-full bg-slate-950 border border-slate-700 rounded-xl px-3.5 py-2.5 text-sm text-cyan-300 font-mono focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>

              {/* Space ID Override */}
              <div className="md:col-span-4">
                <label className="block text-xs font-medium text-slate-300 mb-1">
                  Datasphere Space ID
                </label>
                <input
                  type="text"
                  value={spaceId}
                  onChange={(e) => setSpaceId(e.target.value)}
                  placeholder="e.g. RADH_S3P"
                  required
                  className="w-full bg-slate-950 border border-slate-700 rounded-xl px-3.5 py-2.5 text-sm text-slate-200 font-mono focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>

              {/* Natural Language Query Prompt */}
              <div className="md:col-span-12">
                <label className="block text-xs font-medium text-slate-300 mb-1">
                  Analytical Query / Natural Language Request
                </label>
                <div className="flex gap-2">
                  <div className="relative flex-1">
                    <input
                      type="text"
                      value={prompt}
                      onChange={(e) => setPrompt(e.target.value)}
                      required
                      placeholder="e.g. Select all records, or give me the top 10 grossrevenueamount"
                      className="w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-2.5 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-blue-500 placeholder:text-slate-500"
                    />
                  </div>
                  <button
                    type="submit"
                    disabled={loading}
                    className="px-6 py-2.5 bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-500 hover:to-indigo-500 disabled:opacity-50 text-white font-medium text-sm rounded-xl shadow-lg shadow-blue-600/30 flex items-center gap-2 transition-all shrink-0"
                  >
                    {loading ? (
                      <RefreshCw className="w-4 h-4 animate-spin" />
                    ) : (
                      <Send className="w-4 h-4" />
                    )}
                    <span>{loading ? 'Calling SAP...' : 'Execute Live Query'}</span>
                  </button>
                </div>
              </div>
            </div>

          </form>
        </div>

        {/* Error Alert Box */}
        {error && (
          <div className="p-4 rounded-xl bg-red-950/70 border border-red-500/50 text-red-200 text-sm flex items-start gap-3 shadow-lg shadow-red-950/40">
            <AlertTriangle className="w-5 h-5 text-red-400 shrink-0 mt-1" />
            <div className="flex-1 space-y-2">
              <p className="font-bold text-red-300">SAP Datasphere Connection Notice</p>
              <div className="text-xs text-red-100/90 font-mono whitespace-pre-line leading-relaxed bg-black/40 p-3 rounded-lg border border-red-500/20">
                {error}
              </div>
            </div>
          </div>
        )}

        {/* Live Execution Inspector Panel */}
        {response && (
          <div className="bg-slate-900/60 border border-slate-800 rounded-2xl overflow-hidden transition-all">
            <div 
              onClick={() => setShowMetadataInspector(!showMetadataInspector)}
              className="px-5 py-3 bg-slate-900/90 border-b border-slate-800 flex items-center justify-between cursor-pointer hover:bg-slate-850"
            >
              <div className="flex items-center gap-2.5">
                <Cpu className="w-4 h-4 text-cyan-400" />
                <span className="text-xs font-semibold uppercase tracking-wider text-slate-300">
                  Live SAP Datasphere OData Pipeline Inspector
                </span>
                <span className="text-[10px] bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 px-2 py-0.5 rounded-full font-mono">
                  Live Stream Active
                </span>
              </div>
              <span className="text-xs text-slate-400">{showMetadataInspector ? 'Collapse ▲' : 'Expand ▼'}</span>
            </div>

            {showMetadataInspector && (
              <div className="p-5 grid grid-cols-1 md:grid-cols-3 gap-4 bg-slate-950/40 text-xs">
                
                {/* Step 1: Target View & Space */}
                <div className="p-3.5 rounded-xl bg-slate-900 border border-slate-800 space-y-2">
                  <div className="flex items-center gap-2 text-cyan-400 font-semibold">
                    <Database className="w-3.5 h-3.5" />
                    <span>1. Target Schema & Space</span>
                  </div>
                  <div className="space-y-1 text-slate-300 font-mono text-[11px]">
                    <p><span className="text-slate-500">View Name:</span> <span className="text-cyan-300 font-bold">{response.execution_info.view_name}</span></p>
                    <p><span className="text-slate-500">Space ID:</span> <span className="text-slate-300">{response.execution_info.space_id}</span></p>
                    <p><span className="text-slate-500">DAC User:</span> <span className="text-slate-300">{response.execution_info.dac_enforced_user}</span></p>
                  </div>
                </div>

                {/* Step 2: Generated OData Blueprint */}
                <div className="p-3.5 rounded-xl bg-slate-900 border border-slate-800 space-y-2">
                  <div className="flex items-center gap-2 text-purple-400 font-semibold">
                    <Zap className="w-3.5 h-3.5" />
                    <span>2. OData v4 Parameters</span>
                  </div>
                  <div className="space-y-1 font-mono text-[11px] text-slate-300">
                    {response.query_blueprint.$select ? (
                      <p className="truncate"><span className="text-purple-400 font-bold">$select:</span> {response.query_blueprint.$select}</p>
                    ) : (
                      <p className="text-slate-400">All columns selected</p>
                    )}
                    {response.query_blueprint.$filter && (
                      <p className="truncate"><span className="text-purple-400 font-bold">$filter:</span> {response.query_blueprint.$filter}</p>
                    )}
                    {response.query_blueprint.$orderby && (
                      <p className="truncate"><span className="text-purple-400 font-bold">$orderby:</span> {response.query_blueprint.$orderby}</p>
                    )}
                    <p className="text-slate-400 text-[10px] font-sans italic mt-1">{response.query_blueprint.explanation}</p>
                  </div>
                </div>

                {/* Step 3: Live OData Stream Execution */}
                <div className="p-3.5 rounded-xl bg-slate-900 border border-slate-800 space-y-2">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2 text-emerald-400 font-semibold">
                      <CheckCircle2 className="w-3.5 h-3.5" />
                      <span>3. Secure OData Stream</span>
                    </div>
                    <span className="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">
                      ● Live Data
                    </span>
                  </div>
                  <div className="space-y-1 text-[11px] text-slate-300 font-mono">
                    <p className="truncate" title={response.execution_info.datasphere_endpoint}>
                      <span className="text-slate-500">Endpoint:</span> {response.execution_info.datasphere_endpoint}
                    </p>
                    <p><span className="text-slate-500">Auth:</span> <span className="text-emerald-400">OAuth 2.0 Bearer Token</span></p>
                    <p><span className="text-slate-500">Live Rows Retrieved:</span> <span className="text-blue-400 font-bold">{response.data.length} records</span></p>
                  </div>
                </div>

              </div>
            )}
          </div>
        )}

        {/* Results Container with Tab A (Visual Insights) and Tab B (Data Matrix) */}
        {response && (
          <div className="bg-slate-900 border border-slate-800 rounded-2xl shadow-xl overflow-hidden flex flex-col flex-1">
            
            {/* Tab Header Bar */}
            <div className="px-5 py-3 border-b border-slate-800 flex flex-wrap items-center justify-between gap-4 bg-slate-900/80">
              <div className="flex items-center gap-2 bg-slate-950 p-1 rounded-xl border border-slate-800">
                <button
                  type="button"
                  onClick={() => setActiveTab('matrix')}
                  className={`flex items-center gap-2 px-4 py-1.5 rounded-lg text-xs font-semibold transition-all ${
                    activeTab === 'matrix'
                      ? 'bg-blue-600 text-white shadow-md shadow-blue-600/30'
                      : 'text-slate-400 hover:text-slate-200'
                  }`}
                >
                  <TableIcon className="w-4 h-4" />
                  <span>Data Matrix ({response.data.length} Rows)</span>
                </button>

                <button
                  type="button"
                  onClick={() => setActiveTab('visual')}
                  className={`flex items-center gap-2 px-4 py-1.5 rounded-lg text-xs font-semibold transition-all ${
                    activeTab === 'visual'
                      ? 'bg-blue-600 text-white shadow-md shadow-blue-600/30'
                      : 'text-slate-400 hover:text-slate-200'
                  }`}
                >
                  <BarChart3 className="w-4 h-4" />
                  <span>Visual Insights</span>
                </button>
              </div>

              {/* Actions & Controls */}
              <div className="flex items-center gap-3">
                {activeTab === 'visual' && (
                  <div className="flex items-center gap-2 text-xs bg-slate-950 border border-slate-800 rounded-xl p-1">
                    <button
                      type="button"
                      onClick={() => setChartType('bar')}
                      className={`px-2.5 py-1 rounded-lg ${chartType === 'bar' ? 'bg-slate-800 text-blue-400 font-medium' : 'text-slate-400'}`}
                    >
                      Bar
                    </button>
                    <button
                      type="button"
                      onClick={() => setChartType('line')}
                      className={`px-2.5 py-1 rounded-lg ${chartType === 'line' ? 'bg-slate-800 text-blue-400 font-medium' : 'text-slate-400'}`}
                    >
                      Line
                    </button>
                    <button
                      type="button"
                      onClick={() => setChartType('area')}
                      className={`px-2.5 py-1 rounded-lg ${chartType === 'area' ? 'bg-slate-800 text-blue-400 font-medium' : 'text-slate-400'}`}
                    >
                      Area
                    </button>
                  </div>
                )}

                <button
                  type="button"
                  onClick={downloadCSV}
                  disabled={response.data.length === 0}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 disabled:opacity-50 text-slate-300 text-xs font-medium border border-slate-700 transition-colors"
                >
                  <Download className="w-3.5 h-3.5" />
                  <span>Export CSV</span>
                </button>
              </div>
            </div>

            {/* TAB: Data Matrix */}
            {activeTab === 'matrix' && (
              <div className="overflow-x-auto max-h-[600px]">
                {response.data.length === 0 ? (
                  <div className="text-center py-16 text-slate-500 text-sm">
                    No records found in this view.
                  </div>
                ) : (
                  <table className="w-full text-left text-xs text-slate-300">
                    <thead className="bg-slate-950/80 text-slate-400 uppercase font-semibold border-b border-slate-800 sticky top-0 z-10 backdrop-blur">
                      <tr>
                        {tableHeaders.map((header) => (
                          <th key={header} className="px-4 py-3 whitespace-nowrap bg-slate-950">
                            {header}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/60 font-mono">
                      {response.data.map((row, rowIdx) => (
                        <tr key={rowIdx} className="hover:bg-slate-800/40 transition-colors">
                          {tableHeaders.map((header) => {
                            const val = row[header];
                            const isNumber = typeof val === 'number';
                            return (
                              <td key={header} className={`px-4 py-3 whitespace-nowrap ${isNumber ? 'text-blue-300 text-right' : 'text-slate-200'}`}>
                                {isNumber ? val.toLocaleString() : (val !== null && val !== undefined ? String(val) : '-')}
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}

            {/* TAB: Visual Insights */}
            {activeTab === 'visual' && (
              <div className="p-6 flex flex-col gap-5">
                {response.data.length === 0 ? (
                  <div className="text-center py-16 text-slate-500 text-sm">
                    No numerical records available to graph.
                  </div>
                ) : (
                  <>
                    {/* Dimension & Metric Selector */}
                    <div className="flex flex-wrap items-center justify-between gap-4 p-3 bg-slate-950/60 border border-slate-800/80 rounded-xl text-xs">
                      <div className="flex items-center gap-2">
                        <span className="text-slate-400 font-medium">Dimension (X-Axis):</span>
                        <select
                          value={xAxisKey}
                          onChange={(e) => setXAxisKey(e.target.value)}
                          className="bg-slate-900 border border-slate-700 rounded-lg px-2.5 py-1 text-slate-200 focus:outline-none focus:ring-1 focus:ring-blue-500 font-mono"
                        >
                          {tableHeaders.map((h) => (
                            <option key={h} value={h}>{h}</option>
                          ))}
                        </select>
                      </div>

                      <div className="flex items-center gap-2">
                        <span className="text-slate-400 font-medium">Metrics:</span>
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
                                className={`px-2 py-0.5 rounded-md border text-[11px] font-mono transition-colors ${
                                  isSelected
                                    ? 'bg-blue-600/20 border-blue-500 text-blue-300 font-medium'
                                    : 'bg-slate-900 border-slate-800 text-slate-400 hover:text-slate-300'
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
                          <BarChart data={response.data} margin={{ top: 20, right: 30, left: 20, bottom: 40 }}>
                            <CartesianGrid strokeDasharray="3 3" stroke="#334155" opacity={0.5} />
                            <XAxis dataKey={xAxisKey} stroke="#94a3b8" tick={{ fontSize: 11 }} angle={-25} textAnchor="end" />
                            <YAxis stroke="#94a3b8" tick={{ fontSize: 11 }} />
                            <Tooltip
                              contentStyle={{ backgroundColor: '#0f172a', borderColor: '#334155', borderRadius: '0.75rem', color: '#f8fafc' }}
                              itemStyle={{ color: '#60a5fa' }}
                            />
                            <Legend wrapperStyle={{ paddingTop: '10px' }} />
                            {selectedMetrics.map((metric, idx) => (
                              <Bar 
                                key={metric} 
                                dataKey={metric} 
                                fill={idx === 0 ? '#3b82f6' : idx === 1 ? '#10b981' : '#a855f7'} 
                                radius={[6, 6, 0, 0]} 
                              />
                            ))}
                          </BarChart>
                        ) : chartType === 'line' ? (
                          <LineChart data={response.data} margin={{ top: 20, right: 30, left: 20, bottom: 40 }}>
                            <CartesianGrid strokeDasharray="3 3" stroke="#334155" opacity={0.5} />
                            <XAxis dataKey={xAxisKey} stroke="#94a3b8" tick={{ fontSize: 11 }} angle={-25} textAnchor="end" />
                            <YAxis stroke="#94a3b8" tick={{ fontSize: 11 }} />
                            <Tooltip
                              contentStyle={{ backgroundColor: '#0f172a', borderColor: '#334155', borderRadius: '0.75rem', color: '#f8fafc' }}
                            />
                            <Legend wrapperStyle={{ paddingTop: '10px' }} />
                            {selectedMetrics.map((metric, idx) => (
                              <Line 
                                key={metric} 
                                type="monotone" 
                                dataKey={metric} 
                                stroke={idx === 0 ? '#3b82f6' : idx === 1 ? '#10b981' : '#a855f7'} 
                                strokeWidth={2.5} 
                                dot={{ r: 4 }} 
                              />
                            ))}
                          </LineChart>
                        ) : (
                          <AreaChart data={response.data} margin={{ top: 20, right: 30, left: 20, bottom: 40 }}>
                            <defs>
                              <linearGradient id="areaColor" x1="0" y1="0" x2="0" y2="1">
                                <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.8}/>
                                <stop offset="95%" stopColor="#3b82f6" stopOpacity={0}/>
                              </linearGradient>
                            </defs>
                            <CartesianGrid strokeDasharray="3 3" stroke="#334155" opacity={0.5} />
                            <XAxis dataKey={xAxisKey} stroke="#94a3b8" tick={{ fontSize: 11 }} angle={-25} textAnchor="end" />
                            <YAxis stroke="#94a3b8" tick={{ fontSize: 11 }} />
                            <Tooltip
                              contentStyle={{ backgroundColor: '#0f172a', borderColor: '#334155', borderRadius: '0.75rem', color: '#f8fafc' }}
                            />
                            <Legend wrapperStyle={{ paddingTop: '10px' }} />
                            {selectedMetrics.map((metric) => (
                              <Area 
                                key={metric} 
                                type="monotone" 
                                dataKey={metric} 
                                stroke="#3b82f6" 
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

          </div>
        )}

      </main>

      {/* Footer */}
      <footer className="border-t border-slate-900 bg-slate-950 px-6 py-4 text-center text-xs text-slate-500">
        Live SAP Datasphere OData Integration • OAuth 2.0 Client Credentials • Zero Data Leakage
      </footer>
    </div>
  );
}
