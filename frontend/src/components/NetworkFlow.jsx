import { useEffect, useState } from 'react'
import { ArrowRight, MapPin, Network, TriangleAlert } from 'lucide-react'
import { CircleMarker, MapContainer, Polyline, Popup, TileLayer } from 'react-leaflet'
import 'leaflet/dist/leaflet.css'
import { get, getErrorMessage } from '../services/api'

function markerTone(hospital) {
  if (hospital.critical_shortages > 0) return '#ff675d'
  if (hospital.expiry_units_at_risk > 0) return '#f0ac50'
  if (hospital.surplus_units > 0) return '#b6ff00'
  return '#8fe000'
}

async function fetchRoadRoute(source, destination) {
  const route = await get('/routes', { source_hospital_id: source.hospital_id, destination_hospital_id: destination.hospital_id })
  return {
    coordinates: route.coordinates,
    distanceKm: route.distance_km,
    durationMinutes: route.duration_minutes,
  }
}

export default function NetworkFlow({ refresh = 0 }) {
  const [state, setState] = useState({ data: null, loading: true, error: '' })
  const [selectedRoute, setSelectedRoute] = useState(null)
  const [routeDetails, setRouteDetails] = useState({ loading: false, error: '', coordinates: [], distanceKm: null, durationMinutes: null })

  useEffect(() => {
    let active = true
    get('/network').then((data) => {
      if (!active) return
      setState({ data, loading: false, error: '' })
      if (data?.edges?.length && !selectedRoute) {
        setSelectedRoute(data.edges[0].recommendation_id)
      }
    }).catch((error) => active && setState({ data: null, loading: false, error: getErrorMessage(error) }))
    return () => { active = false }
  }, [refresh])

  useEffect(() => {
    if (!state.data || !selectedRoute) {
      setRouteDetails({ loading: false, error: '', coordinates: [], distanceKm: null, durationMinutes: null })
      return
    }

    const selected = state.data.edges.find((edge) => edge.recommendation_id === selectedRoute)
    if (!selected) {
      setRouteDetails({ loading: false, error: '', coordinates: [], distanceKm: null, durationMinutes: null })
      return
    }

    const byId = Object.fromEntries(state.data.nodes.map((node) => [node.hospital_id, node]))
    const source = byId[selected.source_hospital_id]
    const destination = byId[selected.destination_hospital_id]
    if (!source || !destination) {
      setRouteDetails({ loading: false, error: '', coordinates: [], distanceKm: null, durationMinutes: null })
      return
    }

    let active = true
    setRouteDetails({ loading: true, error: '', coordinates: [], distanceKm: null, durationMinutes: null })
    fetchRoadRoute(source, destination)
      .then((result) => {
        if (!active) return
        setRouteDetails({ loading: false, error: '', coordinates: result.coordinates, distanceKm: result.distanceKm, durationMinutes: result.durationMinutes })
      })
      .catch(() => {
        if (!active) return
        setRouteDetails({ loading: false, error: 'OSRM road route unavailable. Road distance, ETA, and transfer feasibility are unverified.', coordinates: [], distanceKm: null, durationMinutes: null })
      })

    return () => { active = false }
  }, [selectedRoute, state.data])

  if (state.loading && !state.data) return <section className="panel network-panel"><div className="panel-heading"><div><span>NETWORK FLOW</span><h2>Surplus to need</h2></div></div><div className="network-loading">Loading facility positions and transfer routes...</div></section>
  if (state.error) return <section className="panel network-panel"><div className="panel-heading"><div><span>NETWORK FLOW</span><h2>Surplus to need</h2></div></div><div className="network-loading network-error"><TriangleAlert size={16} />{state.error}</div></section>

  const { nodes, edges } = state.data
  const byId = Object.fromEntries(nodes.map((node) => [node.hospital_id, node]))
  const surplusCount = nodes.filter((node) => node.surplus_units > 0).length
  const shortageCount = nodes.filter((node) => node.shortage_units > 0).length
  const expiryCount = nodes.filter((node) => node.expiry_units_at_risk > 0).length
  const selected = selectedRoute ? edges.find((edge) => edge.recommendation_id === selectedRoute) : null
  const selectedRoadPath = routeDetails.coordinates.length > 1 ? routeDetails.coordinates : []
  let routeHeadline = 'Select a marker or eligible route to inspect local redistribution details.'
  if (selected) {
    if (routeDetails.loading) {
      routeHeadline = 'Calculating real road route and ETA…'
    } else if (routeDetails.error) {
      routeHeadline = `${selected.source} → ${selected.destination}: ${selected.quantity.toLocaleString()} ${selected.supply} · road route temporarily unavailable; local estimate active.`
    } else if (routeDetails.distanceKm != null && routeDetails.durationMinutes != null) {
      routeHeadline = `${selected.source} → ${selected.destination}: ${selected.quantity.toLocaleString()} ${selected.supply} · ${routeDetails.distanceKm.toFixed(1)} km road route · ${routeDetails.durationMinutes} min ETA · transfer feasible.`
    } else {
      routeHeadline = `${selected.source} → ${selected.destination}: ${selected.quantity.toLocaleString()} ${selected.supply} · ${selected.distance_km} km · source safety stock maintained.`
    }
  }

  return <section className="panel network-panel">
    <div className="panel-heading"><div><span>NETWORK FLOW · REAL COORDINATES</span><h2>Surplus to need</h2></div><span className="network-summary">{nodes.length} facilities · {edges.length} feasible routes</span></div>
    <div className="network-layout">
      <div className="network-map-wrap">
        <div className="network-legend"><span><i className="surplus" />Surplus</span><span><i className="shortage" />Critical shortage</span><span><i className="expiry" />Expiry exposure</span></div>
        <div className="leaflet-map-shell"><MapContainer center={[11.056, 77.024]} zoom={11.8} scrollWheelZoom={false} className="network-leaflet-map">
            <TileLayer attribution="&copy; OpenStreetMap contributors" url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" />
            {edges.map((edge) => {
              const source = byId[edge.source_hospital_id]
              const destination = byId[edge.destination_hospital_id]
              if (!source || !destination) return null
              const active = selectedRoute === edge.recommendation_id
                      if (!active || selectedRoadPath.length < 2) return null
                      return <Polyline key={edge.recommendation_id} positions={selectedRoadPath} pathOptions={{ color: '#b6ff00', weight: 5, opacity: 0.95 }} eventHandlers={{ click: () => setSelectedRoute(edge.recommendation_id) }} />
            })}
            {nodes.map((node) => { const tone = markerTone(node); return <CircleMarker key={node.hospital_id} center={[node.latitude, node.longitude]} radius={node.critical_shortages ? 10 : 8} pathOptions={{ color: tone, fillColor: tone, fillOpacity: .86, weight: 2 }}><Popup><div className="map-popup"><strong>{node.display_name || node.name}</strong><span>{node.address}</span><b>{node.critical_shortages ? 'CRITICAL SHORTAGE' : node.roles.join(' · ')}</b><small>{node.shortage_units.toLocaleString()} projected need · {node.surplus_units.toLocaleString()} shareable</small></div></Popup></CircleMarker> })}
          </MapContainer></div>
        <div className="network-counts"><span><i className="surplus" />{surplusCount} surplus nodes</span><span><i className="shortage" />{shortageCount} shortage nodes</span><span><i className="expiry" />{expiryCount} expiry-risk nodes</span></div>
      </div>
      <div className="network-routes"><div className="network-routes-title"><MapPin size={15} /><strong>Opt-in supply matches</strong></div>{edges.slice(0, 5).map((edge) => <article className={`network-route ${selectedRoute === edge.recommendation_id ? 'selected-route' : ''}`} key={edge.recommendation_id} onClick={() => setSelectedRoute(edge.recommendation_id)}><div className="network-route-top"><span>{edge.supply}</span><b>{edge.priority_score}/100</b></div><div className="network-route-flow"><div><strong>{edge.source_hospital_id}</strong><small>{edge.source}</small></div><ArrowRight size={15} /><div><strong>{edge.destination_hospital_id}</strong><small>{edge.destination}</small></div></div><div className="network-route-bottom"><span>{edge.quantity.toLocaleString()} shareable units</span><span>{selectedRoute === edge.recommendation_id ? routeDetails.loading ? 'Checking OSRM...' : routeDetails.error ? 'Route unverified' : `${routeDetails.distanceKm?.toFixed(1)} km · ${routeDetails.durationMinutes} min` : 'Select to check route'}</span></div></article>)}</div>
    </div>
    <div className="network-footnote"><Network size={14} /><span>{routeHeadline}</span></div>
  </section>
}