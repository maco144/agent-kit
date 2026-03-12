# Spec 04 — SLA-Backed Support

**Version:** 0.1
**Date:** 2026-03-12
**Status:** Draft
**Depends on:** None (operational, not technical)

---

## 1. Problem

Enterprise engineering teams will not run business-critical agents on infrastructure without a support commitment. When a circuit breaker won't close, an audit chain is failing verification, or a billing agent goes down before payroll runs — they need to know that someone will respond, and how fast.

SLA-backed support is what converts "interesting open-source library" into "enterprise-approved vendor." Without it, the sale stalls at the security/vendor review stage regardless of product quality.

---

## 2. Support Tiers

### Free

- Community Slack channel only
- No response time guarantee
- GitHub issues (best-effort triage, no SLA)
- Self-serve documentation

### Pro ($X/month — price TBD at sales phase)

- Email support
- **Response time:** 1 business day for P2/P3; 4 business hours for P1
- Business hours only (09:00–18:00 customer local time, Mon–Fri)
- Access to private Slack channel (shared, not dedicated)
- Maximum 3 named support contacts per org

### Enterprise (custom contract)

- Dedicated Slack channel with agent-kit engineers
- **Response time:** 1 hour for P1 (24/7); 4 business hours for P2; 1 business day for P3
- Named customer success engineer (CSE) assigned
- Monthly check-in call with CSE
- Access to pre-release builds and roadmap previews
- Unlimited named support contacts
- Quarterly business review
- Custom SLA terms negotiable for regulated industries (financial services, healthcare)

---

## 3. Priority Definitions

| Priority | Definition | Example |
|---------|-----------|---------|
| **P1 — Critical** | Production system down or severely degraded. Business impact occurring now. No workaround available. | All agents throwing `CircuitOpenError`, audit chain verification failing for a compliance deadline, complete ingest outage |
| **P2 — High** | Significant functionality impaired. Workaround exists but is painful. Business impact expected if unresolved. | Dashboard missing data, alerting not firing, export endpoint returning errors |
| **P3 — Normal** | Non-urgent issue or question. No immediate business impact. | Configuration questions, feature requests, integration guidance, non-critical bugs |

Customer sets priority on ticket creation. agent-kit support may downgrade (with explanation) if a ticket is miscategorized. agent-kit support may upgrade a ticket unilaterally if investigation reveals higher impact.

---

## 4. Support Channels

### 4.1 Support Portal (web UI)

Primary channel for Pro and Enterprise. Ticket submission, status tracking, conversation history, file attachments.

**Required fields on submission:**
- Priority (P1/P2/P3)
- Subject
- Description
- agent-kit version (`pip show agent-kit`)
- Provider in use (Anthropic / OpenAI / Ollama)
- Relevant code snippet or error traceback
- Impact: number of agents/users affected

**Optional fields:**
- Audit run ID (for audit chain issues)
- Trace ID (from `AgentResult.trace_id`)
- Circuit breaker resource name

### 4.2 Dedicated Slack Channel (Enterprise)

Channel naming convention: `#agentkit-support-{company-slug}`

Response time SLAs apply to Slack messages as well as portal tickets. P1s posted to Slack outside business hours should be escalated to on-call via PagerDuty (internal).

### 4.3 Emergency P1 Escalation (Enterprise)

Enterprise customers receive a phone escalation path for P1s that have gone unacknowledged past SLA. Escalation contact (email/phone) is documented in the Enterprise contract and communicated at onboarding.

---

## 5. SLA Tracking

### 5.1 First Response Time (FRT)

The clock starts when a ticket is **submitted** (portal) or **posted** (Slack). It stops when a agent-kit support engineer posts a substantive response (not an auto-acknowledgement).

For Pro, the clock runs only during business hours. For Enterprise P1, the clock runs 24/7.

### 5.2 SLA breach process

- **T+50% of SLA window:** Automated internal alert to the assigned engineer.
- **T+80% of SLA window:** Alert escalates to support lead.
- **T+SLA breach:** Alert escalates to VP Engineering (internal). Customer is notified proactively with an apology and updated response ETA.
- **Monthly SLA report:** Emailed to Enterprise customers. Shows all tickets, FRT, resolution time, SLA met/missed.

### 5.3 SLA credits (Enterprise)

Customers may claim credits against their invoice for missed P1 SLAs:

| Missed SLAs in a calendar month | Credit |
|----------------------------------|--------|
| 1 | 5% of monthly fee |
| 2 | 15% of monthly fee |
| 3+ | 25% of monthly fee (capped) |

Credits are applied to the following invoice. They do not accumulate across months and cannot be redeemed for cash. Credit claims must be submitted within 30 days of the incident.

---

## 6. Support Portal — Technical Spec

The support portal is a lightweight internal tool, not a custom build. Use an existing platform:

**Recommended:** [Linear](https://linear.app) (internal ticket management) + [Plain](https://www.plain.com) (customer-facing portal, integrates with Slack and email natively, built for developer tools).

**Alternative:** Zendesk (more mature SLA tooling, heavier).

**Integration with agent-kit Cloud:**
- Support tickets link to the customer's org in the cloud platform.
- Engineers viewing a ticket can access the customer's audit runs, circuit breaker history, and alert history directly from the ticket context — no need to ask the customer for run IDs they may not know how to find.
- This is the **key differentiator** of agent-kit support vs. generic SaaS support: the support engineer arrives already knowing what happened.

**Implementation:** A sidebar widget in the support tool that takes the customer's org ID and renders the last 24h of fleet dashboard data inline.

---

## 7. Onboarding (Enterprise)

### Week 0 — Contract signed

- CSE assigned and introduced via email
- Dedicated Slack channel created
- Emergency escalation contacts shared
- Cloud platform org provisioned

### Week 1 — Technical onboarding

- 60-min call: walkthrough of agent-kit Cloud features with the customer's engineering team
- SDK integration review: customer shares their `AgentConfig` setup; CSE reviews for correct configuration of `CloudReporter`, circuit breaker config, audit settings
- Alert rules configured together in the call
- Runbook created: how the customer's on-call team should respond to P1 alert types

### Month 1 — Check-in

- 30-min call: review of the past month's fleet dashboard data, alert history, any open issues
- Roadmap preview: upcoming features relevant to the customer's use case

### Quarterly — QBR (Quarterly Business Review)

- 60-min call: cost trends, reliability metrics, ROI discussion, roadmap input
- Written summary shared post-call

---

## 8. Knowledge Base

Self-serve documentation covering:

- Troubleshooting circuit breaker stuck in OPEN state
- Interpreting audit chain integrity failures
- Cost spike root cause investigation guide
- Migrating from LangChain to agent-kit (with common pitfalls)
- Provider-specific quirks (Anthropic rate limits, OpenAI timeout behavior)
- Alert tuning guide (avoiding false positives on cost anomaly alerts)

Knowledge base is public (no login required). Articles are written by the support team based on recurring ticket patterns.

---

## 9. Internal Tooling Requirements

### 9.1 On-call rotation (internal)

For Enterprise P1 coverage (24/7):

- PagerDuty rotation across engineering team
- Minimum 2 engineers on rotation at all times
- On-call handbook covering all P1 scenarios with playbooks
- Post-incident review required for all P1s

### 9.2 SLA dashboard (internal)

Internal dashboard (can be a Retool or Metabase view) showing:

- All open tickets with SLA countdown
- Tickets at risk (>80% of SLA window elapsed)
- Monthly FRT and resolution time averages by tier
- SLA breach count (target: 0)

---

## 10. Acceptance Criteria

- [ ] Pro customers can submit a ticket and receive auto-acknowledgement within 5 minutes
- [ ] P1 tickets for Enterprise customers trigger an internal PagerDuty alert within 2 minutes of submission
- [ ] SLA breach escalation fires at correct percentages (50%, 80%, 100%)
- [ ] Monthly SLA reports are sent automatically on the 1st of each month
- [ ] Support portal sidebar shows correct fleet dashboard data for the customer's org
- [ ] Enterprise onboarding call is completed within 7 days of contract signing
- [ ] Knowledge base is searchable and all articles are indexed within 24 hours of publication
