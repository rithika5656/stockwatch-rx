import { useState } from 'react'
import { Activity, AlertTriangle, Check, LoaderCircle, Sparkles, X } from 'lucide-react'
import { getErrorMessage, post } from '../services/api'

const stages = [
  'Analyzing demand signals',
  'Generating prototype forecasts',
  'Detecting shortage and expiry risk',
  'Scoring critical priorities',
  'Optimizing redistribution actions',
]

export default function JudgeAnalysisButton() {
  const [open, setOpen] = useState(false)
  const [running, setRunning] = useState(false)
  const [stage, setStage] = useState(0)
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')

  async function runAnalysis() {
    if (running) return
    setOpen(true)
    setRunning(true)
    setResult(null)
    setError('')
    setStage(0)
    const timer = window.setInterval(() => setStage((current) => Math.min(current + 1, stages.length - 1)), 450)
    try {
      const analysis = await post('/analysis/run')
      setResult(analysis)
      setStage(stages.length)
    } catch (requestError) {
      setError(getErrorMessage(requestError))
    } finally {
      window.clearInterval(timer)
      setRunning(false)
    }
  }

  return <>
    <button className="judge-run-button" onClick={runAnalysis} aria-haspopup="dialog">
      <Sparkles size={15} />Run Intelligence Analysis
    </button>
    {open && <div className="judge-analysis-scrim" role="presentation" onClick={() => !running && setOpen(false)}>
      <section className="judge-analysis-panel" role="dialog" aria-modal="true" aria-labelledby="judge-analysis-title" onClick={(event) => event.stopPropagation()}>
        <header><div className="judge-analysis-icon"><Activity size={18} /></div><div><span>JUDGE DEMO</span><h2 id="judge-analysis-title">Intelligence analysis</h2></div><button className="icon-button" onClick={() => setOpen(false)} aria-label="Close analysis" disabled={running}><X size={17} /></button></header>
        <div className="judge-analysis-steps">{stages.map((label, index) => <div className={`judge-analysis-step ${stage > index ? 'complete' : stage === index && running ? 'active' : ''}`} key={label}>{stage > index ? <Check size={15} /> : stage === index && running ? <LoaderCircle className="step-spinner" size={15} /> : <i />}{label}</div>)}</div>
        {error && <div className="judge-analysis-error"><AlertTriangle size={15} />{error}</div>}
        {result && <div className="judge-analysis-result"><strong>Analysis complete</strong><div><span>{result.forecasts} forecasts</span><span>{result.shortages} shortage risks</span><span>{result.expiry_risks} expiry risks</span><span>{result.recommendations} transfer actions</span></div></div>}
        <footer><span>Prototype decision support · synthetic data</span><button className="button button-primary" onClick={runAnalysis} disabled={running}>{running ? 'Analyzing...' : result ? 'Run again' : 'Start analysis'}</button></footer>
      </section>
    </div>}
  </>
}