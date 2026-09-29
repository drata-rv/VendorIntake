# Drata contract used by the bridge

Base URL `https://public-api.drata.com/public/v2`. Headers on every call: `Authorization: Bearer <key>`, `Accept: application/json`, `Content-Type: application/json`, `User-Agent: VendorIntakeBridge/1.0`. TLS verified, redirects off, retries off, timeout (5 s connect, 15 s read) capped by a 45 s per-operation budget, 1 request/s per process.

## Calls

| Purpose | Request | Expect | Scope |
|---|---|---|---|
| Identify account | `GET /company` | 200 `accountId,domain,name` | Get Company Settings |
| Probe list | `GET /vendors?size=1` | 200 `data[],pagination.cursor` | List Vendors |
| Probe read | `GET /vendors/{id}` | 200 object | Get Vendor |
| Full scan | `GET /vendors?size=500[&statuses[]=...][&cursor=...]` | 200 | List Vendors |
| Read-back | `GET /vendors/{id}[?expand[]=customFields]` | 200 | Get Vendor |
| Create | `POST /vendors` | 201 object with positive integer `id` | Create Vendor |
| Update | `PUT /vendors/{id}` | 200 object with same `id` | Update Vendor |
| Definitions | `GET /custom-field-definitions?entityType=VENDOR&size=500` | 200 | Get Custom Field Definitions |

## Field rules

- Create sends `contactEmail`; update sends `contactsEmail`; reads return `contactsEmail`. Canonical field `vendor_contact_emails`.
- `status: "PROSPECTIVE"` sent on every create. `status`, `confirmed`, `isSubProcessorActive` never sent on update.
- `cost` is integer cents as string (`Decimal`, never float).
- Custom fields write as `{"id": <customFieldId>, "value": ...}`; definitions expose `customFieldId`.
- Update uses `PUT` with changed fields only.
- Correlation marker `[VendorIntakeBridge:<installation-uuid>:<submission-uuid>]` appended to create `notes`.

## Verified against tenant (read-only, 2026-09-29)

- Auth, `GET /company`, `GET /vendors`, `GET /vendors/{id}`, `GET /custom-field-definitions` return 200 with documented shapes.
- Single vendor read carries `_links.self.href` (host `app2.drata.com` for the test tenant). List rows do not.
- Error envelope `{statusCode, message, code, debugInfo}`; `message` may be a string or an array of validation objects. Only `statusCode` and numeric `code` are used.
- `GET /vendors?expand[]=customFields` valid. Enum: `customFields, documents, integrations, lastQuestionnaire, latestSecurityReviews, reviews, vendorUser, vendorRelationshipContact, dataAccessedOrProcessed, scheduleConfiguration, customVendorType, inherentRiskLevel, residualRiskLevel`. Unset custom fields read back as `{customFieldId, name}` without `value`.
- Python `urllib` default User-Agent returned 403; `requests` default and explicit UA return 200. Adapter sets an explicit UA.
- List rows include `notes` in full, used for marker matching without per-vendor GET.

## Deviation from spec: scan strategy

Spec 3.3 scans with all ten `statuses[]` values. In the test tenant:

| Scan | Rows |
|---|---|
| Unfiltered | 878 (includes 143 with `status: null`) |
| `statuses[]` x10 | 735 (no `null` rows) |

Explicit-status scan alone misses vendors with null status. Bridge scans both and unions by vendor ID. Each scan is capped at 20 pages; cap, repeated cursor, or budget exhaustion raises `SCAN_INCOMPLETE` before any write.

## Error to state mapping

| Situation | State / reason |
|---|---|
| Preflight timeout, connect failure, `SCAN_INCOMPLETE`, 5xx on read | `RETRYABLE` |
| Recognized 400 on write | `NEEDS_CORRECTION` / `DRATA_VALIDATION` |
| Recognized 401/403 | `BLOCKED` / `DRATA_PERMISSION`, connection set `BLOCKED`, writes off |
| Recognized 402 | `BLOCKED` / `CUSTOM_FIELDS_UNAVAILABLE` |
| Recognized 412 | `BLOCKED` / `TERMS_NOT_ACCEPTED` |
| Recognized 429 | `RETRYABLE` / `RATE_LIMITED`, not-before persisted (`Retry-After` seconds or HTTP-date, default 60 s) |
| Write 5xx, 408, timeout, reset, unrecognized body, non-201 success | `UNKNOWN` |
| Write failed before connect (`ConnectTimeout`, `NewConnectionError`, TLS handshake, budget) | `RETRYABLE` / `NOT_SENT` |
| Update 404 | `NEEDS_REVIEW` / `TARGET_MISSING` |
| Read-back unavailable | `NEEDS_REVIEW` / `VERIFY_PENDING` |
| Read-back differs | `NEEDS_REVIEW` / `VERIFY_MISMATCH` (property names only stored) |

"Recognized" = JSON object with `statusCode` equal to HTTP status and integer `code`.

## Application routes

JSON errors: `{"error": {"code", "message", "fieldErrors", "submissionId"}}`. Create returns 201 `CREATED`; replay 200; unresolved 202; Drata validation rejection 422 `DRATA_REJECTED`; busy 503 `BRIDGE_BUSY` with `Retry-After: 5`; stale form 409 `FORM_CHANGED`; key reuse with different body 409 `IDEMPOTENCY_CONFLICT`; purged 410 `PAYLOAD_EXPIRED`.
