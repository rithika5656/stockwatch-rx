import { useEffect, useState } from 'react'
import { BrowserRouter, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { Activity, AlertTriangle, ArrowRight, ArrowUpRight, Bell, Boxes, Building2, CalendarClock, Check, ChevronDown, ChevronLeft, ChevronRight, CircleHelp, Clock3, Download, Droplets, FileBarChart2, Gauge, HeartPulse, LayoutDashboard, LogOut, Menu, PackageSearch, Plus, Search, ShieldAlert, Sparkles, Truck, X } from 'lucide-react'
import { Area, AreaChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { get, getErrorMessage, post, put, remove } from './services/api'
import NetworkFlow from './components/NetworkFlow.jsx'
import './App.css'

const groups = [
  ['MY HOSPITAL', [['/dashboard', 'Dashboard', LayoutDashboard], ['/inventory', 'Inventory', Boxes], ['/forecasting', 'Forecasting', Activity], ['/shortages', 'Shortage Risk', ShieldAlert], ['/expiry-risk', 'Expiry & Waste', CalendarClock]]],
  ['LOCAL NETWORK', [['/nearby', 'Nearby Hospitals', Building2], ['/redistribution', 'Redistribution', Truck], ['/map', 'Supply Map', Activity], ['/shareable-pool', 'Shareable Pool', Boxes]]],
  ['DECISIONS', [['/surgeries', 'Surgery Schedule', CalendarClock], ['/weekly-report', 'Weekly Report', FileBarChart2], ['/prioritisation', 'Prioritisation', Gauge], ['/assistant', 'Copilot', Sparkles], ['/reports', 'Reports', FileBarChart2]]],
]
const titles = { '/dashboard': 'Supply intelligence', '/inventory': 'Inventory position', '/forecasting': 'Demand forecasting', '/shortages': 'Shortage risk', '/expiry-risk': 'Expiry & waste', '/redistribution': 'Redistribution queue', '/prioritisation': 'Critical prioritisation', '/hospitals': 'Hospital network', '/supplies': 'Medical supplies', '/assistant': 'MediSupply Copilot', '/reports': 'Reports & exports', '/surgeries': 'Surgery schedule', '/shareable-pool': 'Shareable pool', '/weekly-report': 'Weekly management report' }

function useApiData(path, refresh = 0, params = {}) {
  const key = JSON.stringify(params)
  const [state, setState] = useState({ data: null, loading: true, error: '' })
  useEffect(() => {
    let active = true
    setState((previous) => ({ ...previous, loading: true, error: '' }))
    get(path, JSON.parse(key)).then((data) => active && setState({ data, loading: false, error: '' })).catch((error) => active && setState({ data: null, loading: false, error: getErrorMessage(error) }))
    return () => { active = false }
  }, [path, refresh, key])
  return state
}

function LoginScreen({ onLogin }) {
  const [hospitalId, setHospitalId] = useState('H001')
  const [password, setPassword] = useState('demo123')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const session = await post('/auth/login', { hospital_id: hospitalId, password })
      window.localStorage.setItem('stockwatch_session', session.access_token)
      window.localStorage.setItem('stockwatch_user', JSON.stringify(session.user))
      onLogin(session.user)
    } catch (requestError) {
      setError(getErrorMessage(requestError))
    } finally {
      setBusy(false)
    }
  }
  return <main className="login-screen"><div className="login-grid" /><section className="login-panel"><div className="login-brand"><div className="brand-mark"><HeartPulse size={22} /></div><div><strong>StockWatch-RX</strong><small>COIMBATORE SUPPLY INTELLIGENCE</small></div></div><p className="eyebrow">// HOSPITAL ACCESS</p><h1>Know before<br /><span>the shortage.</span></h1><p className="login-description">Sign in to view your hospital's inventory, forecast signals and nearby eligible supply.</p><form onSubmit={submit}><label>Hospital ID<select value={hospitalId} onChange={(event) => setHospitalId(event.target.value)}><option value="H001">H001 · KMCH</option><option value="H002">H002 · PSG Hospitals</option><option value="H003">H003 · Kumaran Medical Center</option></select></label><label>Demo password<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>{error && <div className="login-error"><AlertTriangle size={15} />{error}</div>}<button className="button button-primary login-button" disabled={busy}>{busy ? 'Signing in...' : 'Enter hospital workspace'}<ArrowRight size={16} /></button></form><div className="login-footer"><span>DEMO DATA</span><span>3 Coimbatore facilities</span></div></section></main>
}

function Shell() {
  const location = useLocation()
  const [authUser, setAuthUser] = useState(() => JSON.parse(window.localStorage.getItem('stockwatch_user') || 'null'))
  const [collapsed, setCollapsed] = useState(false)
  const [mobile, setMobile] = useState(false)
  const [scenario, setScenario] = useState('outbreak')
  const [scenarioLabel, setScenarioLabel] = useState('Outbreak surge')
  const [refresh, setRefresh] = useState(0)
  useEffect(() => { get('/demo/scenario').then((item) => { setScenario(item.key); setScenarioLabel(item.label) }).catch(() => {}) }, [])
  if (!authUser) return <LoginScreen onLogin={setAuthUser} />
  async function selectScenario(event) {
    try { const item = await post('/demo/scenario', { scenario: event.target.value }); setScenario(item.key); setScenarioLabel(item.label); setRefresh((current) => current + 1) }
    catch { event.target.value = scenario }
  }
  return <div className={`app-shell ${collapsed ? 'sidebar-collapsed' : ''}`}>
    <aside className={`sidebar ${mobile ? 'mobile-open' : ''}`}>
      <div className="brand-lockup"><div className="brand-mark"><HeartPulse size={20} /></div><div className="brand-copy"><strong>StockWatch-RX</strong><small>MEDICAL SUPPLY INTELLIGENCE</small></div><button className="mobile-close icon-button" aria-label="Close menu" onClick={() => setMobile(false)}><X size={18} /></button></div>
      <div className="workspace-chip"><div className="workspace-avatar">{authUser.hospital_id}</div><div><strong>Coimbatore</strong><small>{authUser.hospital_name}</small></div></div>
      <nav>{groups.map(([group, links]) => <div className="nav-group" key={group}><div className="nav-group-label">{group}</div>{links.map(([path, label, Icon]) => <NavLink key={path} to={path} onClick={() => setMobile(false)} className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`} title={collapsed ? label : undefined}><Icon size={18} /><span>{label}</span></NavLink>)}</div>)}</nav>
      <div className="sidebar-bottom"><div className="sidebar-status"><i className="status-dot" /><div><strong>{authUser.hospital_id} · Operational</strong><small>Hospital session active</small></div></div><div className="demo-label">DEMO MODE <i /></div><button className="collapse-button" onClick={() => { window.localStorage.removeItem('stockwatch_session'); window.localStorage.removeItem('stockwatch_user'); setAuthUser(null) }}><LogOut size={15} /><span>Sign out</span></button></div>
    </aside>
    {mobile && <button className="mobile-scrim" aria-label="Close menu" onClick={() => setMobile(false)} />}
    <div className="main-column"><header className="topbar"><button className="mobile-menu icon-button" aria-label="Open menu" onClick={() => setMobile(true)}><Menu size={20} /></button><div className="breadcrumb"><span>STOCKWATCH-RX</span><ChevronRight size={14} /><strong>{titles[location.pathname] || titles['/dashboard']}</strong></div><div className="topbar-actions"><label className="scenario-select"><span>DEMO SCENARIO</span><select value={scenario} onChange={selectScenario} aria-label="Choose demo scenario"><option value="normal">Normal operations</option><option value="outbreak">Outbreak surge</option><option value="critical">Critical shortage</option><option value="expiry">Expiry crisis</option><option value="redistribution">Redistribution opportunity</option><option value="kmch_to_psg">KMCH to PSG transfer demo</option></select><ChevronDown size={14} /></label><span className="header-status"><i className="status-dot" />Operational</span><button className="icon-button notification-button" aria-label="Notifications"><Bell size={18} /><i /></button><div className="user-avatar">DR</div></div></header>
      <main className="page-main"><Routes><Route path="/" element={<Dashboard refresh={refresh} scenarioLabel={scenarioLabel} authUser={authUser} />} /><Route path="/dashboard" element={<Dashboard refresh={refresh} scenarioLabel={scenarioLabel} authUser={authUser} />} /><Route path="/inventory" element={<Inventory refresh={refresh} />} /><Route path="/forecasting" element={<Forecast refresh={refresh} authUser={authUser} />} /><Route path="/shortages" element={<Shortages refresh={refresh} />} /><Route path="/expiry-risk" element={<Expiry refresh={refresh} />} /><Route path="/redistribution" element={<Transfers refresh={refresh} authUser={authUser} />} /><Route path="/prioritisation" element={<Priorities refresh={refresh} />} /><Route path="/hospitals" element={<Hospitals refresh={refresh} />} /><Route path="/supplies" element={<Supplies refresh={refresh} />} /><Route path="/assistant" element={<Assistant />} /><Route path="/reports" element={<Reports refresh={refresh} />} /><Route path="/surgeries" element={<SurgerySchedule refresh={refresh} authUser={authUser} />} /><Route path="/shareable-pool" element={<ShareablePool refresh={refresh} />} /><Route path="/weekly-report" element={<WeeklyReport refresh={refresh} />} /><Route path="*" element={<Dashboard refresh={refresh} scenarioLabel={scenarioLabel} authUser={authUser} />} /></Routes><footer className="page-footer"><span>STOCKWATCH-RX · DECISION SUPPORT PROTOTYPE</span><span>Not medical advice · Validate actions with local teams</span></footer></main>
    </div>
  </div>
}

function Heading({ title, description, action }) { return <div className="page-heading"><div><p className="eyebrow">{title === 'Supply intelligence' ? 'NETWORK CONTROL CENTER' : 'OPERATIONS'}</p><h1>{title}</h1><p className="heading-description">{description}</p></div>{action}</div> }
function PanelHeading({ title, eyebrow, action }) { return <div className="panel-heading"><div><span>{eyebrow}</span><h2>{title}</h2></div>{action}</div> }

function DashboardHero({ data, scenarioLabel, authUser }) {
  const lead = data.critical_supplies.find((item) => item.risk_level === 'CRITICAL') || data.critical_supplies[0]
  return <section className="dashboard-hero">
    <div className="hero-copy">
      <div className="hero-overline"><span>// INTELLIGENCE CONTROL CENTER</span><span>01 / SUPPLY NETWORK</span></div>
      <h1>MEDICAL SUPPLY<br /><span>INTELLIGENCE</span></h1>
      <p className="hero-tagline">Know Before the Shortage.</p>
      <p className="hero-description">Prototype forecasting, risk detection and transfer decision support for {authUser?.hospital_name || 'your hospital'}.</p>
      <p className="prototype-disclaimer">Hospital locations use public information; inventory and demand are synthetic and are not live hospital data.</p>
      <div className="hero-signals"><span><i className="status-dot" />DECISION SUPPORT ACTIVE</span><span>SCENARIO / {scenarioLabel.toUpperCase()}</span><span>LAST UPDATED / {data.updated_at}</span></div>
    </div>
    <aside className="hero-focus"><div className="hero-focus-label"><span>ACTIVE RISK SIGNAL</span><Badge value={lead?.risk_level || 'LOW'} /></div><strong className="hero-focus-days">{lead?.days_until_stockout ?? '--'}<small>DAYS</small></strong><span className="hero-focus-event">SAFETY STOCK BREACH WINDOW</span><span className="hero-focus-name">{lead?.hospital || 'No facility flagged'}</span><span className="hero-focus-supply">{lead?.supply || 'Supply network stable'}</span><div className="hero-focus-footer"><span>{data.kpis.critical_shortages.toString().padStart(2, '0')} CRITICAL SIGNALS</span><span>SIMULATED FORECAST</span></div></aside>
    <div className="hero-baseline" />
  </section>
}

function NearbyPage({ refresh, hospitalId }) {
  const { data, loading, error } = useApiData('/nearby-supplies', refresh)
  const hospitals = useApiData('/hospitals', refresh).data || []
  const supplies = useApiData('/supplies', refresh).data || []
  const [whatIfSource, setWhatIfSource] = useState('')
  const [whatIfSupply, setWhatIfSupply] = useState('MED001')
  const [whatIfEnabled, setWhatIfEnabled] = useState(true)
  const [whatIfQuantity, setWhatIfQuantity] = useState('600')
  const [etaIncrease, setEtaIncrease] = useState('0')
  const [simulation, setSimulation] = useState(null)
  const [simulating, setSimulating] = useState(false)
  const [simulationError, setSimulationError] = useState('')
  const sourceOptions = hospitals.filter((item) => item.hospital_id !== hospitalId)
  const sourceId = whatIfSource || sourceOptions[0]?.hospital_id || ''

  async function simulateTransfer(event) {
    event.preventDefault()
    setSimulating(true)
    setSimulationError('')
    try {
      setSimulation(await post('/nearby-supplies/simulate', {
        source_hospital_id: sourceId,
        supply_id: whatIfSupply,
        sharing_enabled: whatIfEnabled,
        shareable_quantity: Number(whatIfQuantity),
        eta_increase_minutes: Number(etaIncrease),
      }))
    } catch (requestError) {
      setSimulationError(getErrorMessage(requestError))
    } finally {
      setSimulating(false)
    }
  }

  const [requested, setRequested] = useState({})
  const [requestError, setRequestError] = useState('')
  async function requestTransfer(item) {
    try {
      const result = await post(`/redistribution/${item.recommendation_id}/request`)
      setRequested((current) => ({ ...current, [item.recommendation_id]: `${result.status} · ${result.request_id}` }))
      setRequestError('')
    } catch (error) {
      setRequestError(getErrorMessage(error))
    }
  }
  return <><Heading title="Nearby hospitals" description="Local hospitals and supplies explicitly offered to this facility; internal source stock remains private." />
    <section className="panel what-if-panel"><div className="what-if-heading"><div><span className="eyebrow">NON-PERSISTENT SCENARIO</span><h2>What-if transfer feasibility</h2></div><span className="risk-badge medium">SIMULATION</span></div><form className="what-if-controls" onSubmit={simulateTransfer}>
      <Select label="Source hospital" value={sourceId} change={setWhatIfSource} options={sourceOptions.map((item) => [item.hospital_id, item.display_name || item.name])} />
      <Select label="Supply" value={whatIfSupply} change={setWhatIfSupply} options={supplies.map((item) => [item.supply_id, item.name])} />
      <label className="filter-control"><span>Shareable units</span><input type="number" min="0" value={whatIfQuantity} onChange={(event) => setWhatIfQuantity(event.target.value)} /></label>
      <label className="filter-control"><span>Added ETA (minutes)</span><input type="number" min="0" max="10080" value={etaIncrease} onChange={(event) => setEtaIncrease(event.target.value)} /></label>
      <label className="sharing-toggle"><input type="checkbox" checked={whatIfEnabled} onChange={(event) => setWhatIfEnabled(event.target.checked)} /><span>Sharing enabled</span></label>
      <button className="button button-secondary" type="submit" disabled={simulating || !sourceId}>{simulating ? 'Recalculating...' : 'Recalculate feasibility'}<Activity size={14} /></button>
    </form>{simulationError && <div className="simulation-error">{simulationError}</div>}{simulation && <div className="what-if-result"><span className={`feasibility-tag ${simulation.feasible ? 'feasible' : 'not-feasible'}`}>{simulation.status}</span><strong>{simulation.recommended_quantity.toLocaleString()} units</strong><span>{simulation.road_distance_km == null ? 'Road route unavailable' : `${simulation.road_distance_km} km · ${simulation.estimated_travel_minutes} min ETA`}</span><p>{simulation.reason}</p></div>}</section>
    {requestError && <Error message={requestError} />}<section className="nearby-match-list">{loading ? <Loading /> : error ? <Error message={error} /> : !data?.length ? <div className="panel empty-state"><Building2 size={20} /><strong>No shareable supply matches</strong><span>Nearby facilities have not enabled a compatible pool, or no transfer is currently feasible.</span></div> : data.map((item) => <article className="panel nearby-match" key={`${item.hospital_id}-${item.supply_id}`}><div className="nearby-match-main"><span className="eyebrow">{item.hospital_id} · {item.supply_id}</span><h2>{item.hospital_name}</h2><strong>{item.supply}</strong><span>{item.shareable_quantity.toLocaleString()} units shareable</span></div><div className="nearby-match-route"><span><strong>{item.road_distance_km == null ? '—' : `${item.road_distance_km} km`}</strong>road distance</span><span><strong>{item.estimated_travel_minutes == null ? '—' : `${item.estimated_travel_minutes} min`}</strong>OSRM ETA</span><span className={`feasibility-tag ${item.feasible ? 'feasible' : 'not-feasible'}`}>{item.status}</span></div><div className="nearby-match-footer"><p>{item.reason}</p><div className="nearby-match-actions">{item.feasible && <NavLink className="button button-secondary" to="/map">View route <ArrowUpRight size={14} /></NavLink>}<button className="button button-primary" disabled={!item.feasible || !item.recommendation_id || Boolean(requested[item.recommendation_id])} onClick={() => requestTransfer(item)}>{requested[item.recommendation_id] || 'Request transfer'}<ArrowRight size={14} /></button></div></div></article>)}</section><p className="prototype-disclaimer">Hospital locations are based on public map data; supply availability and demand are simulated. Facility {hospitalId} only sees quantities explicitly shared.</p></>
}

function MapPage({ refresh }) {
  return <><Heading title="Supply map" description="Publicly mapped Coimbatore hospitals; inventory and demand are simulated. OSRM road geometry is shown only when a route is available." /><NetworkFlow refresh={refresh} /></>
function Hospitals({ refresh }) { const { data, loading, error } = useApiData('/hospitals', refresh); return <><Heading title="Hospital network" description="Public hospital identity and location; all operational indicators below are simulated prototype data." /><section className="panel data-panel"><Table loading={loading} error={error} rows={data || []} columns={[["Facility", (r) => <Cell r={r.display_name || r.name} sub={`${r.city}, ${r.state}`} />], ['Beds', (r) => r.bed_capacity], ['Occupancy', (r) => `${Math.round(r.occupancy_rate * 100)}%`], ['Emergency load', (r) => `${Math.round(r.emergency_load * 100)}%`], ['Critical gaps', (r) => r.critical_shortages], ['Expiry flags', (r) => r.expiry_risk_count], ['Supply health', (r) => <Health score={r.supply_health_score} />]]} /></section><p className="prototype-disclaimer">Hospital locations use publicly available OpenStreetMap information. Capacity, occupancy, emergency load, and all supply values are simulated.</p></> }
}

function Dashboard({ refresh, scenarioLabel, authUser }) {
  const location = useLocation()
  const { data, loading, error } = useApiData('/dashboard/summary', refresh)
  if (location.pathname === '/nearby') return <NearbyPage refresh={refresh} hospitalId={authUser.hospital_id} />
  if (location.pathname === '/map') return <MapPage refresh={refresh} />
  if (loading && !data) return <Loading />
  if (error) return <Error message={error} />
  const metrics = [['Current inventory', data.kpis.current_inventory_units, 'Usable units at this hospital', Boxes, 'green'], ['Critical supplies', data.kpis.critical_supplies_count, 'Require attention', AlertTriangle, 'red'], ['Forecast alerts', data.kpis.forecast_alerts, 'Local demand signals', Activity, 'teal'], ['Expiry risks', data.kpis.expiry_risks, `${data.kpis.stock_at_risk.toLocaleString()} units at risk`, CalendarClock, 'amber'], ['Nearby surplus', data.kpis.nearby_surplus_hospitals, 'Eligible local sources', Truck, 'blue']]
  const totalRisk = Object.values(data.risk_counts).reduce((sum, count) => sum + count, 0)
  return <><DashboardHero data={data} scenarioLabel={scenarioLabel} authUser={authUser} />
    <section className="dashboard-addons"><article className="panel surgery-impact-card"><div className="impact-card-heading"><span className="eyebrow">NEXT 7 DAYS · PROTOTYPE SIMULATION</span><NavLink className="text-link" to="/surgeries">Surgery schedule <ArrowRight size={14} /></NavLink></div><h2>Upcoming surgery impact</h2><div className="impact-metrics"><div><strong>{data.upcoming_surgery_impact.scheduled_cases}</strong><span>scheduled cases</span></div><div><strong>{data.upcoming_surgery_impact.affected_supplies}</strong><span>affected supplies</span></div><div><strong>+{data.upcoming_surgery_impact.additional_units.toLocaleString()}</strong><span>forecast units</span></div><div><strong>{data.upcoming_surgery_impact.risk_changes}</strong><span>risk levels changed</span></div></div></article><article className="panel pool-summary-card"><span className="eyebrow">OPT-IN SHARING</span><h2>Shareable supply</h2><strong className="pool-summary-value">{data.shareable_supply_units.toLocaleString()} <small>units</small></strong><span className="pool-summary-note">Enabled by this facility</span><NavLink className="text-link" to="/shareable-pool">Manage pool <ArrowRight size={14} /></NavLink></article></section>
    <section className="panel dashboard-weekly-summary"><div><span className="eyebrow">WEEKLY MANAGEMENT SUMMARY</span><strong>{data.kpis.critical_shortages} critical shortages · {data.kpis.expiry_risks} expiry risks · {data.kpis.recommended_transfers} transfer candidates</strong></div><NavLink className="text-link" to="/weekly-report">Open weekly report <ArrowRight size={14} /></NavLink></section>
    <div className="scenario-banner"><div className="scenario-icon"><Activity size={18} /></div><div><strong>{scenarioLabel}</strong><span>Scenario active · Forecasts and recommendations reflect this selection</span></div><span className="banner-tag">DEMO SIGNAL</span></div>
    <section className="kpi-grid">{metrics.map(([label, value, note, Icon, tone]) => <article className="kpi-card" key={label}><div className={`kpi-icon ${tone}`}><Icon size={18} /></div><div className="kpi-label">{label}</div><div className="kpi-value">{value.toLocaleString()}</div><div className="kpi-note">{note}</div></article>)}</section>
    <div className="dashboard-grid"><section className="panel demand-panel"><PanelHeading title="Demand signal" eyebrow={`${authUser?.hospital_id || 'H001'} · NORMAL SALINE 500ML`} action={<span className="chart-legend"><i className="legend-historical" />History <i className="legend-forecast" />Forecast</span>} /><div className="chart-wrap"><ResponsiveContainer width="100%" height="100%"><AreaChart data={data.demand_chart} margin={{ top: 10, right: 12, left: -18, bottom: 0 }}><defs><linearGradient id="demandFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#167966" stopOpacity={0.2} /><stop offset="95%" stopColor="#167966" stopOpacity={0} /></linearGradient></defs><CartesianGrid vertical={false} stroke="#e9eeec" /><XAxis dataKey="day" tickLine={false} axisLine={false} tick={{ fill: '#84918d', fontSize: 11 }} interval={5} /><YAxis tickLine={false} axisLine={false} tick={{ fill: '#84918d', fontSize: 11 }} /><Tooltip /><Area dataKey="historical" type="monotone" stroke="#167966" strokeWidth={2} fill="url(#demandFill)" connectNulls name="Historical" /><Line dataKey="forecast" type="monotone" stroke="#dc8b37" strokeWidth={2} strokeDasharray="5 4" dot={false} connectNulls name="Forecast" /></AreaChart></ResponsiveContainer></div><div className="chart-footnote"><span><ArrowUpRight size={14} />7-day weighted demand baseline</span><span>Forecast confidence: moderate</span></div></section>
      <section className="panel risk-panel"><PanelHeading title="Network risk mix" eyebrow="SHORTAGE EXPOSURE" action={<NavLink className="text-link" to="/shortages">View all <ArrowRight size={14} /></NavLink>} /><div className="risk-total"><strong>{totalRisk}</strong><span>hospital-supply pairs<br />requiring review</span></div><div className="risk-stacks">{Object.entries(data.risk_counts).map(([risk, count]) => <div className={`risk-stack ${risk.toLowerCase()}`} key={risk}><div className="risk-stack-top"><span><i />{risk}</span><strong>{count}</strong></div><div className="risk-track"><i style={{ width: `${Math.max(4, count / Math.max(1, totalRisk) * 100)}%` }} /></div></div>)}</div><div className="risk-insight"><ShieldAlert size={16} /><span><strong>{data.risk_counts.CRITICAL} critical signals</strong> need near-term action</span></div></section></div>
    <div className="dashboard-grid"><section className="panel table-panel"><PanelHeading title="Recommended transfers" eyebrow="OPTIMISED ACTIONS" action={<NavLink className="text-link" to="/redistribution">Open queue <ArrowRight size={14} /></NavLink>} />{data.transfers.slice(0, 4).map((item) => <TransferRow item={item} key={item.recommendation_id} compact />)}</section><section className="panel table-panel"><PanelHeading title="Expiry watch" eyebrow="WASTE PREVENTION" action={<NavLink className="text-link" to="/expiry-risk">View queue <ArrowRight size={14} /></NavLink>} />{data.expiry_risks.slice(0, 5).map((item) => <div className="expiry-row" key={item.batch_id}><div className="expiry-date"><strong>{item.days_until_expiry}</strong><small>DAYS</small></div><div className="expiry-info"><strong>{item.supply}</strong><span>{item.hospital} · {item.batch_id}</span></div><div className="expiry-units"><strong>{item.expected_waste.toLocaleString()}</strong><small>units exposed</small></div></div>)}</section></div>
    <div className="dashboard-grid"><section className="panel table-panel"><PanelHeading title="Facilities to watch" eyebrow="HOSPITAL SUPPLY HEALTH" action={<NavLink className="text-link" to="/hospitals">All hospitals <ArrowRight size={14} /></NavLink>} />{data.critical_hospitals.map((hospital) => <div className="facility-row" key={hospital.hospital_id}><div className="facility-pin"><Building2 size={15} /></div><div className="facility-info"><strong>{hospital.name}</strong><span>{hospital.city} · {hospital.critical_shortages} critical · {hospital.shortage_count} at risk</span></div><Health score={hospital.supply_health_score} /></div>)}</section><section className="panel priority-callout"><div className="callout-top"><div className="callout-icon"><Gauge size={18} /></div><span>PRIORITY ALLOCATION</span><ArrowUpRight size={16} /></div><h2>Make limited stock count.</h2><p>Urgency, emergency load, patient occupancy, and supply criticality are scored transparently.</p><NavLink to="/prioritisation" className="callout-link">Review allocation ranking <ArrowRight size={15} /></NavLink><div className="callout-metric"><strong>{data.critical_supplies[0]?.priority_score ?? 0}</strong><span>Highest priority score<br />out of 100</span></div></section></div>
    <NetworkFlow refresh={refresh} />
  </>
}

function TransferRow({ item, compact, decide }) { return <div className={`transfer-row ${compact ? 'compact' : ''}`}><div className="transfer-route"><div className="transfer-facility source"><i className="route-dot source" /><strong>{item.source_hospital || 'Source unavailable'}</strong></div><div className="transfer-arrow"><span>{item.recommended_quantity.toLocaleString()} units</span><ArrowRight size={15} aria-label="to" /></div><div className="transfer-facility destination"><i className="route-dot destination" /><strong>{item.destination_hospital || 'Destination unavailable'}</strong></div></div><div className="transfer-meta"><div className="transfer-supply"><strong>{item.supply}</strong><span>{item.road_distance_km == null ? 'Road route unverified' : `${item.road_distance_km} km road · ${item.estimated_transport_minutes} min ETA`} · {item.destination_expected_coverage_days}d cover after</span></div><Badge value={item.priority} /><span className={`feasibility-tag ${item.transfer_feasible ? 'feasible' : 'not-feasible'}`}>{item.transfer_feasible ? 'FEASIBLE' : 'NOT FEASIBLE'}</span>{decide && <span className="transfer-actions"><button className="button button-approve" disabled={!item.transfer_feasible} onClick={() => decide(item.recommendation_id, 'approve')}><Check size={14} />Approve</button><button className="icon-button" onClick={() => decide(item.recommendation_id, 'reject')} aria-label="Reject"><X size={16} /></button></span>}</div><p className="transfer-reason">{item.reason} {item.feasibility_reason}</p></div> }
function Badge({ value }) { return <span className={`risk-badge ${(value || 'low').toLowerCase()}`}><i />{value || 'LOW'}</span> }
function Health({ score }) { const tone = score < 50 ? 'red' : score < 70 ? 'amber' : 'green'; return <div className="health-meter"><div><i className={tone} style={{ width: `${score}%` }} /></div><strong>{score}</strong></div> }

function Inventory({ refresh }) {
  const { data, loading, error } = useApiData('/inventory', refresh, { page_size: 1000 })
  const hospitals = useApiData('/hospitals', refresh).data || []
  const supplies = useApiData('/supplies', refresh).data || []
  const [search, setSearch] = useState('')
  const [risk, setRisk] = useState('all')
  const [hospitalId, setHospitalId] = useState('all')
  const [supplyId, setSupplyId] = useState('all')
  const [selected, setSelected] = useState(null)
  const rows = (data || []).filter((row) => (!search || `${row.hospital} ${row.supply} ${row.batch_id}`.toLowerCase().includes(search.toLowerCase())) && (risk === 'all' || row.risk_level === risk || row.expiry_risk === risk) && (hospitalId === 'all' || row.hospital_id === hospitalId) && (supplyId === 'all' || row.supply_id === supplyId))
  return <><Heading title="Inventory position" description="Batch-level visibility with projected consumption and expiry exposure." /><section className="panel data-panel"><div className="table-toolbar"><div className="toolbar-search"><Search size={16} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search hospital, supply, or batch" /></div><select value={hospitalId} onChange={(event) => setHospitalId(event.target.value)} className="filter-select"><option value="all">All hospitals</option>{hospitals.map((item) => <option key={item.hospital_id} value={item.hospital_id}>{item.name}</option>)}</select><select value={supplyId} onChange={(event) => setSupplyId(event.target.value)} className="filter-select"><option value="all">All supplies</option>{supplies.map((item) => <option key={item.supply_id} value={item.supply_id}>{item.name}</option>)}</select><select value={risk} onChange={(event) => setRisk(event.target.value)} className="filter-select"><option value="all">All risk levels</option>{['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].map((item) => <option key={item}>{item}</option>)}</select><span className="filter-count">{rows.length} batches</span></div><Table loading={loading} error={error} rows={rows} click={setSelected} columns={[["Hospital", (r) => <Cell r={r.hospital} sub={r.city} />], ["Supply", (r) => <Cell r={r.supply} sub={r.category} />], ["Batch", (r) => r.batch_id], ["On hand", (r) => r.quantity.toLocaleString()], ["Safety stock", (r) => r.safety_stock.toLocaleString()], ["Daily use", (r) => `${r.daily_demand}/d`], ["Expiry", (r) => `${r.expiry_date} · ${r.days_until_expiry}d`], ["Risk", (r) => <Badge value={r.risk_level} />], ["Waste", (r) => <Badge value={r.expiry_risk} />]]} /></section>{selected && <Drawer title={selected.supply} close={() => setSelected(null)}><Detail label="Hospital" value={selected.hospital} /><Detail label="Batch" value={selected.batch_id} /><Detail label="Quantity" value={selected.quantity.toLocaleString()} /><Detail label="Safety stock" value={selected.safety_stock.toLocaleString()} /><Detail label="Daily forecast" value={`${selected.daily_demand} units`} /><Detail label="Expiry" value={`${selected.expiry_date} (${selected.days_until_expiry} days)`} /></Drawer>}</>
}

function SurgerySchedule({ refresh, authUser }) {
  const [revision, setRevision] = useState(0)
  const [editingId, setEditingId] = useState('')
  const [busy, setBusy] = useState(false)
  const [simulation, setSimulation] = useState(null)
  const [simulating, setSimulating] = useState(false)
  const [error, setError] = useState('')
  const [form, setForm] = useState({ scheduled_date: new Date().toISOString().slice(0, 10), surgery_type: 'general_surgery', number_of_cases: 12, expected_duration_minutes: 120 })
  const { data: types } = useApiData('/surgery-types', refresh)
  const { data: surgeries, loading } = useApiData('/surgeries', refresh + revision)
  const activeSurgeries = (surgeries || []).filter((item) => item.status === 'scheduled')
  const impact = (types || []).find((item) => item.surgery_type === form.surgery_type)?.supplies || []
  const cases = Number(form.number_of_cases) || 0
  const projectedUnits = impact.reduce((sum, item) => sum + item.units_per_case * cases, 0)

  async function save(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const payload = { ...form, number_of_cases: cases, expected_duration_minutes: Number(form.expected_duration_minutes) }
      if (editingId) await put(`/surgeries/${editingId}`, payload)
      else await post('/surgeries', payload)
      setEditingId('')
      setForm((current) => ({ ...current, number_of_cases: 12 }))
      setRevision((value) => value + 1)
    } catch (requestError) {
      setError(getErrorMessage(requestError))
    } finally {
      setBusy(false)
    }
  }

  async function simulate() {
    setSimulating(true)
    setError('')
    try {
      setSimulation(await post('/forecast/simulate', {
        supply_id: 'MED001',
        surgery: { ...form, number_of_cases: cases, expected_duration_minutes: Number(form.expected_duration_minutes) },
      }))
    } catch (requestError) {
      setError(getErrorMessage(requestError))
    } finally {
      setSimulating(false)
    }
  }

  async function cancel(id) {
    try {
      await remove(`/surgeries/${id}`)
      setRevision((value) => value + 1)
    } catch (requestError) {
      setError(getErrorMessage(requestError))
    }
  }

  function edit(item) {
    setEditingId(item.surgery_id)
    setForm({ scheduled_date: item.scheduled_date, surgery_type: item.surgery_type, number_of_cases: item.number_of_cases, expected_duration_minutes: item.expected_duration_minutes })
  }

  return <>
    <Heading title="Surgery schedule" description={`Schedule demo cases for ${authUser?.hospital_name || 'this hospital'}; their mapped supply demand is recalculated in forecasting.`} />
    <div className="schedule-layout">
      <section className="panel schedule-form-panel">
        <PanelHeading title={editingId ? 'Edit scheduled cases' : 'Add scheduled cases'} eyebrow="HOSPITAL-OWNED SCHEDULE" />
        <form className="schedule-form" onSubmit={save}>
          <label className="filter-control"><span>Scheduled date</span><input type="date" min={new Date().toISOString().slice(0, 10)} value={form.scheduled_date} onChange={(event) => setForm({ ...form, scheduled_date: event.target.value })} required /></label>
          <Select label="Surgery type" value={form.surgery_type} change={(value) => setForm({ ...form, surgery_type: value })} options={(types || []).map((item) => [item.surgery_type, item.surgery_type.replaceAll('_', ' ')])} />
          <label className="filter-control"><span>Number of cases</span><input type="number" min="0" max="1000" value={form.number_of_cases} onChange={(event) => setForm({ ...form, number_of_cases: event.target.value })} required /></label>
          <label className="filter-control"><span>Expected duration (minutes)</span><input type="number" min="1" max="1440" value={form.expected_duration_minutes} onChange={(event) => setForm({ ...form, expected_duration_minutes: event.target.value })} required /></label>
          <div className="surgery-impact-preview"><span className="eyebrow">MAPPED ADDITIONAL DEMAND</span><strong>+{projectedUnits.toLocaleString()} units</strong><div>{impact.map((item) => <span key={item.supply_id}>{item.supply}: +{(item.units_per_case * cases).toLocaleString()}</span>)}</div></div>
          {error && <div className="login-error"><AlertTriangle size={15} />{error}</div>}
          {simulation && simulation.scheduled_cases === cases && simulation.scheduled_date === form.scheduled_date && simulation.surgery_type === form.surgery_type && <div className="surgery-simulation-result"><span className="eyebrow">WHAT-IF · NORMAL SALINE</span><div><span>Baseline</span><strong>{simulation.baseline_forecast_daily_demand} / day</strong><Badge value={simulation.baseline_risk_level} /></div><div><span>Adjusted</span><strong>{simulation.adjusted_forecast_daily_demand} / day</strong><Badge value={simulation.adjusted_risk_level} /></div><small>Safety-stock breach: {simulation.baseline_safety_breach_days} → {simulation.adjusted_safety_breach_days} days · +{simulation.surgery_additional_units} surgery units this week</small></div>}
          <div className="schedule-form-actions"><button type="button" className="button button-secondary" onClick={simulate} disabled={simulating || !types?.length}>{simulating ? 'Simulating...' : 'Simulate forecast'}<Activity size={14} /></button><button className="button button-primary" disabled={busy || !types?.length}>{busy ? 'Updating forecast...' : editingId ? 'Save changes' : 'Add surgery schedule'}<Plus size={15} /></button>{editingId && <button type="button" className="button button-secondary" onClick={() => setEditingId('')}>Cancel edit</button>}</div>
        </form>
      </section>
      <section className="schedule-list-section"><div className="schedule-list-heading"><div><span className="eyebrow">{authUser?.hospital_id} · DEMO SCHEDULE</span><h2>Scheduled cases</h2></div><span className="inline-stat">{activeSurgeries.length} active</span></div>
        {loading ? <Loading /> : !activeSurgeries.length ? <div className="panel empty-state"><CalendarClock size={20} /><strong>No scheduled cases</strong><span>Add a schedule to recalculate supply demand.</span></div> : activeSurgeries.map((item) => <article className="panel surgery-row" key={item.surgery_id}><div className="surgery-row-top"><div><span className="eyebrow">{item.surgery_id}</span><h3>{new Date(`${item.scheduled_date}T00:00:00`).toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'short' })}</h3></div><span className="risk-badge medium">{item.number_of_cases} cases</span></div><p>{item.surgery_type.replaceAll('_', ' ')} · {item.expected_duration_minutes} min expected</p><div className="surgery-supply-impact">{item.supply_impact.map((row) => <span key={row.supply_id}>{row.supply} <strong>+{row.quantity.toLocaleString()}</strong></span>)}</div><div className="schedule-row-actions"><button className="button button-secondary" onClick={() => edit(item)}>Edit</button><button className="button button-danger" onClick={() => cancel(item.surgery_id)}>Cancel surgery</button></div></article>)}
      </section>
    </div>
    <p className="prototype-disclaimer">Surgery schedules and consumption coefficients are prototype simulation inputs, not clinical guidance.</p>
  </>
}

function ShareablePool({ refresh }) {
  const [revision, setRevision] = useState(0)
  const [supplyId, setSupplyId] = useState('MED001')
  const [quantity, setQuantity] = useState('0')
  const [enabled, setEnabled] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const { data, loading } = useApiData('/shareable-pool', refresh + revision)
  const rows = data || []
  const selected = rows.find((row) => row.supply_id === supplyId)
  useEffect(() => {
    if (!selected) return
    setQuantity(String(selected.shareable_quantity || 0))
    setEnabled(Boolean(selected.enabled))
  }, [selected?.supply_id])
  const maximumShareable = selected?.maximum_allowed_shareable ?? 0
  const requestedQuantity = quantity === '' ? NaN : Number(quantity)
  const quantityError = quantity === ''
    ? 'Enter a shareable quantity.'
    : requestedQuantity < 0
      ? 'Shareable quantity cannot be negative.'
      : !Number.isInteger(requestedQuantity)
        ? 'Shareable quantity must be a whole number.'
        : maximumShareable === 0 && requestedQuantity > 0
        ? 'No safe surplus is currently available to share.'
        : enabled && requestedQuantity > maximumShareable
          ? `Maximum shareable quantity is ${maximumShareable.toLocaleString()} units.`
          : ''

  async function save(event) {
    event.preventDefault()
    if (quantityError || (enabled && maximumShareable === 0)) {
      setError(quantityError || 'No safe surplus is currently available to share.')
      return
    }
    setBusy(true)
    setError('')
    try {
      await post('/shareable-pool', { supply_id: supplyId, shareable_quantity: Number(quantity), enabled })
      setRevision((value) => value + 1)
    } catch (requestError) {
      setError(getErrorMessage(requestError))
    } finally {
      setBusy(false)
    }
  }

  function choose(id) {
    setSupplyId(id)
    const row = rows.find((item) => item.supply_id === id)
    setQuantity(String(row?.shareable_quantity || 0))
    setEnabled(Boolean(row?.enabled))
  }

  return <>
    <Heading title="Shareable pool" description="Your facility chooses which surplus quantities are visible to nearby hospitals. Internal stock and safety reserves remain private." />
    <div className="share-pool-layout">
      <section className="panel share-pool-form"><PanelHeading title="Configure supply sharing" eyebrow="OPT-IN CONTROL" />
        <form className="schedule-form" noValidate onSubmit={save}>
          <Select label="Medical supply" value={supplyId} change={choose} options={rows.map((row) => [row.supply_id, row.supply])} />
          {selected && <div className="share-private-summary"><Detail label="Total stock · private" value={`${selected.total_stock.toLocaleString()} units`} /><Detail label="Protected reserve · private" value={`${selected.safety_reserve.toLocaleString()} units`} /><Detail label="Available source surplus" value={`${selected.source_surplus.toLocaleString()} units`} /></div>}
          <label className="filter-control"><span>Shareable quantity · max {maximumShareable.toLocaleString()}</span><input type="number" min="0" max={maximumShareable} step="1" value={quantity} onChange={(event) => setQuantity(event.target.value)} aria-invalid={Boolean(quantityError)} aria-describedby="shareable-quantity-feedback" /></label>
          <label className="sharing-toggle"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} /><span>Enable this supply in the local share pool</span></label>
          <div id="shareable-quantity-feedback" className={`shareable-quantity-feedback ${quantityError ? 'invalid' : ''}`} aria-live="polite">{quantityError || (maximumShareable === 0 ? 'No safe surplus is currently available to share.' : `Up to ${maximumShareable.toLocaleString()} units are available after reserve and commitments.`)}</div>
          {error && <div className="login-error"><AlertTriangle size={15} />{error}</div>}
          <button className="button button-primary" disabled={busy || !selected || Boolean(quantityError) || (enabled && maximumShareable === 0)}>{busy ? 'Updating pool...' : 'Save sharing preference'}<Check size={15} /></button>
        </form>
      </section>
      <section className="share-pool-list"><div className="schedule-list-heading"><div><span className="eyebrow">PRIVATE FACILITY VIEW</span><h2>Current pool settings</h2></div></div>{loading ? <Loading /> : rows.map((row) => { const poolIsShareable = row.enabled && row.maximum_allowed_shareable > 0; return <article className="panel pool-row" key={row.supply_id}><div><span className="eyebrow">{row.supply_id}</span><h3>{row.supply}</h3><small>{poolIsShareable ? 'SHAREABLE' : 'NOT SHAREABLE'}</small></div><strong>{poolIsShareable ? Math.min(row.shareable_quantity, row.maximum_allowed_shareable).toLocaleString() : '0'} <small>units visible to network</small></strong><button className="button button-secondary" onClick={() => choose(row.supply_id)}>Configure</button></article> })}</section>
    </div>
    <p className="prototype-disclaimer">Only the enabled quantity is exposed to other hospitals. Inventory, demand, and pool quantities are simulated.</p>
  </>
}

function WeeklyReport({ refresh }) {
  const [revision, setRevision] = useState(0)
  const [endDate, setEndDate] = useState(new Date(Date.now() + 6 * 86400000).toISOString().slice(0, 10))
  const [startDate, setStartDate] = useState(new Date().toISOString().slice(0, 10))
  const { data, loading, error } = useApiData('/management-report/weekly', refresh + revision, { start_date: startDate, end_date: endDate })
  const summary = data?.executive_summary

  function exportCsv() {
    if (!data) return
    const records = [
      ...data.shortages.map((row) => ({ section: 'shortage', supply: row.supply, hospital: row.hospital, risk: row.risk_level, units: row.current_stock, detail: row.explanation })),
      ...data.transfers.map((row) => ({ section: 'transfer', supply: row.supply, hospital: `${row.source_hospital} to ${row.destination_hospital}`, risk: row.transfer_feasible ? 'FEASIBLE' : 'NOT FEASIBLE', units: row.recommended_quantity, detail: row.feasibility_reason })),
      ...data.expiry_risks.map((row) => ({ section: 'expiry', supply: row.supply, hospital: row.hospital, risk: row.risk_level, units: row.expected_waste, detail: row.recommended_action })),
      ...data.management_actions.map((row) => ({ section: 'action', supply: '', hospital: '', risk: row.priority, units: '', detail: row.action })),
    ]
    const columns = ['section', 'supply', 'hospital', 'risk', 'units', 'detail']
    const csv = [columns.join(','), ...records.map((row) => columns.map((key) => `"${String(row[key] ?? '').replaceAll('"', '""')}"`).join(','))].join('\r\n')
    const link = document.createElement('a')
    link.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }))
    link.download = 'stockwatch-rx-weekly-management-report.csv'
    link.click()
    URL.revokeObjectURL(link.href)
  }

  return <>
    <Heading title="Weekly management report" description="Calculated supply risks, opt-in transfer recommendations, expiry exposure, and surgery impact for the selected period." action={<div className="report-actions"><button className="button button-secondary" onClick={() => window.print()} disabled={!data}><FileBarChart2 size={15} />Print / PDF</button><button className="button button-primary" onClick={exportCsv} disabled={!data}><Download size={15} />Export CSV</button></div>} />
    <section className="panel report-period"><label className="filter-control"><span>Start date</span><input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} /></label><label className="filter-control"><span>End date</span><input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} /></label><button className="button button-secondary" onClick={() => setRevision((value) => value + 1)}>Refresh report</button></section>
    {loading && !data ? <Loading /> : error ? <Error message={error} /> : data && <div className="weekly-report-print"><div className="report-titleline"><div><span className="eyebrow">{data.period.start_date} — {data.period.end_date}</span><h2>Weekly network overview</h2></div><span className="risk-badge medium">DEMO DATA</span></div>
      <section className="report-summary-grid">{[['Hospitals monitored', summary.hospitals_monitored], ['Critical shortages', summary.critical_shortages], ['High-risk supplies', summary.high_risk_supplies], ['Expiry risks', summary.expiry_risks], ['Transfers recommended', summary.transfers_recommended], ['Transfers completed', summary.transfers_completed], ['Surgery cases', summary.upcoming_surgery_cases]].map(([label, value]) => <article className="report-summary-item" key={label}><span>{label}</span><strong>{value.toLocaleString()}</strong></article>)}</section>
      <ReportSection title="Shortage risk" rows={data.shortages} empty="No shortage risks for this hospital in the active analysis." columns={['Supply', 'Hospital', 'Risk', 'On hand', 'Forecast / day', 'Safety breach']} render={(row) => [row.supply, row.hospital, <Badge key="risk" value={row.risk_level} />, row.current_stock.toLocaleString(), row.forecast_daily_demand.toLocaleString(), `${row.days_until_stockout} days`]} />
      <ReportSection title="Surgery impact" rows={data.surgeries} empty="No scheduled surgeries in this report period." columns={['Date', 'Surgery type', 'Cases', 'Affected supplies / units', 'Adjusted forecast / day', 'Risk change']} render={(row) => [row.scheduled_date, row.surgery_type.replaceAll('_', ' '), row.number_of_cases, row.supply_impact.map((item) => `${item.supply} +${item.additional_units}`).join(' · '), row.supply_impact.map((item) => `${item.supply}: ${item.adjusted_forecast_daily_demand}`).join(' · '), row.supply_impact.map((item) => `${item.baseline_risk_level} → ${item.adjusted_risk_level}`).join(' · ')]} />
      <ReportSection title="Redistribution" rows={data.transfers} empty="No transfer candidates for this facility." columns={['Source', 'Destination', 'Supply', 'Quantity', 'Road distance', 'ETA', 'Feasibility', 'Reason']} render={(row) => [row.source_hospital, row.destination_hospital, row.supply, row.recommended_quantity.toLocaleString(), row.road_distance_km == null ? 'Unavailable' : `${row.road_distance_km} km`, row.estimated_transport_minutes == null ? 'Unavailable' : `${row.estimated_transport_minutes} min`, row.transfer_feasible ? 'FEASIBLE' : 'NOT FEASIBLE', row.feasibility_reason]} />
      <ReportSection title="Expiry risk" rows={data.expiry_risks} empty="No expiry-risk records for this hospital." columns={['Supply', 'Hospital', 'Batch', 'Expiry', 'Expected use', 'Potential waste', 'FEFO action']} render={(row) => [row.supply, row.hospital, row.batch_id, row.expiry_date, row.expected_usage.toLocaleString(), row.expected_waste.toLocaleString(), row.recommended_action]} />
      <section className="panel report-actions-list"><PanelHeading title="Management actions" eyebrow="DATA-BASED FOLLOW-UP" />{data.management_actions.length ? data.management_actions.map((item, index) => <div className="report-action-row" key={`${item.action}-${index}`}><Badge value={item.priority} /><div><strong>{item.action}</strong><span>{item.basis}</span></div></div>) : <div className="empty-state">No actions generated from current risk data.</div>}</section>
      <p className="prototype-disclaimer">{data.disclaimer}</p>
    </div>}
  </>
}

function ReportSection({ title, rows, columns, render, empty }) {
  return <section className="panel weekly-table-panel"><PanelHeading title={title} eyebrow="REPORT DETAIL" />{rows.length ? <div className="table-scroll"><table className="data-table"><thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={row.surgery_id || row.recommendation_id || row.batch_id || `${row.hospital_id}-${row.supply_id}-${index}`}>{render(row).map((value, cellIndex) => <td key={cellIndex}>{value}</td>)}</tr>)}</tbody></table></div> : <div className="empty-state">{empty}</div>}</section>
}

function Forecast({ refresh, authUser }) {
  const hospitals = useApiData('/hospitals', refresh).data || []
  const supplies = useApiData('/supplies', refresh).data || []
  const [hospital, setHospital] = useState(authUser?.hospital_id || 'H001')
  const [supply, setSupply] = useState('MED001')
  const [horizon, setHorizon] = useState('14')
  const { data, loading, error } = useApiData('/forecast', refresh, { hospital_id: hospital, supply_id: supply, horizon_days: Number(horizon) })
  const points = [...(data?.historical_series || []).slice(-20).map((value, index) => ({ label: `-${20 - index}d`, historical: value })), ...(data?.forecast_series || []).map((item) => ({ label: `+${item.day}d`, forecast: item.demand }))]
  const components = data?.forecast_components
  const componentRows = components ? [
    ['Historical baseline', components.historical_baseline],
    ['Existing model adjustments', components.existing_model_adjustments],
    ['Surgery demand · 7-day average', components.surgery_additional_daily_average],
    ['Adjusted forecast', components.adjusted_daily_forecast],
  ] : []
  const visibleHospitals = hospitals.filter((item) => authUser?.role === 'network_admin' || item.hospital_id === authUser?.hospital_id)

  return <>
    <Heading title="Demand forecasting" description="Historical demand plus dated surgery requirements and existing model adjustments. Prototype decision support only." />
    <section className="panel filter-panel">
      <Select label="Hospital" value={hospital} change={setHospital} options={visibleHospitals.map((item) => [item.hospital_id, item.name])} />
      <Select label="Medical supply" value={supply} change={setSupply} options={supplies.map((item) => [item.supply_id, item.name])} />
      <label className="filter-control"><span>Horizon</span><select value={horizon} onChange={(event) => setHorizon(event.target.value)}><option value="7">7 days</option><option value="14">14 days</option><option value="30">30 days</option><option value="60">60 days</option></select></label>
    </section>
    {error ? <Error message={error} /> : <>
      <div className="forecast-kpis">{[['Current stock', data?.current_stock?.toLocaleString(), 'units'], ['Average daily demand', data?.average_daily_demand, 'units / day'], ['Forecast demand', data?.forecast_daily_demand, 'units / day'], ['Expected stock-out', `${data?.days_until_stockout ?? '—'} days`, data?.stockout_date]].map(([label, value, note]) => <div className="forecast-metric" key={label}><span>{label}</span><strong>{loading ? '…' : value ?? '—'}</strong><small>{note}</small></div>)}</div>
      <section className="panel forecast-chart-panel">
        <PanelHeading title={`${data?.supply || 'Supply'} demand curve`} eyebrow={`${visibleHospitals.find((item) => item.hospital_id === hospital)?.name || hospital} · ${horizon} DAY HORIZON`} action={<Badge value={data?.risk_level} />} />
        <div className="large-chart"><ResponsiveContainer width="100%" height="100%"><LineChart data={points}><CartesianGrid vertical={false} stroke="#e8eeeb" /><XAxis dataKey="label" tickLine={false} axisLine={false} interval={4} /><YAxis tickLine={false} axisLine={false} /><Tooltip /><Line dataKey="historical" name="Historical" stroke="#167966" strokeWidth={2} dot={false} connectNulls /><Line dataKey="forecast" name="Forecast" stroke="#dc8b37" strokeWidth={2} strokeDasharray="5 4" dot={false} connectNulls /></LineChart></ResponsiveContainer></div>
        <div className="forecast-explanation"><CircleHelp size={17} /><div><strong>Why did the forecast change?</strong><p>{data?.explanation}</p><div className="forecast-component-grid">{componentRows.map(([label, value]) => <div key={label}><span>{label}</span><strong>{Number(value).toLocaleString(undefined, { maximumFractionDigits: 1 })} units/day</strong></div>)}</div><small>Historical consumption and scheduled surgery demand are synthetic prototype inputs.</small></div></div>
      </section>
    </>}
  </>
}
function Select({ label, value, change, options }) { return <label className="filter-control"><span>{label}</span><select value={value} onChange={(event) => change(event.target.value)}>{options.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label> }

function Shortages({ refresh }) { const { data, loading, error } = useApiData('/shortages', refresh); return <><Heading title="Shortage risk" description="Forecast-ranked supply gaps, ordered by projected stock-out urgency." /><section className="panel data-panel"><Table loading={loading} error={error} rows={data || []} columns={[['Risk', (r) => <Badge value={r.risk_level} />], ['Hospital', (r) => r.hospital], ['Supply', (r) => r.supply], ['On hand', (r) => r.current_stock.toLocaleString()], ['Daily forecast', (r) => `${r.forecast_daily_demand}/d`], ['Stock-out', (r) => <strong className="critical-value">{r.days_until_stockout} days</strong>], ['Probability', (r) => `${Math.round(r.shortage_probability * 100)}%`], ['Driver', (r) => r.outbreak_signal ? 'Outbreak signal' : `Trend ${r.trend_percent}%`]]} /></section></> }

function Expiry({ refresh }) { const { data, loading, error } = useApiData('/expiry-risks', refresh); return <><Heading title="Expiry & waste" description="Expected remaining quantity by batch, projected through expiry." /><section className="panel data-panel"><Table loading={loading} error={error} rows={data || []} columns={[['Risk', (r) => <Badge value={r.risk_level} />], ['Hospital', (r) => r.hospital], ['Supply / batch', (r) => <Cell r={r.supply} sub={r.batch_id} />], ['Quantity', (r) => r.quantity.toLocaleString()], ['Expiry', (r) => `${r.expiry_date} · ${r.days_until_expiry}d`], ['Expected use', (r) => r.expected_usage.toLocaleString()], ['Expected waste', (r) => <strong className="critical-value">{r.expected_waste.toLocaleString()}</strong>], ['Action', (r) => r.recommended_action]]} /></section></> }

function FulfillmentProgressCard({ request, direction, authUser, updateLeg, cancelRequest }) {
  const legs = request.visibleLegs || []
  return <article className="panel fulfillment-request">
    <header className="fulfillment-request-header">
      <div><span className="eyebrow">{direction === 'incoming' ? 'INCOMING SUPPLY' : 'OUTGOING SUPPLY'} · {request.request_id}</span><h3>{request.supply}</h3></div>
      <span className={`fulfillment-status ${request.status.toLowerCase()}`}>{request.status.replaceAll('_', ' ')}</span>
    </header>
    <div className="fulfillment-progress">
      <div><strong>{request.fulfilled_quantity.toLocaleString()} <small>/ {request.requested_quantity.toLocaleString()} units</small></strong><span>{request.remaining_quantity.toLocaleString()} remaining · {request.matching_status.replaceAll('_', ' ')}</span></div>
      <div className="fulfillment-progress-track"><i style={{ width: `${Math.min(100, request.fulfilled_quantity / request.requested_quantity * 100)}%` }} /></div>
    </div>
    {legs.length ? <div className="fulfillment-legs">{legs.map((leg) => <div className="fulfillment-leg" key={leg.leg_id}>
      <div className="fulfillment-leg-route"><strong>{leg.source_hospital || 'Source unavailable'}</strong><ArrowRight size={15} /><strong>{leg.destination_hospital || 'Destination unavailable'}</strong><span className={`fulfillment-status ${leg.status.toLowerCase()}`}>{leg.status.replaceAll('_', ' ')}</span></div>
      <div className="fulfillment-leg-meta"><b>{leg.allocated_quantity.toLocaleString()} units</b><span>{leg.route_distance_km} km road</span><span>ETA {leg.estimated_eta_minutes} min</span><span>{leg.route_provider}</span></div>
      <small className="fulfillment-batches">FEFO batches: {leg.source_batch_allocations.map((batch) => `${batch.batch_id} · ${batch.quantity}`).join(' / ')}</small>
      {leg.failure_reason && <small className="fulfillment-failure">{leg.failure_reason}</small>}
      <div className="fulfillment-leg-actions">
        {leg.status === 'OFFERED' && leg.source_hospital_id === authUser.hospital_id && <><button className="button button-primary" onClick={() => updateLeg(request.request_id, leg.leg_id, 'accept')}><Check size={14} />Accept & commit</button><button className="button button-danger" onClick={() => updateLeg(request.request_id, leg.leg_id, 'reject')}><X size={14} />Reject</button></>}
        {leg.status === 'COMMITTED' && leg.source_hospital_id === authUser.hospital_id && <button className="button button-primary" onClick={() => updateLeg(request.request_id, leg.leg_id, 'status', { status: 'IN_TRANSIT' })}><Truck size={14} />Dispatch</button>}
        {leg.status === 'IN_TRANSIT' && leg.destination_hospital_id === authUser.hospital_id && <button className="button button-primary" onClick={() => updateLeg(request.request_id, leg.leg_id, 'status', { status: 'DELIVERED' })}><Check size={14} />Confirm delivery</button>}
        {['COMMITTED', 'IN_TRANSIT'].includes(leg.status) && <button className="button button-danger" onClick={() => updateLeg(request.request_id, leg.leg_id, 'fail', { reason: 'Source stock or route became unavailable.' })}>Report failure</button>}
      </div>
    </div>)}</div> : <div className="fulfillment-no-legs">No eligible source is currently offering this supply.</div>}
    {direction === 'incoming' && !legs.some((leg) => ['DISPATCHING', 'IN_TRANSIT', 'DELIVERED'].includes(leg.status)) && !['CANCELLED', 'FULLY_FULFILLED'].includes(request.status) && <button className="button button-secondary fulfillment-cancel" onClick={() => cancelRequest(request.request_id)}>Cancel request</button>}
  </article>
}

function Transfers({ refresh, authUser }) {
  const [revision, setRevision] = useState(0)
  const [supplyId, setSupplyId] = useState('MED001')
  const [quantity, setQuantity] = useState('600')
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState('')
  const [statuses, setStatuses] = useState({})
  const { data, loading, error } = useApiData('/redistribution', refresh)
  const { data: requests, loading: requestsLoading, error: requestsError } = useApiData('/supply-requests', refresh + revision)
  const suppliesData = useApiData('/supplies', refresh).data || []
  useEffect(() => {
    const interval = window.setInterval(() => setRevision((value) => value + 1), 5000)
    return () => window.clearInterval(interval)
  }, [])
  async function createRequest(event) {
    event.preventDefault()
    setBusy(true)
    try {
      const request = await post('/supply-requests', { supply_id: supplyId, requested_quantity: Number(quantity) })
      setToast(`${request.request_id} matching started.`)
      setRevision((value) => value + 1)
    } catch (requestError) { setToast(getErrorMessage(requestError)) }
    finally { setBusy(false) }
  }
  async function updateLeg(requestId, legId, action, body) {
    try {
      await post(`/supply-requests/${requestId}/legs/${legId}/${action}`, body)
      setToast(action === 'accept' ? 'Stock committed. Matching the remaining need now.' : action === 'reject' ? 'Offer rejected. Searching the next eligible source.' : 'Fulfillment progress updated.')
      setRevision((value) => value + 1)
    } catch (requestError) { setToast(getErrorMessage(requestError)) }
  }
  async function cancelRequest(requestId) {
    try { await remove(`/supply-requests/${requestId}`); setToast('Supply request cancelled.'); setRevision((value) => value + 1) }
    catch (requestError) { setToast(getErrorMessage(requestError)) }
  }
  async function decideRecommendation(id, action) {
    try {
      const result = await post(`/redistribution/${id}/${action}`)
      setStatuses((current) => ({ ...current, [id]: result.status }))
      setToast(`Recommendation ${result.status}.`)
    } catch (requestError) { setToast(getErrorMessage(requestError)) }
  }
  const incomingRequests = (requests || []).filter((request) => request.destination_hospital_id === authUser.hospital_id)
    .map((request) => ({ ...request, visibleLegs: request.legs.filter((leg) => leg.destination_hospital_id === authUser.hospital_id) }))
  const outgoingRequests = (requests || []).map((request) => ({
    ...request,
    visibleLegs: request.legs.filter((leg) => leg.source_hospital_id === authUser.hospital_id),
  })).filter((request) => request.visibleLegs.length > 0)
  return <><Heading title="Redistribution queue" description="Dynamic Multi-Source Fulfilment matches opt-in supply, commits partial quantities, and automatically searches again for the remaining need." />
    <section className="panel fulfillment-create-panel"><div><span className="eyebrow">DYNAMIC MULTI-SOURCE FULFILMENT</span><h2>Request supply across the local network</h2><p>Each source commits only what it can safely provide. Remaining demand is re-matched immediately.</p></div><form className="fulfillment-create-form" onSubmit={createRequest}><label className="filter-control"><span>Medical supply</span><select value={supplyId} onChange={(event) => setSupplyId(event.target.value)}>{suppliesData.map((item) => <option key={item.supply_id} value={item.supply_id}>{item.name}</option>)}</select></label><label className="filter-control"><span>Total required units</span><input type="number" min="1" value={quantity} onChange={(event) => setQuantity(event.target.value)} /></label><button className="button button-primary" disabled={busy || !suppliesData.length}>{busy ? 'Matching sources...' : 'Create supply request'}<ArrowRight size={15} /></button></form></section>
    {toast && <div className="toast-message"><Check size={15} />{toast}</div>}
    <section className="fulfillment-section"><div className="fulfillment-section-heading"><div><span className="eyebrow">MY HOSPITAL · {authUser.hospital_name}</span><h2>Incoming and outgoing supplies</h2></div><span className="fulfillment-live"><i className="status-dot" />RE-MATCHING ACTIVE</span></div>
      {requests?.length > 0 && <div className="fulfillment-direction-groups">
        <section className="fulfillment-direction-group"><div className="fulfillment-direction-heading"><h3>INCOMING</h3><span>Destination is {authUser.hospital_name}</span></div>{incomingRequests.length ? <div className="fulfillment-request-list directional">{incomingRequests.map((request) => <FulfillmentProgressCard key={`incoming-${request.request_id}`} request={request} direction="incoming" authUser={authUser} updateLeg={updateLeg} cancelRequest={cancelRequest} />)}</div> : <div className="fulfillment-direction-empty">No incoming supply requests.</div>}</section>
        <section className="fulfillment-direction-group"><div className="fulfillment-direction-heading"><h3>OUTGOING</h3><span>Source is {authUser.hospital_name}</span></div>{outgoingRequests.length ? <div className="fulfillment-request-list directional">{outgoingRequests.map((request) => <FulfillmentProgressCard key={`outgoing-${request.request_id}`} request={request} direction="outgoing" authUser={authUser} updateLeg={updateLeg} cancelRequest={cancelRequest} />)}</div> : <div className="fulfillment-direction-empty">No outgoing transfers.</div>}</section>
      </div>}
      {requestsError && !requests ? <Error message={requestsError} /> : requestsLoading && !requests ? <Loading /> : !requests?.length ? <div className="panel empty-state"><Truck size={20} /><strong>No supply requests yet</strong><span>New requests will appear here with each source leg, route, ETA, and committed quantity.</span></div> : null}
    </section>
    <section className="fulfillment-section legacy-recommendations"><div className="fulfillment-section-heading"><div><span className="eyebrow">DECISION ENGINE</span><h2>Current transfer recommendations</h2></div></div>{error ? <Error message={error} /> : loading ? <Loading /> : !data?.length ? <div className="panel empty-state"><Truck size={20} /><strong>No transfer candidates</strong><span>No route-verified candidate currently meets source reserve and expiry constraints.</span></div> : <div className="recommendation-list">{data.map((item) => <article className="panel recommendation-panel" key={item.recommendation_id}><div className="recommendation-top"><div><span className="eyebrow">{item.recommendation_id}</span><h2>{item.supply}</h2></div><Badge value={item.priority} /></div><TransferRow item={item} decide={decideRecommendation} /><div className="recommendation-details"><Detail label="Available shareable" value={`${item.shareable_quantity.toLocaleString()} units`} /><Detail label="Suggested quantity" value={`${item.recommended_quantity.toLocaleString()} units`} /><Detail label="Road distance" value={item.road_distance_km == null ? 'Unverified' : `${item.road_distance_km} km`} /><Detail label="ETA" value={item.estimated_transport_minutes == null ? 'Unverified' : `${item.estimated_transport_minutes} minutes`} /><Detail label="Status" value={statuses[item.recommendation_id] || item.status} /></div><div className="action-status">{item.feasibility_reason}</div></article>)}</div>}</section>
  </>
}

function Priorities({ refresh }) { const { data, loading, error } = useApiData('/prioritisation', refresh); return <><Heading title="Critical prioritisation" description="Transparent allocation scoring across emergency demand, patient load, stock-out urgency, alternatives, and criticality." /><section className="score-method"><strong>Priority score weights</strong><div className="weight-chips"><span>Emergency demand <b>35%</b></span><span>Patient load <b>25%</b></span><span>Stock-out urgency <b>20%</b></span><span>Alternative unavailable <b>10%</b></span><span>Supply criticality <b>10%</b></span></div></section><section className="panel data-panel"><Table loading={loading} error={error} rows={data || []} columns={[['Priority', (r) => <Badge value={r.priority} />], ['Hospital', (r) => <Cell r={r.hospital} sub={`${r.days_until_stockout} days cover`} />], ['Supply', (r) => <Cell r={r.supply} sub={`${r.criticality} criticality`} />], ['Score', (r) => <Score value={r.priority_score} />], ['Probability', (r) => `${Math.round(r.shortage_probability * 100)}%`], ['Decision factors', (r) => r.reasons.join(' · ')]]} /></section><p className="method-disclaimer">Scores support operational review; they are not clinical triage.</p></> }

function Hospitals({ refresh }) { const { data, loading, error } = useApiData('/hospitals', refresh); return <><Heading title="Hospital network" description="Facility supply health across the synthetic demo network." /><section className="panel data-panel"><Table loading={loading} error={error} rows={data || []} columns={[['Facility', (r) => <Cell r={r.name} sub={`${r.city}, ${r.state}`} />], ['Beds', (r) => r.bed_capacity], ['Occupancy', (r) => `${Math.round(r.occupancy_rate * 100)}%`], ['Emergency load', (r) => `${Math.round(r.emergency_load * 100)}%`], ['Critical gaps', (r) => r.critical_shortages], ['Expiry flags', (r) => r.expiry_risk_count], ['Supply health', (r) => <Health score={r.supply_health_score} />]]} /></section></> }
function Supplies({ refresh }) { const { data, loading, error } = useApiData('/supplies', refresh); const [search, setSearch] = useState(''); const rows = (data || []).filter((item) => `${item.name} ${item.category}`.toLowerCase().includes(search.toLowerCase())); return <><Heading title="Medical supplies" description="Catalogue-level stock, aggregate demand, and network exposure." /><section className="panel data-panel"><div className="table-toolbar"><div className="toolbar-search"><Search size={16} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search supply or category" /></div></div><Table loading={loading} error={error} rows={rows} columns={[['Supply', (r) => <Cell r={r.name} sub={r.category} />], ['Criticality', (r) => r.criticality], ['Total stock', (r) => r.total_stock.toLocaleString()], ['Daily demand', (r) => `${r.daily_demand.toLocaleString()}/d`], ['14-day forecast', (r) => r.forecast_demand.toLocaleString()], ['Hospitals at risk', (r) => r.shortage_hospitals], ['Expiry batches', (r) => r.expiry_batches], ['Risk', (r) => <Badge value={r.risk_level} />]]} /></section></> }

function Assistant() {
  const [messages, setMessages] = useState([{ role: 'assistant', text: 'I can query current shortage exposure, expiry risk, inventory totals, and recommended transfers. What would you like to know?' }])
  const [question, setQuestion] = useState('')
  const [busy, setBusy] = useState(false)
  const prompts = ['Which supplies are at highest risk at my hospital?', 'Which local batches expire soon?', 'Why should H002 transfer saline to H001?', 'What happens if demand increases by 20%?']
  async function ask(value = question) { const text = value.trim(); if (!text || busy) return; setMessages((items) => [...items, { role: 'user', text }]); setQuestion(''); setBusy(true); try { const result = await post('/assistant/query', { question: text }); setMessages((items) => [...items, { role: 'assistant', text: result.answer, sources: result.sources }]) } catch (error) { setMessages((items) => [...items, { role: 'assistant', text: getErrorMessage(error) }]) } finally { setBusy(false) } }
  return <><Heading title="MediSupply Copilot" description="Natural-language answers grounded in current backend analysis." action={<span className="demo-ai-badge"><Sparkles size={14} />DEMO AI MODE</span>} /><section className="assistant-layout"><div className="panel chat-panel"><div className="chat-header"><div className="chat-avatar"><Sparkles size={18} /></div><div><strong>MediSupply Copilot</strong><span><i className="status-dot" />Connected to demo data</span></div><span className="mode-pill">RULE-BASED</span></div><div className="chat-messages">{messages.map((item, index) => <div className={`chat-message ${item.role}`} key={index}><div className="message-avatar">{item.role === 'assistant' ? <Sparkles size={15} /> : 'DR'}</div><div className="message-body"><p>{item.text}</p>{item.sources && <small>Data sources: {item.sources.map((source) => source.tool.replaceAll('_', ' ')).join(' · ')}</small>}</div></div>)}{busy && <div className="chat-thinking">Checking current supply signals...</div>}</div><form className="chat-compose" onSubmit={(event) => { event.preventDefault(); ask() }}><input value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="Ask about shortages, expiry, or transfers..." /><button disabled={!question.trim() || busy} aria-label="Send"><ArrowRight size={18} /></button></form><div className="chat-disclaimer">Synthetic demo data · Validate decisions with local teams</div></div><aside className="panel prompt-panel"><span className="eyebrow">SUGGESTED QUESTIONS</span><h2>Explore the network</h2>{prompts.map((prompt) => <button className="suggestion-button" key={prompt} onClick={() => ask(prompt)}>{prompt}<ArrowUpRight size={14} /></button>)}</aside></section></>
}

function Reports({ refresh }) {
  const { data: inventory, loading, error } = useApiData('/inventory', refresh, { page_size: 1000 })
  const shortages = useApiData('/shortages', refresh).data || []
  const expiry = useApiData('/expiry-risks', refresh).data || []
  const transfers = useApiData('/redistribution', refresh).data || []
  function exportCsv() { if (!inventory) return; const cols = ['hospital', 'supply', 'batch_id', 'quantity', 'safety_stock', 'daily_demand', 'expiry_date', 'risk_level', 'expiry_risk']; const csv = [cols.join(','), ...inventory.map((row) => cols.map((key) => `"${String(row[key] ?? '').replaceAll('"', '""')}"`).join(','))].join('\r\n'); const link = document.createElement('a'); link.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' })); link.download = 'stockwatch-rx-inventory.csv'; link.click(); URL.revokeObjectURL(link.href) }
  const reports = [['Supply health report', 'Inventory batches and network signals', HeartPulse, inventory?.length || 0], ['Shortage report', 'Forecast stock-out risks ranked by urgency', ShieldAlert, shortages.length], ['Expiry risk report', 'Batches with expected waste exposure', CalendarClock, expiry.length], ['Redistribution report', 'Transfers with source safety checks', Truck, transfers.length]]
  return <><Heading title="Reports & exports" description="Operational snapshots generated from the active scenario." action={<button className="button button-primary" onClick={exportCsv} disabled={loading || !inventory}><Download size={15} />Export inventory CSV</button>} />{error && <Error message={error} />}<div className="report-grid">{reports.map(([title, description, Icon, count]) => <article className="panel report-card" key={title}><div className="report-icon"><Icon size={18} /></div><span className="eyebrow">LIVE REPORT</span><h2>{title}</h2><p>{description}</p><div className="report-footer"><span>{count.toLocaleString()} records</span><button className="icon-button" onClick={() => title === 'Supply health report' ? exportCsv() : window.print()} aria-label={`Export ${title}`}><Download size={16} /></button></div></article>)}</div><div className="report-note"><CircleHelp size={16} /><p>Exports contain synthetic demo data. Use the browser print dialog for PDF output.</p></div></>
}

function tableRowKey(row, fallback) { return row.inventory_id || row.recommendation_id || row.batch_id || (row.hospital_id && row.supply_id ? `${row.hospital_id}-${row.supply_id}` : null) || row.supply_id || row.hospital_id || fallback }
function Table({ rows = [], columns, loading, error, click }) { const [page, setPage] = useState(1); const pageSize = 25; const pageCount = Math.max(1, Math.ceil(rows.length / pageSize)); const start = (Math.min(page, pageCount) - 1) * pageSize; const visibleRows = rows.slice(start, start + pageSize); useEffect(() => setPage(1), [rows.length]); if (loading && !rows.length) return <Loading />; if (error) return <Error message={error} />; if (!rows.length) return <div className="empty-state"><PackageSearch size={20} /><strong>No records in this view</strong><span>Try another filter or scenario.</span></div>; return <div className="table-scroll"><table className="data-table"><thead><tr>{columns.map(([label]) => <th key={label}>{label}</th>)}</tr></thead><tbody>{visibleRows.map((row, index) => <tr key={tableRowKey(row, start + index)} className={click ? 'clickable-row' : ''} onClick={click ? () => click(row) : undefined}>{columns.map(([label, render]) => <td key={label}>{render(row, start + index)}</td>)}</tr>)}</tbody></table><div className="table-pagination"><span>Showing {start + 1}–{Math.min(start + pageSize, rows.length)} of {rows.length.toLocaleString()} records</span><div><button disabled={page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))} aria-label="Previous page"><ChevronLeft size={15} /></button><span>{page} / {pageCount}</span><button disabled={page >= pageCount} onClick={() => setPage((value) => Math.min(pageCount, value + 1))} aria-label="Next page"><ChevronRight size={15} /></button></div></div></div> }
function Cell({ r, sub }) { return <span className="cell-main"><strong>{r}</strong>{sub && <small>{sub}</small>}</span> }
function Detail({ label, value }) { return <div className="detail-line"><span>{label}</span><strong>{value}</strong></div> }
function Score({ value }) { return <span className="score-cell"><strong>{value}</strong><i><b style={{ width: `${value}%` }} /></i></span> }
function Drawer({ title, close, children }) { return <><button className="drawer-scrim" aria-label="Close" onClick={close} /><aside className="detail-drawer"><div className="drawer-header"><h2>{title}</h2><button className="icon-button" onClick={close} aria-label="Close"><X size={18} /></button></div><div className="drawer-content">{children}</div></aside></> }
function Loading() { return <div className="state-panel"><div className="loading-spinner" /><strong>Loading supply intelligence</strong><span>Retrieving current analysis from the API</span></div> }
function Error({ message }) { return <div className="state-panel error-state"><AlertTriangle size={20} /><strong>Unable to load this view</strong><span>{message}</span><small>Confirm the FastAPI service is running at localhost:8000.</small></div> }

export default function App() { return <BrowserRouter><Shell /></BrowserRouter> }
