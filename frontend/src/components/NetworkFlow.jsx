import { useEffect, useState } from 'react'
import { ArrowRight, MapPin, Network, TriangleAlert } from 'lucide-react'
import { get, getErrorMessage } from '../services/api'

function pointFor(hospital) {
  const x = 30 + ((hospital.longitude - 68) / 29) * 590
  const y = 284 - ((hospital.latitude - 7) / 31) * 256
  return { x, y }
}

function pointColor(hospital) {
  if (hospital.critical_shortages > 0) return '#ff675d'
  if (hospital.expiry_units_at_risk > 0) return '#f0ac50'
  if (hospital.surplus_units > 0) return '#b6ff00'
  return '#8fe000'
}

export default function NetworkFlow({ refresh = 0 }) {
  const [state, setState] = useState({ data: null, loading: true, error: '' })
  useEffect(() => {
    let active = true
    get('/network').then((data) => active && setState({ data, loading: false, error: '' }))
      .catch((error) => active && setState({ data: null, loading: false, error: getErrorMessage(error) }))
    return () => { active = false }
  }, [refresh])

  if (state.loading && !state.data) return <section className="panel network-panel"><div className="panel-heading"><div><span>NETWORK FLOW</span><h2>Surplus to need</h2></div></div><div className="network-loading">Loading facility positions and transfer routes...</div></section>
  if (state.error) return <section className="panel network-panel"><div className="panel-heading"><div><span>NETWORK FLOW</span><h2>Surplus to need</h2></div></div><div className="network-loading network-error"><TriangleAlert size={16} />{state.error}</div></section>

  const { nodes, edges } = state.data
  const byId = Object.fromEntries(nodes.map((node) => [node.hospital_id, node]))
  const surplusCount = nodes.filter((node) => node.surplus_units > 0).length
  const shortageCount = nodes.filter((node) => node.shortage_units > 0).length
  const expiryCount = nodes.filter((node) => node.expiry_units_at_risk > 0).length

  return <section className="panel network-panel">
    <div className="panel-heading"><div><span>NETWORK FLOW · REAL COORDINATES</span><h2>Surplus to need</h2></div><span className="network-summary">{nodes.length} facilities · {edges.length} feasible routes</span></div>
    <div className="network-layout">
      <div className="network-map-wrap">
        <div className="network-legend"><span><i className="surplus" />Surplus</span><span><i className="shortage" />Critical shortage</span><span><i className="expiry" />Expiry exposure</span></div>
        <svg className="network-map" viewBox="0 0 650 320" role="img" aria-label="Hospital coordinates and recommended medical supply transfer routes">
          <rect x="24" y="16" width="604" height="278" rx="4" fill="#f7faf8" stroke="#e7eeea" />
          {[100, 200, 300, 400, 500].map((x) => <line key={`x${x}`} x1={x} y1="17" x2={x} y2="294" stroke="#e8eeea" strokeDasharray="3 6" />)}
          {[70, 135, 200, 265].map((y) => <line key={`y${y}`} x1="25" y1={y} x2="628" y2={y} stroke="#e8eeea" strokeDasharray="3 6" />)}
          {edges.slice(0, 14).map((edge) => {
            const source = byId[edge.source_hospital_id]
            const destination = byId[edge.destination_hospital_id]
            if (!source || !destination) return null
            const start = pointFor(source)
            const end = pointFor(destination)
            return <g className="network-edge" key={edge.recommendation_id}><line x1={start.x} y1={start.y} x2={end.x} y2={end.y} stroke="#b6ff00" strokeWidth="1.4" strokeOpacity=".38" /><circle cx={(start.x + end.x) / 2} cy={(start.y + end.y) / 2} r="2.5" fill="#8fe000" /></g>
          })}
          {nodes.map((node) => {
            const point = pointFor(node)
            return <g key={node.hospital_id}><title>{`${node.name}: ${node.surplus_units.toLocaleString()} surplus, ${node.shortage_units.toLocaleString()} projected need`}</title><circle cx={point.x} cy={point.y} r={node.critical_shortages ? 8 : 6} fill={pointColor(node)} fillOpacity=".92" stroke="white" strokeWidth="2" /><text x={point.x + 8} y={point.y - 8} fill="#52675d" fontSize="8" fontWeight="600">{node.hospital_id}</text></g>
          })}
          <text x="31" y="310" fill="#819087" fontSize="8">68°E</text><text x="588" y="310" fill="#819087" fontSize="8">97°E</text><text x="8" y="25" fill="#819087" fontSize="8">38°N</text><text x="9" y="289" fill="#819087" fontSize="8">7°N</text>
        </svg>
        <div className="network-counts"><span><i className="surplus" />{surplusCount} surplus nodes</span><span><i className="shortage" />{shortageCount} shortage nodes</span><span><i className="expiry" />{expiryCount} expiry-risk nodes</span></div>
      </div>
      <div className="network-routes"><div className="network-routes-title"><MapPin size={15} /><strong>Priority transfer routes</strong></div>{edges.slice(0, 5).map((edge) => <article className="network-route" key={edge.recommendation_id}><div className="network-route-top"><span>{edge.supply}</span><b>{edge.priority_score}/100</b></div><div className="network-route-flow"><div><strong>{edge.source_hospital_id}</strong><small>{edge.source}</small></div><ArrowRight size={15} /><div><strong>{edge.destination_hospital_id}</strong><small>{edge.destination}</small></div></div><div className="network-route-bottom"><span>{edge.quantity.toLocaleString()} units</span><span>{edge.transport_hours}h transport</span></div></article>)}</div>
    </div>
    <div className="network-footnote"><Network size={14} /><span>Lines show calculated feasible transfers; node color prioritizes critical shortage, then expiry, then available surplus.</span></div>
  </section>
}