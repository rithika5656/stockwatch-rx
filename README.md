# StockWatch-RX

**Prototype Decision Support for Medical Supply Intelligence**  
*Know Before the Shortage.*

StockWatch-RX is a decision-support prototype for anticipating medical supply shortages, highlighting expiry exposure, ranking critical demand, and recommending explainable inter-hospital transfers. It uses synthetic Indian hospital and supply data only; it is not a clinical or procurement authority.

## Problem and solution

Hospitals can hold excess stock while nearby facilities approach a stock-out, and batches may expire before consumption. StockWatch-RX joins 90 days of synthetic demand history with inventory batches, estimates near-term demand, detects risk, and proposes transfers only when an opt-in source remains above its protected reserve.

## Architecture

- `backend/app/demo_data.py`: reproducible synthetic operational data for three public Coimbatore hospital locations.
- `backend/app/engine.py`: historical-demand prototype forecast, scheduled-surgery demand adjustment, daily depletion simulation, FEFO expiry exposure, explainable priorities, and opt-in transfer optimisation.
- `backend/app/main.py`: FastAPI routes and rule-based assistant backed by analysis functions.
- `backend/app/fulfillment.py`: parent supply requests, dynamically re-matched source legs, FEFO allocations, commitments, dispatch, delivery, rejection, and failure recovery.
- `backend/app/database.py`: MongoDB source records plus durable request/leg records, conditional share-pool and batch commitments, derived analysis persistence, and demo fallback.
- `backend/app/routing.py`: OSRM road geometry, distance, and ETA lookup with explicit unavailable-route handling.
- `frontend/src/App.jsx`: responsive React dashboard and routed operational pages.
- `frontend/src/services/api.js`: Axios client for the FastAPI service.
- `frontend/src/components/NetworkFlow.jsx`: Leaflet/OpenStreetMap facility map with routes from the shared OSRM endpoint.
- `frontend/src/components/JudgeAnalysisButton.jsx`: judge-facing analysis command and progress dialog.

The forecast is labelled **Prototype Forecast** and uses a weighted moving average with recent trend, patient load, emergency cases, seasonality, and outbreak-signal adjustments. Configured surgery-type consumption is added by date and case count before daily stock depletion and risk are calculated. These are heuristics over synthetic data, not validated medical predictions.

## Features

- Three publicly mapped facilities: KMCH, PSG Hospitals, and Kumaran Medical Center. Coordinates and addresses are configured once in the backend seed data.
- 13 supplies, 78 inventory batches, and 3,510 synthetic daily demand observations.
- Six demo scenarios: normal operations, outbreak surge, critical shortage, expiry crisis, redistribution opportunity, and a KMCH-to-PSG transfer demonstration.
- Demand forecasts, projected days-to-stock-out, and shortage risk.
- Batch-level expected expiry waste and recommended rotation.
- Surgery schedule create/edit/cancel, non-persistent what-if forecast simulation, and schedule-linked demand/risk explanations.
- Hospital-controlled shareable pools. Nearby hospitals see only enabled supply and quantity; source inventory, safety reserves, and unshared supplies are private.
- Non-persistent transfer what-if simulation for sharing on/off, shareable quantity, and ETA delay; the result is not written to hospital settings.
- FEFO lot allocation and expiry-aware transfer feasibility. Transfer amounts are bounded by shareable quantity, source surplus after reserve, destination need, eligible source lots, and supply-specific transfer caps.
- Dynamic Multi-Source Fulfilment: one parent request tracks the total need while multiple source legs commit partial amounts; accepting, rejecting, or failing a leg automatically recalculates and re-matches remaining demand.
- COMMITTED share-pool and batch quantities are reserved with conditional MongoDB updates, preventing the same opted-in stock or FEFO lots from being committed twice. Dispatch consumes the reserved batches and reduces the source pool; failed pre-dispatch legs release their reservations.
- Per-leg OSRM route, distance, ETA, status, and FEFO batch traceability, shown in fulfillment progress and the existing Leaflet network map.
- OSRM road geometry/distance/ETA; no-route or provider failure leaves transfer feasibility unverified and does not draw a fabricated road path.
- Weekly management report with calculated shortages, surgeries, transfers, expiry risks, management actions, CSV export, and browser print-to-PDF.
- Configurable priority and redistribution score weights with returned component values and penalties.
- Coordinate-based network view with calculated transfer edges.
- Global **Run Intelligence Analysis** command that runs the backend pipeline and reports its results.
- Transparent priority ranking and a data-grounded rule-based MediSupply Copilot.
- Inventory search and risk filtering, facility/supply tables, transfer approval/rejection demo state, and CSV export.

## Requirements

- Python 3.10+ (64-bit recommended; see the environment note below).
- Node.js 20.19+ or 22.12+ and npm.
- MongoDB 7+ only if using persistence. The app works in demo mode without MongoDB.

The current forecast/optimisation core intentionally uses standard Python math rather than a binary ML dependency. The documented method is replaceable behind the analysis engine.

## Environment

Copy `backend/.env.example` to `backend/.env` when configuring the backend. Copy `frontend/.env.example` to `frontend/.env` only if the API is not at the default URL.

Backend variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `MONGODB_URI` | empty | MongoDB connection string. |
| `DATABASE_NAME` | `medisupplyiq` | Database name. |
| `DEMO_MODE` | `true` | Use synthetic in-memory data if MongoDB is absent/unavailable. Set `false` to require MongoDB. |
| `LLM_API_KEY` | empty | Reserved for a future provider adapter; never placed in frontend code. |
| `LLM_PROVIDER` | empty | Reserved provider selection. |
| `RISK_CRITICAL_DAYS` | `2` | Maximum days to safety-stock breach for CRITICAL risk. |
| `RISK_HIGH_DAYS` | `5` | Maximum days for HIGH risk. |
| `RISK_MEDIUM_DAYS` | `10` | Maximum days for MEDIUM risk. |
| `OSRM_ROUTING_URL` | `https://router.project-osrm.org` | OSRM-compatible routing service base URL. |
| `LOCAL_REDISTRIBUTION_RADIUS_KM` | `15` | Proximity prefilter for local transfer candidates; final distance and ETA use OSRM. |

Frontend variable: `VITE_API_BASE_URL` defaults to `http://localhost:8000/api`.

## Run locally

### Backend (Windows PowerShell)

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

Swagger UI is available at `http://localhost:8000/docs`.

### Frontend

```powershell
cd frontend
npm install
npm run dev
```

Open the URL printed by Vite, usually `http://localhost:5173`.

## MongoDB setup

Start a local MongoDB service or use an Atlas connection string. Set `MONGODB_URI` in `backend/.env` and keep `DEMO_MODE=true` for a safe fallback if the database is temporarily unavailable. On first successful connection, the backend seeds `hospitals`, `supplies`, `inventory`, and `demand_history` with outbreak demo records. Scenario changes update those source collections. Startup and **Run Intelligence Analysis** refresh `forecast_results`, `shortage_risks`, `expiry_risks`, `redistribution_recommendations`, and `alerts`. `DEMO_MODE=false` makes a missing/unavailable MongoDB connection a clear startup error.

Seed/reset source records explicitly from `backend` with:

```powershell
python -m scripts.seed_database
```

## Demo scenarios and story

The default is **Outbreak Surge**, so the judge lands on the core risk story:

1. Compare Normal Operations to Outbreak Surge. H003 Normal Saline crosses safety stock in about 4 days normally and about 2 days during the outbreak; physical depletion is shown separately.
2. Login as H001 (KMCH), add a future surgery schedule, and use **Simulate forecast** to compare the historical/model baseline with surgery-adjusted demand, risk, and safety-stock timing.
3. Login as H002 (PSG Hospitals) and manage the Normal Saline shareable pool. H001 sees only the enabled quantity, never H002's inventory or reserve.
4. Review Nearby Hospitals or Supply Map. A route is feasible only after OSRM returns geometry and its ETA meets the predicted safety-stock deadline.
5. Select **KMCH to PSG transfer demo** to inspect a synthetic, safety-checked 578-unit KMCH → PSG Normal Saline transfer; its route origin and destination follow those same hospital IDs.
6. Open Weekly Management Report to review dynamically calculated actions and export CSV or print to PDF.
7. Click **Run Intelligence Analysis** to recalculate forecasts, shortage, expiry, and transfer candidates. Transfer approval state remains in memory for this process.

## API

All application routes are under `/api` and return `{ "data": ..., "meta": ... }` envelopes.

- `GET /health`
- `GET /dashboard/summary`
- `GET /network`
- `GET /hospitals`, `GET /hospitals/{hospital_id}`
- `GET /supplies`
- `GET /inventory` (supports hospital, supply, risk, search, page, and page_size filters)
- `GET /forecast` and `POST /forecast/run`
- `POST /forecast/simulate`
- `GET /surgery-types`, `GET/POST /surgeries`, `PUT/DELETE /surgeries/{surgery_id}`
- `GET/POST /shareable-pool`
- `GET /nearby-supplies`, `POST /nearby-supplies/simulate`, `GET /routes`
- `GET /management-report/weekly`
- `GET /shortages`
- `GET /expiry-risks`
- `GET /redistribution`, `GET /redistribution/{recommendation_id}`
- `POST /redistribution/{recommendation_id}/approve`, `POST /redistribution/{recommendation_id}/reject`
- `GET/POST /supply-requests`, `DELETE /supply-requests/{request_id}`
- `POST /supply-requests/{request_id}/legs/{leg_id}/accept`, `/reject`, `/status`, `/fail`
- `GET /prioritisation`
- `GET /alerts`
- `POST /assistant/query`
- `POST /analysis/run`
- `GET /demo/scenario`, `POST /demo/scenario`

## Limitations and next steps

- Hospital identities/locations are based on OpenStreetMap public data (OpenStreetMap contributors, ODbL). Inventory, demand, surgeries, patient load, stock-out, expiry, and transfer data are synthetic and are not supplied by those hospitals.
- Forecast probabilities and scores are demonstrative heuristics, not validated predictive or clinical models.
- Surgery schedules and pool preferences use MongoDB source collections when MongoDB is enabled; fulfillment requests, legs, and stock commitments use dedicated MongoDB collections in that mode. In demo-only mode, workflow state is process memory and resets on restart. Dispatch/delivery actions are simulated workflow updates and are not connected to external logistics.
- The public OSRM demo endpoint has availability and usage limits. Configure `OSRM_ROUTING_URL` for a production routing service; if routing is unavailable, the app marks feasibility unverified rather than substituting straight-line distance.
- MongoDB scenario/derived persistence code is implemented, but no live MongoDB URI or service was available for connection testing in this workspace.
- A production deployment should add authentication/authorization, audit events, durable workflow state, transport/vendor constraints, input validation against operational feeds, model evaluation, and monitoring with domain experts.
- The current machine's selected Python is 32-bit Python 3.13.14; a 64-bit Python installation is recommended for normal local dependency support.
