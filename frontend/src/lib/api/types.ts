/**
 * Types mirror the real backend response/request shapes in
 * app/modules/access_control/models.py exactly -- field names, optionality,
 * and enum values are copied from there, never invented. See
 * docs/architecture/access-control-v1.md and ADR-033.
 */

export type SystemRole = 'admin'

export type CaseRole = 'case_owner' | 'case_manager' | 'investigator' | 'analyst' | 'reviewer' | 'viewer'

export type ClearanceLevel = 'restricted' | 'confidential' | 'secret'

export interface PublicUser {
  user_id: string
  email_normalized: string
  display_name: string
  is_active: boolean
  created_at: string
  system_role: SystemRole | null
  must_change_password: boolean
  totp_enabled: boolean
}

export interface CaseMembershipView {
  case_id: string
  role: CaseRole
  clearance: ClearanceLevel
  is_active: boolean
}

export interface MeResponse {
  user: PublicUser
  case_memberships: CaseMembershipView[]
}

export interface CaseView {
  case_id: string
  case_reference: string
  classification: ClearanceLevel
  status: 'open' | 'closed' | 'archived'
  created_at: string
}

/**
 * `POST /auth/login`'s response. Two disjoint valid shapes in one type --
 * see `TokenPairResponse`'s own docstring in models.py. Narrow on
 * `mfa_required` before reading the other fields.
 */
export interface TokenPairResponse {
  access_token: string | null
  refresh_token: string | null
  token_type: string
  expires_in: number | null
  mfa_required: boolean
  mfa_token: string | null
}

export interface MfaEnrollResponse {
  secret: string
  provisioning_uri: string
}

export interface AdminResetCredentialsResponse {
  temporary_password: string
}

export interface AdminProvisionUserRequest {
  email: string
  password: string
  display_name: string
  system_role?: SystemRole | null
}

export interface PublicUserListResponse {
  items: PublicUser[]
}

/** The safe, structured error envelope every non-2xx response body carries. */
export interface ApiErrorBody {
  error: {
    code: string
    message: string
    request_id: string
  }
  errors?: Array<{ loc: Array<string | number>; type: string; msg: string }>
}
