import { useEffect, useRef, useState } from 'react'
import {
  analyzeResume,
  compareResume,
  downloadUpdatedResume,
  fetchResumeAnalysis,
  rewriteResume,
} from '../../api/resumeAnalysis'
import { uploadResume } from '../../api/profile'
import Button from '../../components/Button/Button'
import Callout from '../../components/Callout/Callout'
import './ResumeAnalyzer.css'

const FORMAT_LABEL = {
  PDF: 'PDF',
  DOCX: 'Word document',
  TXT: 'text file',
}

const FILE_ACCEPT = '.pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain'

function scoreLabel(score) {
  if (score >= 80) return 'Strong'
  if (score >= 60) return 'Needs work'
  return 'Weak'
}

function groupScore(checklist, titles) {
  const items = (checklist || []).filter((item) => titles.includes(item.title))
  if (!items.length) return null
  const points = { good: 100, weak: 55, missing: 20 }
  const total = items.reduce((sum, item) => sum + (points[item.status] ?? 55), 0)
  return Math.round(total / items.length)
}

export default function ResumeAnalyzer() {
  const uploadRef = useRef(null)
  const compareRef = useRef(null)
  const [report, setReport] = useState(null)
  const [status, setStatus] = useState('loading')
  const [error, setError] = useState(null)

  useEffect(() => {
    const controller = new AbortController()
    fetchResumeAnalysis(controller.signal)
      .then((data) => {
        setReport(data)
        setStatus('ready')
      })
      .catch((cause) => {
        if (cause.name === 'AbortError') return
        setError(cause)
        setStatus('ready')
      })
    return () => controller.abort()
  }, [])

  const run = async (action, work) => {
    setStatus(action)
    setError(null)
    try {
      const next = await work()
      if (next) setReport(next)
      setStatus('ready')
    } catch (cause) {
      setError(cause)
      setStatus('ready')
    }
  }

  const onUpload = (event) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    run('uploading', async () => {
      await uploadResume(file)
      return fetchResumeAnalysis()
    })
  }

  const analysis = report?.analysis
  const comparison = report?.comparison
  const resume = report?.resume
  const fileLabel = FORMAT_LABEL[resume?.format] || 'PDF'
  const issues = (analysis?.checklist || []).filter((item) => item.status !== 'good')
  const bars = analysis ? [
    ['Content', groupScore(analysis.checklist, ['Summary', 'Experience', 'Projects'])],
    ['Keywords', groupScore(analysis.checklist, ['Skills', 'Keywords for applications'])],
    ['Structure', groupScore(analysis.checklist, ['Contact details', 'Education', 'Length and formatting'])],
  ].filter((item) => item[1] != null) : []
  const busy = status.endsWith('ing')

  return (
    <div className="analyzer-page">
      <header className="analyzer-heading">
        <div>
          <p>CAREER MATERIALS</p>
          <h1>Resume Analyzer</h1>
          <span>Upload a resume, review the score and issues, then apply the changes and download the same file type. Reviews use Gemini 3.8 Flash on the free tier.</span>
        </div>
      </header>

      {error ? (
        <Callout tone="error" title="Resume review failed">
          <p>{error.message}</p>
        </Callout>
      ) : null}

      <section className="analyzer-section">
        <div className="analyzer-section-heading">
          <div>
            <h2>{resume?.filename || 'No resume on file'}</h2>
            <p>
              {resume
                ? `${resume.format || 'File'} on your profile. Uploading here replaces the resume and details on Skills & Profile.`
                : 'Upload a PDF, Word, or text file. It is saved to Skills & Profile, including the parsed details.'}
            </p>
          </div>
          <div className="analyzer-actions">
            <Button variant="secondary" onClick={() => uploadRef.current?.click()} disabled={status === 'uploading'}>
              {status === 'uploading' ? 'Uploading…' : 'Upload resume'}
            </Button>
            <Button onClick={() => run('analyzing', analyzeResume)} disabled={!resume || status === 'analyzing'}>
              {status === 'analyzing' ? 'Analyzing…' : analysis ? 'Reanalyse' : 'Analyze'}
            </Button>
            <input ref={uploadRef} className="analyzer-file-input" type="file" accept={FILE_ACCEPT} onChange={onUpload} />
          </div>
        </div>

        {status === 'loading' ? <p className="analyzer-note">Loading your saved resume…</p> : null}
        {!analysis && status === 'ready' ? (
          <p className="analyzer-note">Analyze the resume uploaded here or on Skills & Profile. The score and issues appear after the review.</p>
        ) : null}

        {analysis ? (
          <div className="analyzer-report">
            {report.improvement ? (
              <p className="analyzer-improvement">
                {report.improvement.summary}
                {report.improvement.changes?.length ? ` ${report.improvement.changes.join(' ')}` : ''}
              </p>
            ) : null}
            <div className="analyzer-score" style={{ '--score': analysis.score }}>
              <div>
                <strong>{analysis.score}</strong>
                <span>{scoreLabel(analysis.score)}</span>
              </div>
            </div>
            <div className="analyzer-score-copy">
              <h3>{analysis.headline || 'Resume score'}</h3>
              <p>{analysis.skills?.length || 0} skills noticed. A score of 80 or higher is the target.</p>
              <div className="analyzer-bars">
                {bars.map(([label, value]) => (
                  <div key={label}>
                    <span>{label}</span>
                    <div><i style={{ width: `${value}%` }} /></div>
                    <strong>{value}</strong>
                  </div>
                ))}
              </div>
            </div>
          </div>
        ) : null}

        {issues.length ? (
          <>
            <h3>Issues to fix</h3>
            <ul className="analyzer-issues">
              {issues.map((item) => (
                <li key={item.title}>
                  <span className={`analyzer-status is-${item.status}`}>{item.status}</span>
                  <div><strong>{item.title}</strong><p>{item.detail}</p></div>
                </li>
              ))}
            </ul>
          </>
        ) : null}

        {analysis?.improvements?.length ? (
          <>
            <h3>Suggested changes</h3>
            <ul className="analyzer-pairs">
              {analysis.improvements.map((item) => (
                <li key={item.title}><strong>{item.title}</strong><p>{item.detail}</p></li>
              ))}
            </ul>
          </>
        ) : null}

        {analysis?.strengths?.length ? (
          <>
            <h3>What is already working</h3>
            <ul className="analyzer-points">
              {analysis.strengths.map((item) => <li key={item}>{item}</li>)}
            </ul>
          </>
        ) : null}

        {analysis ? (
          <div className="analyzer-apply">
            <Button variant="secondary" onClick={() => run('rewriting', rewriteResume)} disabled={busy}>
              {status === 'rewriting' ? 'Updating…' : 'Apply changes'}
            </Button>
            <Button onClick={() => run('downloading', downloadUpdatedResume)} disabled={!report?.rewriteReady || status === 'downloading'}>
              {status === 'downloading' ? 'Preparing…' : `Download ${fileLabel}`}
            </Button>
            <p>{report?.rewriteReady ? `Updated ${fileLabel} is ready.` : 'Apply the changes, then download the updated file.'}</p>
          </div>
        ) : null}
      </section>

      <section className="analyzer-section">
        <div className="analyzer-section-heading">
          <div>
            <h2>Compare with another resume</h2>
            <p>See the stronger approach in each resume. Useful points can be applied to yours, then downloaded. Facts that are not already yours are left out.</p>
          </div>
          <Button variant="secondary" onClick={() => compareRef.current?.click()} disabled={!resume || status === 'comparing'}>
            {status === 'comparing' ? 'Comparing…' : 'Upload resume to compare'}
          </Button>
          <input
            ref={compareRef}
            className="analyzer-file-input"
            type="file"
            accept={FILE_ACCEPT}
            onChange={(event) => {
              const file = event.target.files?.[0]
              event.target.value = ''
              if (file) run('comparing', () => compareResume(file))
            }}
          />
        </div>

        {comparison ? (
          <>
            <p className="analyzer-summary">{comparison.summary}</p>
            <div className="analyzer-columns">
              <div>
                <h3>Good in yours</h3>
                <ul className="analyzer-points">
                  {comparison.goodInYours?.map((item) => <li key={item}>{item}</li>)}
                </ul>
              </div>
              <div>
                <h3>Good in {comparison.otherName || 'the other resume'}</h3>
                <ul className="analyzer-points">
                  {comparison.goodInTheirs?.map((item) => <li key={item}>{item}</li>)}
                </ul>
              </div>
            </div>
            {comparison.improveYours?.length ? (
              <>
                <h3>Apply these to your resume</h3>
                <ul className="analyzer-pairs">
                  {comparison.improveYours.map((item) => (
                    <li key={item.title}><strong>{item.title}</strong><p>{item.detail}</p></li>
                  ))}
                </ul>
              </>
            ) : null}
            <div className="analyzer-apply">
              <Button variant="secondary" onClick={() => run('rewriting', rewriteResume)} disabled={busy}>
                {status === 'rewriting' ? 'Updating…' : 'Update resume with these points'}
              </Button>
              <Button onClick={() => run('downloading', downloadUpdatedResume)} disabled={!report?.rewriteReady || status === 'downloading'}>
                {status === 'downloading' ? 'Preparing…' : `Download ${fileLabel}`}
              </Button>
            </div>
          </>
        ) : (
          <p className="analyzer-note">The second file is used only for this comparison. It does not replace your profile resume.</p>
        )}
      </section>
    </div>
  )
}
