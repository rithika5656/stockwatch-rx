# StockWatch-RX

**AI for Medical Supply Intelligence**  
*Know Before the Shortage.*

StockWatch-RX is a decision-support prototype for anticipating medical supply shortages, highlighting expiry exposure, ranking critical demand, and recommending explainable inter-hospital transfers. It uses synthetic Indian hospital and supply data only; it is not a clinical or procurement authority.

## Problem and solution

Hospitals can hold excess stock while nearby facilities approach a stock-out, and batches may expire before consumption. StockWatch-RX joins 90 days of synthetic demand history with inventory batches, estimates near-term demand, detects risk, and proposes transfers only when a source remains above its safety stock and a seven-day reserve.

## Architecture

- `backend/app/demo_data.py`: reproducible synthetic network and scenario generation.
- `backend/app/engine.py`: feature-adjusted prototype forecast, daily depletion simulation, FEFO expiry exposure, explainable priorities, and transfer optimisation.
- `backend/app/main.py`: FastAPI routes and rule-based assistant backed by analysis functions.
- `backend/app/database.py`: MongoDB connection, scenario source updates, derived analysis persistence, and demo fallback.
- `frontend/src/App.jsx`: responsive React dashboard and routed operational pages.
- `frontend/src/services/api.js`: Axios client for the FastAPI service.
- `frontend/src/components/NetworkFlow.jsx`: coordinate-based facility and transfer network visualization.
- `frontend/src/components/JudgeAnalysisButton.jsx`: judge-facing analysis command and progress dialog.

The forecast is labelled **Prototype Forecast** and uses a weighted moving average with recent trend, patient load, emergency cases, seasonality, and outbreak-signal adjustments. Daily stock depletion is simulated to both the safety-stock threshold and physical zero. This is a heuristic over synthetic data, not a validated medical prediction.

## Features

- 20 hospitals, 24 supplies, 960 batches, and 43,200 daily demand observations.
- Five demo scenarios: normal operations, outbreak surge, critical shortage, expiry crisis, and redistribution opportunity.
- Demand forecasts, projected days-to-stock-out, and shortage risk.
- Batch-level expected expiry waste and recommended rotation.
- FEFO lot allocation and expiry-aware transfer feasibility. Transfer amounts are computed from destination need through supplier lead time, eligible source lots, safety/reserve limits, and supply-specific transfer caps. Saline actions cap at 750 units; quantities are not fixed.
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
2. Review the recommended H001 (Coimbatore Central Hospital) to H003 (Madurai Emergency Medical Center) saline transfer. Its amount responds to lead-time demand, stays within source safety stock, and selects an eligible FEFO batch.
3. Inspect the score factors, source remaining balance, transport time, and destination coverage change.
4. Ask MediSupply Copilot why the transfer is recommended, for the top five routes, or what a 20% demand increase would do.
5. Click **Run Intelligence Analysis** to run backend analysis and display completed forecast, shortage, expiry, and transfer counts.
6. Switch to Expiry Crisis to surface near-term batch exposure. Approve/reject a transfer to demonstrate process state; the action is held in backend memory for this process.

## API

All application routes are under `/api` and return `{ "data": ..., "meta": ... }` envelopes.

- `GET /health`
- `GET /dashboard/summary`
- `GET /network`
- `GET /hospitals`, `GET /hospitals/{hospital_id}`
- `GET /supplies`
- `GET /inventory` (supports hospital, supply, risk, search, page, and page_size filters)
- `GET /forecast` and `POST /forecast/run`
- `GET /shortages`
- `GET /expiry-risks`
- `GET /redistribution`, `GET /redistribution/{recommendation_id}`
- `POST /redistribution/{recommendation_id}/approve`, `POST /redistribution/{recommendation_id}/reject`
- `GET /prioritisation`
- `GET /alerts`
- `POST /assistant/query`
- `POST /analysis/run`
- `GET /demo/scenario`, `POST /demo/scenario`

## Limitations and next steps

- All demand, hospital, and inventory data are synthetic. No patient data or external hospital APIs are used.
- Forecast probabilities and scores are demonstrative heuristics, not validated predictive or clinical models.
- Transfer approval/rejection state is in-memory and resets on backend restart; transfer execution is not connected to logistics.
- MongoDB scenario/derived persistence code is implemented, but no live MongoDB URI or service was available for connection testing in this workspace.
- A production deployment should add authentication/authorization, audit events, durable workflow state, transport/vendor constraints, input validation against operational feeds, model evaluation, and monitoring with domain experts.
- The current machine's selected Python is 32-bit Python 3.13.14; a 64-bit Python installation is recommended for normal local dependency support.
