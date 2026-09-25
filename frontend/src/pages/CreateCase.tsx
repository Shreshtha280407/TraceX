import { useState } from 'react'
import type { FormEvent } from 'react'
import { AlertCircle, FileStack } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { Button } from '../components/Button'
import { Card } from '../components/Card'
import { ApiError, authApi, casesApi } from '../lib/api/client'
import type { ClearanceLevel } from '../lib/api/types'
import { useAuthStore } from '../lib/auth/store'

const CLASSIFICATIONS: { value: ClearanceLevel; label: string; description: string }[] = [
  { value: 'restricted', label: 'Restricted', description: 'Lowest clearance requirement.' },
  { value: 'confidential', label: 'Confidential', description: 'Standard case-sensitive material.' },
  { value: 'secret', label: 'Secret', description: 'Highest clearance requirement.' },
]

/**
 * Page 4 -- Create Case. Real form matching the backend's actual
 * `CaseCreateRequest` exactly: `case_reference` + `classification` are the
 * only fields the API accepts (Section 8) -- there is no "access policy"
 * field on creation itself; case-scoped access is granted afterward, one
 * member at a time, via `POST /cases/{id}/members` (Case Management/
 * Workspace's job in a later phase, not this form's). The creator becomes
 * the case's `case_owner` automatically (real backend behavior).
 */
export function CreateCase() {
  const navigate = useNavigate()
  const setIdentity = useAuthStore((state) => state.setIdentity)
  const [caseReference, setCaseReference] = useState('')
  const [classification, setClassification] = useState<ClearanceLevel>('confidential')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      const created = await casesApi.create({ case_reference: caseReference, classification })
      // Refresh identity so the new case_owner membership (created
      // server-side alongside the case) is picked up immediately --
      // otherwise it would only appear after the next full login.
      const me = await authApi.me()
      setIdentity(me.user, me.case_memberships)
      navigate('/cases', { state: { justCreatedCaseId: created.case_id } })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to reach the server.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold text-text">Create Case</h1>
        <p className="text-sm text-text-dim">You become this case's owner immediately on creation.</p>
      </div>

      <Card className="max-w-lg p-6">
        {error ? (
          <div className="mb-4 flex items-start gap-2 rounded-control border border-crimson/30 bg-crimson/10 px-3 py-2 text-xs text-crimson">
            <AlertCircle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
            <span>{error}</span>
          </div>
        ) : null}

        <form className="flex flex-col gap-4" onSubmit={handleSubmit}>
          <label className="flex flex-col gap-1.5 text-sm">
            <span className="font-medium text-text-dim">Case reference</span>
            <input
              type="text"
              required
              minLength={1}
              maxLength={200}
              value={caseReference}
              onChange={(event) => setCaseReference(event.target.value)}
              placeholder="OPERATION-NIGHTFALL"
              className="rounded-control border border-card-border bg-card px-3 py-2 font-mono text-sm text-text focus:outline-none focus:ring-2 focus:ring-crimson/40"
            />
            <span className="text-xs text-text-faint">
              Must be unique across every case in the system.
            </span>
          </label>

          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-sm font-medium text-text-dim">Classification</legend>
            {CLASSIFICATIONS.map((option) => (
              <label
                key={option.value}
                className="flex cursor-pointer items-start gap-3 rounded-control border border-card-border bg-card px-3 py-2 has-[:checked]:border-crimson has-[:checked]:bg-crimson/5"
              >
                <input
                  type="radio"
                  name="classification"
                  value={option.value}
                  checked={classification === option.value}
                  onChange={() => setClassification(option.value)}
                  className="mt-0.5"
                />
                <span className="flex flex-col">
                  <span className="text-sm font-medium text-text">{option.label}</span>
                  <span className="text-xs text-text-dim">{option.description}</span>
                </span>
              </label>
            ))}
          </fieldset>

          <Button type="submit" disabled={submitting || !caseReference.trim()} className="mt-2 w-fit">
            <FileStack size={16} aria-hidden="true" />
            {submitting ? 'Creating...' : 'Create case'}
          </Button>
        </form>
      </Card>
    </div>
  )
}
