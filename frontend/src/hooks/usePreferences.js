import { useCallback, useEffect, useState } from 'react'
import { fetchPreferences, savePreferences } from '../api/preferences'

export const DEFAULT_PAUSED_PORTALS = ['instahyre', 'naukri']

export const DEFAULT_PREFERENCES = {
  experience: null,
  posted: 'all',
  roles: [],
  pausedPortals: DEFAULT_PAUSED_PORTALS,
  auto_analyse_resume: true,
  geminiApiKey: '',
  updatedAt: '',
}

export default function usePreferences() {
  const [preferences, setPreferences] = useState(DEFAULT_PREFERENCES)
  const [status, setStatus] = useState('loading')
  const [error, setError] = useState(null)

  useEffect(() => {
    const controller = new AbortController()
    fetchPreferences(controller.signal)
      .then((result) => {
        setPreferences({ ...DEFAULT_PREFERENCES, ...result })
        setStatus('idle')
      })
      .catch((cause) => {
        if (cause.name === 'AbortError') return
        setError(cause)
        setStatus('error')
      })
    return () => controller.abort()
  }, [])

  const save = useCallback(async (next) => {
    setStatus('saving')
    setError(null)
    try {
      const result = await savePreferences(next)
      setPreferences((current) => ({
        ...DEFAULT_PREFERENCES,
        ...current,
        ...result,
        apiKey: result.apiKey || result.geminiApiKey || current.apiKey || '',
        pausedPortals: result.pausedPortals ?? current.pausedPortals,
      }))
      setStatus('saved')
      return true
    } catch (cause) {
      setError(cause)
      setStatus('error')
      return false
    }
  }, [])

  return { preferences, status, error, save }
}