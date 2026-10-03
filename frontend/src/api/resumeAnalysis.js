import { auth } from '../firebase'
import { ApiError, BASE_URL, getJson, postJson } from './client'

export function fetchResumeAnalysis(signal) {
  return getJson('/api/resume/analysis', { signal })
}

export function analyzeResume() {
  return postJson('/api/resume/analyze', { force: true })
}

export function compareResume(file) {
  return fileAsBase64(file).then((content) => postJson('/api/resume/compare', {
    filename: file.name,
    content,
  }))
}

export function rewriteResume() {
  return postJson('/api/resume/rewrite', {})
}

function fileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(reader.error || new Error('Could not read the resume'))
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1] || '')
    reader.readAsDataURL(file)
  })
}

export async function downloadUpdatedResume() {
  const token = auth.currentUser ? await auth.currentUser.getIdToken() : null
  let response
  try {
    response = await fetch(`${BASE_URL}/api/resume/download`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
  } catch {
    throw new ApiError('Could not download the updated resume.', 0)
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    throw new ApiError(payload?.error || 'Could not download the updated resume.', response.status)
  }
  const blob = await response.blob()
  const header = response.headers.get('Content-Disposition') || ''
  const match = /filename="([^"]+)"/.exec(header)
  const filename = match?.[1] || 'updated-resume'
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}
