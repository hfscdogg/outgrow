# Pilot timeline

The owner + Zack pilot is a 30-day send window with a go/no-go decision
15 days after the last send so every late-pilot pulse accrues its full
14-day deal-attribution tail before the call is made. Concrete dates
below assume a **May 4, 2026** start.

| Day | Date | Gate | What happens |
| --- | --- | --- | --- |
| 0 | Mon, May 4, 2026 | **Pulses go live** | First daily pulse to owner + Zack. Open + action rates start tracking from day 1. |
| 5 | Mon, May 11, 2026 | **Queue review gate** | 5th business day after start (review the first 5 sends). Owner runs `outgrow queue explain --rep <id>` for both reps and flags any obviously-wrong customer. Flags feed back into `suppressions` or eligibility-rule tweaks. Owner can abort the pilot here if targeting looks broken before errors compound across the remaining 25 days. |
| 30 | Wed, Jun 3, 2026 | **Sends stop, habit metrics evaluated** | Pulses stop sending. Habit metrics (open rate, action rate, reply rate) are evaluated on the trailing 30-day window per rep. Targets: action rate ≥80%, reply rate ≥20%. |
| 45 | Thu, Jun 18, 2026 | **Go/no-go + Phase 2 sandbox promotion** | Outcome metrics (≥1 attributed deal per rep, attributed revenue) evaluated with full 14-day tails on every send. Decision: roll out to the rest of the team, and promote Phase 2 sandbox write-back to production if the day-1–5 sandbox run was clean. |

**Day-N convention.** All "day N" milestones snap forward to the next
business day if the calendar date falls on a weekend, since the engine
only sends Mon–Fri and review work happens on weekdays. Day 0 anchors
on the chosen Monday start; subsequent gates are interpreted as "Nth
business day after start" where the gate is review work, and "Nth
calendar day after start (snapped forward)" where the gate is an
attribution checkpoint.

**Attribution tails (days 30–45).** Sends stop at day 30 but deal
attribution keeps running for another 15 days so every send — including
the last one on day 30 — gets its full 14-day window before outcome
metrics are read at day 45.
