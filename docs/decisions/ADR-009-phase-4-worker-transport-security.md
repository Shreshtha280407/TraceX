# ADR-009: Secure and bounded worker control plane

## Decision

Extend the existing worker credential, claim-token, and lease mechanism for
LAN workers. Secure transport is configurable, forwarded protocol headers are
trusted only from configured proxy IPs, and request limits apply before
expensive processing. Lease renewal is the durable heartbeat record.

## Consequences

Local HTTP remains an explicit development exception. TLS termination,
certificate handling, and reverse-proxy configuration are operator
responsibilities. Retry/recovery preserves Nipun's idempotent checkpoint
semantics and never gives workers datastore credentials.
