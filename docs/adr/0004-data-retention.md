# 0004. Data retention (Q9)

- Status: Accepted
- Date: 2026-10-06

## Decision

| Data | Retention | Configured by | Fallback |
|---|---|---|---|
| Conversations: sessions, messages, generated DAX, query metadata | Deleted after N hours **without activity** | `CONVERSATION_RETENTION_HOURS` | **12** |
| Security audit events | Deleted after N hours | `AUDIT_RETENTION_HOURS` | **2160** (90 days) |
| Authorization cache rows | Deleted once expired | (Phase 5 TTLs) | — |
| Users | Deleted when they have no conversations and haven't been seen within either window | derived | — |

- Missing, non-numeric or non-positive values fall back to the defaults, with a warning in the log.
- **Raw query result rows are never stored.** Query executions keep the DAX, row count, column names, duration and
  status. Answers shown to the user (which contain the figures) are stored as messages.
- Audit events hold who / which model / decision / reason only. A key allow-list blocks question text and data values.
- **Cleanup runs two ways:** a built-in scheduler (`CLEANUP_SCHEDULER_ENABLED`, `CLEANUP_INTERVAL_MINUTES`, fallback
  15) and a standalone command, `python -m app.jobs.cleanup`, for OS cron or cloud schedulers. A Postgres advisory
  lock makes sure only one instance deletes at a time.
- Expired conversations are already unreachable through the API before cleanup removes them.

## Why

- Measuring from last activity keeps an active chat whole, so follow-up questions keep their context.
- Audit events need a longer, separate window for access investigations.
- Raw result rows can contain row-level-security-protected data, and keeping them adds risk without benefit.

## Consequences

Data can outlive its window by up to one cleanup interval in storage, but never through the API.
