# Livewire Outgrow Engine — Build Plan

## Context

Livewire's residential/commercial AV and smart home integration business has paying customers going back to 1994, many worth $50K–$500K+ in lifetime value. A meaningful chunk of those relationships predate the current QBO instance — QBO shows only a partial transaction history, not true lifetime spend, for the oldest customers. Alex Goldfayn's *Outgrow* methodology says growth comes from daily, small, proactive outreach to those existing and dormant customers — not cold leads. Reps believe it; reps don't consistently do it. A 30-year-old dormant customer is often the highest-value re-engagement the business can make and also the hardest (data quality decays, reps-of-record retire), so the engine must treat these as first-class, not footnotes.

The engine's job is to make the daily habit **frictionless for reps**, not to replace them. Each weekday morning the engine emails each rep a single, pre-drafted Outgrow pulse: one dormant customer to reach, why now, and a suggested text written in the rep's voice. The rep sends it from their own phone (or pastes/edits it first), then clicks "I sent it" in the pulse email — which writes a Call Activity to Zoho on their behalf. The engine never sends SMS. It is a **prompter**, not a sender.

This preserves: authentic rep voice (no AI drift into customer thread), clean consent (rep is texting their own customer from their own number), natural reply handling (reply lands in the rep's existing SMS thread with the rep, who started the conversation), and Goldfayn's daily-ritual shape.

Target architecture: standalone Python service, scheduled daily via GitHub Actions (pulse email at 07:00 ET weekdays), SQLite for local state, same layout DNA as Permit Miner / Intel Engine (owner to confirm alignment — repo was greenfield at plan time).

## Locked decisions (from Q&A in planning thread)

| # | Decision |
|---|---|
| Send model | **Pure suggestion.** Engine never sends SMS. Each rep gets a daily email pulse; rep sends from their own phone and clicks "I sent it" to log. |
| Cadence | **Daily morning pulse, Mon–Fri 07:00 ET**, one suggestion per rep per day. |
| Delivery medium | **Email** to rep inbox, with signed action links ("Sent as-is" / "Sent with edits" / "Skip today"). |
| Zoho logging | Engine writes **Call Activity** to Zoho on rep's confirmation (not on engine's own send, because there isn't one). Builds the logging habit without adding rep work. |
| Zoho ↔ QBO matching | No shared ID exists. 3-tier match: (1) email exact, (2) E.164 phone exact, (3) fuzzy name+address. Only tiers 1+2 auto-flow to Growable queue. Tier 3 goes to review queue. |
| D-Tools | Cloud API exposes project + project-item data (manufacturer, model, serial). DYK feasible but needs a hand-curated prerequisite matrix — deferred to Phase 2. |
| Service-ticket suppression | **Zoho Desk** — pull open tickets nightly into `suppressions` with `reason=open_ticket`, auto-expire on ticket close. |
| Voice source | v1 = hand-authored YAML per rep (tone notes + 10 sample real texts). Fathom API exists but covers only Google Meet and is per-user keyed — layered in Phase 3. |
| Selection logic | Play-first-then-best-customer, with seasonal weights per month. **Within a play, ranking is LTV-weighted + dormancy-weighted + legacy-bonus + rep-match-confidence-as-multiplier**. Default `rep_id = Zoho contact owner`; ambiguous-ownership customers still surface (to the Zoho-owner rep) with a one-tap "Not my customer — reassign to <suggested_rep>" action in the pulse, which updates `customers.rep_id` and re-queues. Only fully three-way-disagreeing matches stay silently suppressed. |
| Ranking priority | High-value opportunities, high-value customers, and right-rep-to-right-customer matching are **first-order product concerns**, not tuning details. Default weights live in `plays.yaml` and `config/ranking.yaml`; tune from real action/reply data. |
| Data horizon | QBO + Zoho CRM + D-Tools only; no pre-QBO legacy import. QBO-visible spend is a floor, not true lifetime, for customers older than the QBO horizon; a `data_completeness_score` signal flags this in pulse briefings and ranking. |
| Owner oversight | **Pre-launch:** 3–5 days of Zack's pulses CC'd to owner for voice-quality review. Owner is a pilot rep too, so owner keeps a lightweight drift journal for self-pulses. **Steady state:** daily digest of what got actioned vs. skipped. No blocking approval — rep is the last human in the loop. |
| Pilot scope | **Owner + Zack, 30-day send window with go/no-go decision at day 45.** Sends stop at day 30; the extra 15 days lets every late-pilot send accrue its full 14-day deal-attribution tail before the decision is made. Habit metrics (open/action rate) are evaluated at day 30; outcome metrics (deals/revenue) at day 45. |
| Production safety posture | **Zoho CRM:** Phase 0 + Phase 1 read directly from production (read-only scopes); Phase 2 Call Activity writes go to Zoho sandbox company for 5 days before promoting to prod. **QBO:** develop against QBO sandbox, promote to prod after match audit passes. Write operations are lint-forbidden in `qbo.py` (read-only by design). Pulse-email recipient list is CI-enforced against a hardcoded internal allowlist. |
| TCPA consent | Reps text customers from their own numbers; existing paying-customer relationship + rep-initiated send covers consent. |
| Seasonal weights | Ship with proposed defaults in `plays.yaml`; owner tunes over time. |

## Highest-risk assumptions (order to de-risk)

1. **Reps will actually open and act on a daily pulse email.** This is now the whole product; without rep action, nothing ships. Mitigation: 30-day pilot with owner + Zack; track open + action rates per rep from day 1; weekly check-in in pilot; if action rate <60% after 2 weeks, rethink medium (SMS reminder, dashboard, manager nudge).
2. **Zoho ↔ QBO matching precision ≥98%.** Mitigation: offline match audit of 200 records before any pulse goes out. Tier 3 fuzzy matches never auto-flow.
3. **Right-rep-to-right-customer accuracy.** A high-value customer surfaced to the wrong rep is as bad as the wrong customer. Mitigation: `rep_match_confidence` triangulates Zoho contact owner + QBO responsible-sales-rep + D-Tools project manager. Default rep is the Zoho contact owner; on disagreement, the pulse surfaces with a "Not my customer — reassign to <suggested_rep>" one-tap action so high-value-but-ambiguous customers stay reachable rather than silently disappearing. Only three-way disagreements stay suppressed pending owner review. A cross-rep disagreement report lands on owner's desk during Phase 0.
4. **Generated voice is usable as a starting draft.** Bar is lower than before — rep edits before sending — but an embarrassing draft erodes trust. Mitigation: Phase 0 hand-drafted samples review; Zack's first 5 days of pulses CC'd to owner; owner keeps drift journal for own pulses.
5. **DYK prerequisites are computable.** Deferred to Phase 2; prerequisite matrix curated by owner.
6. **Active-dispute suppression covers all cases.** Mitigation: Zoho Desk open-ticket pull + manual "do not text" field on Zoho contact as fail-safe. Rep is also last-line defense — they see the customer name before sending.
7. **Each rep has enough dormants for daily cadence.** Need ≥6× cooldown_days (45d default) = ~270 eligible. Mitigation: `scripts/queue_depth.py` report in Phase 0; lower cadence or widen window if thin. Legacy customers (pre-QBO horizon) expand the pool — they're often the highest-value dormants in the book.
8. **Data-quality decay on old customers.** Customers from 1994–2005 likely have stale phones/emails. Mitigation: bounce/disconnect signals feed `suppressions`; rep can one-click "bad contact info" from pulse to park a customer until contact details are refreshed.
9. **Production write-back pollution.** Wrong-contact Zoho Call Activity writes (Phase 2) are the single highest-risk operation. Mitigation: sandbox-first for first 5 days of Phase 2; minimum-scope token (Activities.CREATE only); every write embeds `outgrow_pulse_id` in Description for audit/rollback; `--dry-run` is default, explicit `--write` + `OUTGROW_ENV=production` required for real writes.

## Phased build plan

### Phase 0 — De-risk (1.5–2 weeks, mostly no-code)

> Honest sizing: with credentials kickoff + match audit + rep-match triangulation audit + voice-profile wizard + sample-draft review + Zack pilot conversation + consent memo + QBO sandbox setup, this realistically takes 1.5–2 calendar weeks elapsed (not full-time effort).

- Offline Zoho↔QBO match on 200 records; hand-audit precision (target ≥98% on tiers 1+2)
- **Rep-match triangulation audit**: cross-check Zoho contact owner vs. QBO responsible-sales-rep vs. D-Tools project manager on a 100-customer sample; any disagreements go to owner for manual reassignment before Phase 1. This is the new "right rep to right customer" baseline.
- Pull Zack's (and owner's) completed D-Tools projects to validate access and data shape
- Pull Zoho Desk open tickets to validate suppression feed
- **Voice profile bootstrap from history**: export owner's and Zack's last ~200 outbound customer texts (RingCentral admin export or Zoho activity log CSV); each marks 10–20 "sounds like me at my best" via `scripts/voice_profile_wizard.py`. Wizard extracts **distilled tone patterns** (typical sentence length, greeting/sign-off conventions, vocab/idioms, emoji and punctuation habits, formality level) and writes those into `config/reps/<rep>.yaml`; raw customer texts stay in a gitignored local cache (`.cache/voice/<rep>.csv`) and are not committed. Keeps PII out of git history while preserving voice fidelity.
- Owner reviews 15 hand-drafted sample messages × 2 reps (owner + Zack) × 3 plays (Daily Proactive, Reverse DYK, Referral)
- **Pilot-commitment conversation with Zack** only (owner is self-committed); document Zack's verbal commitment to daily open + action
- Write consent documentation memo (rep-initiated sends from rep's own number)
- **QBO sandbox setup**: connect engine to QBO sandbox company, verify pull works against sandbox data shape, defer production connection to after match audit passes
- **Zoho CRM production read-only tokens**: mint tokens scoped to `ZohoCRM.modules.contacts.READ`, `ZohoCRM.modules.deals.READ` only (Call Activity write scope deferred to Phase 2)
- **Done when:** all assumption mitigations have evidence; owner + Zack have signed off on voice samples; Zack has committed to the pilot; match precision ≥98% on tier 1+2; rep-match triangulation audit complete with disagreements reassigned

#### Phase 0 dispatch kit (pre-built artifacts to minimize owner/rep scheduling load)

These are produced up-front so Phase 0 execution is "forward + click" rather than "schedule meetings and draft emails." Each artifact is referenced from the Phase 0 bullets above.

- **Credential setup checklist** — `docs/phase0/credentials.md`: per-provider click-path (Zoho CRM, Zoho Desk, QBO, D-Tools, Anthropic, SMTP/IMAP) with exact UI locations, scope lists, and GH Actions secret names. Owner works down the list once.
- **Rep voice-sample dispatch email** — `docs/phase0/rep_voice_sample_email.md`: ready-to-forward email template with attached pre-sorted CSV of rep's last 200 outbound texts; asks rep to reply with 10–20 favorite row numbers. Owner forwards unchanged to Zack (owner self-serves).
- **Match-audit worksheet** — `scripts/match_audit.py --sample 200 --out csv`: emits side-by-side Zoho/QBO row pairs with confidence score and suggested verdict in a single sortable CSV; owner or VA fills the "confirm" column. Turns a judgment task into a spreadsheet task that a VA can run.
- **Rep-match triangulation worksheet** — `scripts/rep_match_audit.py --sample 100 --out csv`: for each sampled customer emits Zoho contact owner, QBO responsible-sales-rep, D-Tools project manager side-by-side with a `disagreement` flag. Owner reviews disagreement rows and sets authoritative `rep_id`.
- **Pilot-conversation talking points** — `docs/phase0/pilot_conversation.md`: 1-page brief for owner's 20-min conversation with Zack covering what to pitch, common objections, commitment phrasing, and promised rep-side benefits. Owner reads it, then has the conversation.
- **Consent memo draft** — `docs/phase0/consent_memo.md`: full memo text documenting TCPA coverage basis (rep-initiated sends from rep's own number to existing paying customer); owner reviews and signs, optionally routes to counsel.
- **Zoho pipeline configuration questionnaire** — `docs/phase0/zoho_config_questions.md`: single-page list of short questions owner answers once (which deal stages count as open, any custom "do not text" field, preferred call-activity format). Bundled with credential kickoff.
- **DYK matrix worksheet** — `docs/phase2/dyk_worksheet.csv` (produced at Phase 2 start): top-30 active SKUs pre-pulled from D-Tools with blank columns for related accessory / typical upgrade / consumable; owner fills from industry knowledge.

**Residual human time after dispatch** — owner: ~2 hr irreducible Phase 0 (credentials, conversations, memos) + ~1.5 hr rep-match triangulation review + ~2 hr Phase 2 DYK matrix + ~25 min Phase 1 CC review of Zack's pulses + ~10 min/week digest + own-pulse drift journal daily; Zack: ~70 min Phase 0 one-time, then the daily habit itself.

### Phase 1 — Pulse MVP with owner review (weeks 2–3)
- Nightly Zoho CRM (contacts + **open deals**) production read + QBO pull (sandbox for first few days of dev, then promote to prod once match audit green) → SQLite Growable queue. Customers with an active Zoho deal (stage ∉ won/lost) are written to `suppressions` with `reason=active_deal` so the engine never interrupts a live sales thread with a "just checking in" text.
- Zoho Desk open-ticket pull → suppressions
- **Ranking**: `domain/growables.py` scores eligible customers by `play_seasonal × log10(LTV + legacy_bonus) × dormancy_factor × rep_match_confidence − suppression_penalty`. Legacy customers (`first_known_contact_at` before QBO horizon) get a configurable bonus so the oldest/highest-relational-value dormants surface. `rep_match_confidence` acts as a multiplier; medium-confidence customers still surface (to the Zoho-owner rep) and the pulse exposes a one-tap "Not my customer — reassign" link. Only three-way-disagreeing matches stay suppressed.
- **Draft generation**: `generate/drafter.py` calls Anthropic API with **Sonnet 4.6** (`claude-sonnet-4-6`) as default; on rate-limit or 5xx, retry once on **Haiku 4.5** (`claude-haiku-4-5-20251001`). Both pinned in `config/ranking.yaml`. Customer notes / Desk descriptions / deal context are **sanitized through a strip-and-quote wrapper** in `generate/prompts.py` (escapes prompt-control sequences, wraps in clearly delimited XML tags, instructs the model that wrapped content is data not instructions) before being fed to the prompt — defends against prompt-steering content in customer notes (malicious or accidental).
- Daily job at 07:00 ET weekdays: skip reps where `today <= ooo_until`; for remaining reps pick-play-then-customer, generate draft via Anthropic API
- Pulse email to rep with **pre-pulse context briefing** (LTV estimate from QBO with a "partial since <first_qbo_date>" disclosure for legacy customers, first/last purchase date, `first_known_contact_at`, last install project + line items from QBO invoices, recent Zoho Desk tickets if any), draft, and signed action links: **Sent as-is**, **Sent with edits** (opens form to paste final text), **Skip today** (opens form for reason), plus a conditional **Not my customer — reassign to <suggested_rep>** link shown only when `rep_match_confidence` is medium (Zoho/QBO/D-Tools partially disagree). The briefing turns the pulse into a self-contained mini-CRM view so "Sent as-is" is a reasonable default more often and the rep doesn't need to flip to another tool to feel prepared.
- **Owner CC'd on Zack's pulses for 3–5 days** to catch voice drift; owner keeps a one-line-per-day drift journal for own pulses (own-voice CC doesn't work since owner is both reviewer and reviewee). Owner can email `kill <draft_id>` to suppress any pulse in-flight (pulse queued 06:55, sends 07:00 — 5-min kill window).
- **Day-5 queue review gate**: on pilot day 5, owner runs `outgrow queue explain --rep <id>` for both reps and flags any obviously-wrong customer. Flagged customers feed back into `suppressions` or eligibility-rule tweaks. Catches targeting errors before they compound across the remaining 25 days.
- Plays enabled: Daily Proactive Outreach, Reverse DYK
- Rep OOO: `config/reps/<rep>.yaml` supports `ooo_until: YYYY-MM-DD`; engine silently skips OOO reps
- Recipient allowlist enforced: pulse emails can only be sent to addresses in `config/recipient_allowlist.yaml`; CI fails if any rep YAML references an email off the list
- PII scrubbing in application logs (customer names + phones redacted before disk write)
- No RingCentral integration. No Zoho write-back yet.
- **Done when:** 5 straight business days of pulses, owner signs off on Zack's voice quality + own drift journal, both reps have opened ≥80% of pulses

### Phase 2 — Full feedback loop (weeks 4–5)
- Drop Zack-CC (owner moves to EOD digest instead; own drift journal continues as long as owner is piloting)
- **Zoho Call Activity write: sandbox first.** For the first 5 business days of Phase 2, writes go to a Zoho CRM sandbox company using the production contact graph mirrored over. Owner spot-checks sandbox timeline entries; after 5 clean days, promote to production write scope. Minimum-scope token adds `ZohoCRM.modules.activities.CREATE` only; lint forbids any other write operation in `zoho_crm.py`.
- **"I sent it" action** writes Call Activity to Zoho on rep's behalf with the final sent text (as-drafted or edited); each Activity Description includes a signed `outgrow_pulse_id` for audit/rollback
- Add Referral + Testimonial plays (60–180d post-install window, satisfaction gate)
- Per-rep frequency cap (1/day) + per-customer cooldown (45d)
- Add DYK play (requires prerequisite matrix authored by owner in `config/dyk_matrix.yaml`)
- Email kill-switch listener: reps email `outgrow-control@...` with `PAUSE <customer-id>` / `UNPAUSE <customer-id>` / `PAUSE ME <days>` / `UNPAUSE ME` (SKIP TODAY isn't needed — rep just ignores the pulse)
- **Inbox poll cadence**: control-inbox poller runs **every 15 min during 07:00–19:00 ET on weekdays** (hourly otherwise) so "Sent as-is" / "Sent with edits" actions reflect in Zoho Call Activity within ~15 min instead of hours. Tightens the rep feedback loop without burning GH Actions minutes.
- EOD digest email to owner: pulses sent, actioned, edited, skipped, ignored — per rep
- **Friday win-recap email to each rep at 16:00 ET**: this week's sent count, replies reported, attributed deals and attributed revenue (if outcome-join has landed by then — else counts only). Closes the feedback loop visibly and rewards the daily habit, directly protecting against the action-rate decay that is the biggest product risk.
- Manager alert email only on no-eligible-customer days (queue depth warning) and ignored-2-days-in-a-row per rep
- **Done when:** 10 business days, rep action rate (sent+edited+skipped clicks / pulses) ≥70%, 0 wrong-customer incidents

### Phase 3 — Metrics + refinement (weeks 6+)
- Nightly outcome-join job: match "sent" events to Zoho deals (14d window) and QBO invoices (90d window); populate `pulse_outcomes`
- Follow-up pulse 3 days after a "sent" action: "Did Jane reply?" y/n link — writes to `pulse_outcomes.reply_reported`
- Weekly metrics email: reply rate, attributed deals, attributed revenue — per rep and aggregate
- **Rate-this-draft signal**: optional 1–5 score mailto links in the pulse (below the main action links); rep score → `pulses.draft_quality_score`. Cheap directional signal on voice quality beyond the edit-rate proxy; scores feed prompt A/B testing.
- Fathom transcript ingestion for voice refinement (per-rep API keys)
- A/B test prompt variants per play; track action rate, reply rate, and mean draft-quality score per variant

### Phase 4 — Expansion
- Event-triggered plays: warranty expiry, firmware updates, install anniversary
- Weekly preview email: "Next week at a glance" Sunday evening with 5 customers coming up
- Outcome dashboard (web): reply rate, booked-appointment rate, revenue attribution
- Roll out beyond owner + Zack pilot (expansion trigger below)

## Success metrics & 30-day pilot targets

The engine is a **habit prompter** — its first job is to get reps texting daily; its second is for those texts to convert. The pilot runs **30 days of sends with owner + Zack** and a **decision point at day 45** so late-pilot sends (days 16–30) accrue their full 14-day deal-attribution tail before go/no-go on (a) widening to remaining reps and (b) enabling Phase 2 write-back to production (if sandbox run in Phase 2 days 1–5 was clean). Habit metrics are per-rep on a trailing 30-day window evaluated at day 30; outcome metrics are evaluated at day 45.

**Funnel (top to bottom):**

1. **Pulse open rate.** Did the rep open the email? Tracked via 1×1 pixel.
   - **Target: ≥85% of workday pulses.** Below 60% = medium is wrong (try SMS reminder).

2. **Rep action rate.** Did the rep click any of Sent / Sent-with-edits / Skip? Ignoring the pulse counts as ignored.
   - **Target: ≥70% by day 15, ≥80% by day 30.** This is the core habit metric.

3. **Send rate.** Of actioned pulses, what fraction were sent (vs. skipped)?
   - **Target: ≥70% of actioned = sent.** Low send rate = targeting is off (wrong customer).

4. **Reply rate.** Customers who reply within 72h of a rep-sent text. Rep-reported via 3-day follow-up pulse. Lossy but directional.
   - **Target: ≥20% by day 30.** Below 10% sustained = voice or targeting is off.

5. **Attributed deals.** New Zoho deals/opportunities created within 14 days of a sent event, joined on `(rep_id, customer_id, sent_at)`.
   - **Target: ≥1 per rep in the 30-day pilot window** (it's a 30-day window, 2 reps, 14-day attribution — realistic floor).

6. **Attributed revenue.** QBO invoices to the customer dated within 90 days. Reported, not targeted in v1 — baseline set during pilot.

**Meta (measures the engine itself):**
- **Queue depth per rep.** Must stay ≥6× cooldown_days; alert if it drops.
- **Wrong-customer-surfaced incidents.** Hard floor: 0. Any incident triggers match-audit rerun. (Rep is last-line defense so blast radius is small, but still treat as serious.)
- **Edit rate.** What fraction of sends are "sent with edits"? Directional signal for voice quality.

**Expansion trigger (day 45):** rollout to remaining reps if both owner + Zack (a) action rate ≥70% (measured at day 30), (b) reply rate ≥20% (measured at day 30), (c) at least 1 attributed deal each or strong qualitative signal (booked appointments, warm replies), evaluated at day 45 with full attribution tails, (d) Phase 2 sandbox write-back was clean, and (e) both explicitly say they want to continue.

## Data model (SQLite)

```
reps
  id, name, email, zoho_user_id,
  voice_profile_path, active,
  ooo_until,              -- nullable date; if today <= ooo_until, rep is skipped silently
  paused_until,           -- nullable date; from PAUSE ME <days> command
  created_at

customers
  id, zoho_contact_id, qbo_customer_id, dtools_contact_id, rep_id (FK),
  name, phone_e164, email,
  first_purchase_at, last_purchase_at, lifetime_spend_cents,  -- QBO-visible; floor for legacy customers
  first_known_contact_at,       -- earliest of Zoho creation / QBO creation / D-Tools project; drives legacy_bonus
  data_completeness_score,      -- 0.0-1.0; flags customers older than QBO horizon
  last_install_completed_at,
  match_tier (1|2|3), match_confidence,
  rep_match_confidence,         -- 0.0-1.0 from Zoho-owner vs QBO-rep vs D-Tools-PM triangulation; ranking multiplier
  rep_match_sources_json,       -- {zoho_owner: ..., qbo_rep: ..., dtools_pm: ...} for audit
  suppressed, suppression_reason,
  bad_contact_reported_at,      -- rep-flagged from pulse; parks customer until refreshed
  created_at, updated_at

plays
  id, key (daily_proactive|reverse_dyk|referral|testimonial|dyk),
  base_weight, enabled,
  min_days_since_install, max_days_since_install,
  seasonal_weights_json

play_rotation_state
  rep_id (FK), play_id (FK), last_used_at, use_count_30d
  PK(rep_id, play_id)

pulses
  id, rep_id, customer_id, play_id, generated_at,
  draft_text, model, prompt_version,
  pulse_email_sent_at, pulse_email_opened_at,
  status (queued|emailed|sent_as_is|sent_with_edits|skipped|ignored|killed_by_owner),
  final_sent_text,        -- populated on "Sent as-is" (=draft_text) or "Sent with edits"
  sent_at, skip_reason, rep_acted_at,
  draft_quality_score,    -- nullable 1-5, from optional rate-this-draft mailto link (Phase 3)
  zoho_call_activity_id, zoho_write_error

pulse_outcomes
  pulse_id (FK, unique), reply_reported, reply_reported_at, reply_source (followup_link|rep_email),
  first_attributed_deal_zoho_id, first_attributed_deal_created_at,
  first_attributed_invoice_qbo_id, attributed_revenue_cents,
  last_recomputed_at
  -- joined nightly via (rep_id, customer_id, sent_at) against Zoho Deals + QBO invoices

suppressions
  customer_id (FK), reason (open_ticket|active_deal|dispute|opted_out|manual|rep_paused),
  source_ticket_id, source_deal_id, added_at, added_by, expires_at

queue_snapshots
  taken_at, rep_id, customer_id, play_id, score, rank
  -- per-day audit of why this customer was chosen

control_events
  id, received_at, sender_email, command (pause_customer|unpause_customer|pause_me|unpause_me|kill_pulse),
  target_customer_id, target_pulse_id, raw_body, processed, error

config_versions
  id, kind (voice_profile|play_weights|prompt_template|dyk_matrix),
  rep_id (nullable), payload_json, activated_at
```

Indexes: `customers(rep_id, last_purchase_at)`, `customers(rep_match_confidence)`, `customers(first_known_contact_at)`, `pulses(rep_id, generated_at)`, `pulses(customer_id, sent_at)`, `pulses(status)`, `suppressions(customer_id, expires_at)`.

## Directory structure

> Placeholder — owner to confirm against Permit Miner / Intel Engine conventions before Phase 1.

```
outgrow/
  pyproject.toml
  README.md
  .github/workflows/
    pulse.yml              # 07:00 ET Mon-Fri: generate + email pulses
    nightly-sync.yml       # Zoho / QBO / Zoho Desk pull
    control-inbox-poll.yml # reads kill-switch + action-link mailbox; */15 min 07:00–19:00 ET weekdays, hourly otherwise
    outcomes-join.yml      # nightly deal/invoice attribution
  config/
    reps/<rep>.yaml        # voice profile, optional ooo_until, optional paused_until
    plays.yaml             # base + seasonal weights, eligibility windows
    ranking.yaml           # LTV / dormancy / legacy-bonus / rep-match weights
    recipient_allowlist.yaml # CI-enforced internal-only pulse recipients
    prompts/<play>.md      # draft-generation templates per play
    dyk_matrix.yaml        # Phase 2+
  src/outgrow/
    __init__.py
    cli.py                 # pulse, sync, match-audit, actions, digest, outcomes-join, queue (with `explain` subcmd)
    settings.py
    db.py
    migrations/
    sources/
      zoho_crm.py
      zoho_desk.py
      qbo.py
      dtools.py
    matching/
      identity.py          # 3-tier Zoho↔QBO match
    domain/
      growables.py         # eligibility + ranking
      rotation.py          # play-first selection with seasonality
      suppression.py
    generate/
      prompts.py
      voice.py
      drafter.py           # Anthropic calls
    deliver/
      pulse_email.py       # daily rep pulse with context briefing + signed action links
      digest_email.py      # EOD owner digest + weekly metrics
      followup_email.py    # T+3 "did they reply?" pulse
      friday_recap.py      # Phase 2: Friday 16:00 ET weekly win-recap to each rep
      zoho_call_activity.py
    actions/
      links.py             # signed mailto action links builder + verifier
    control/
      inbox_poller.py      # IMAP reader for action + control commands
      commands.py          # SENT / EDITED / SKIPPED / PAUSE / UNPAUSE / PAUSE ME / KILL parser + applier
    observability/
      metrics.py
      logging.py
  tests/
    fixtures/
    test_*.py
  scripts/
    match_audit.py         # Zoho↔QBO precision report
    rep_match_audit.py     # Zoho-owner vs QBO-rep vs D-Tools-PM triangulation
    queue_depth.py         # per-rep runway report
    voice_profile_wizard.py
```

**No always-on server needed.** Action links in the pulse email are `mailto:` URLs that pre-fill a reply to `outgrow-control@livewire.com` with a signed subject (e.g., `SENT pulse_<id> <token>`). Rep taps the link → native mail app opens with everything prefilled → rep taps Send → IMAP poller processes within the next batch run. This reuses the same inbox infrastructure as the kill-switch commands and keeps the whole system stateless/serverless. Poller runs every 15 min during 07:00–19:00 ET weekdays so Zoho Call Activity reflects the action within ~15 min — tight enough that reps don't see "I just sent it but Zoho doesn't show it" confusion.

## Critical files to create (Phase 1 targets)

- `src/outgrow/db.py` + `migrations/0001_init.sql` — schema above
- `src/outgrow/sources/zoho_crm.py` (read-only scopes Phase 1; CI lint forbids non-Activity writes), `zoho_desk.py`, `qbo.py` (lint forbids all writes), `dtools.py`
- `src/outgrow/matching/identity.py` — 3-tier Zoho↔QBO matcher
- `src/outgrow/matching/rep_match.py` — Zoho-owner vs QBO-rep vs D-Tools-PM triangulation → `rep_match_confidence`
- `src/outgrow/domain/growables.py` — eligibility filter + LTV / dormancy / legacy-bonus / rep-match-multiplier scoring
- `src/outgrow/domain/rotation.py` — play-first-then-customer selector with seasonal weights
- `src/outgrow/generate/drafter.py` — Anthropic call with voice profile + play prompt; default model `claude-sonnet-4-6`, fallback `claude-haiku-4-5-20251001` on retry
- `src/outgrow/generate/prompts.py` — prompt-template loader + customer-data sanitizer (strip-and-quote wrapper around Zoho notes / Desk descriptions / deal context to defend against prompt-steering content)
- `src/outgrow/deliver/pulse_email.py` — morning pulse email with mailto action links; recipient allowlist enforced
- `src/outgrow/actions/links.py` — signed mailto URL builder + HMAC verifier
- `src/outgrow/control/inbox_poller.py` + `commands.py` — parse inbound action + control emails
- `src/outgrow/observability/logging.py` — PII scrubber
- `config/reps/<rep>.yaml`, `config/plays.yaml`, `config/ranking.yaml`, `config/recipient_allowlist.yaml`, `config/prompts/*.md`
- `scripts/match_audit.py`, `scripts/rep_match_audit.py`, `scripts/queue_depth.py` — Phase 0 de-risk tools

## Verification

**Phase 0:**
- `python scripts/match_audit.py --sample 200` → prints precision/recall per tier; manually audit flagged cases
- `python scripts/rep_match_audit.py --sample 100` → emits CSV of Zoho-owner vs QBO-rep vs D-Tools-PM; owner resolves disagreements and sets authoritative `rep_id`
- `python scripts/queue_depth.py --rep <owner|zack>` → prints eligible-customer count; must be ≥270 per rep for 45d cooldown × 1/day cadence
- `python scripts/voice_profile_wizard.py --rep <id> --history path/to/exported_texts.csv` → interactive session where rep marks favorites; writes **distilled tone patterns** (sentence-length distribution, greeting/sign-off style, vocab/idiom list, emoji and punctuation frequency, formality level) to `config/reps/<rep>.yaml`; raw texts cached locally at `.cache/voice/<rep>.csv` (gitignored), never committed
- Owner + Zack sign off on generated sample drafts (out-of-band, documented in memo)
- Zack verbally commits to the pilot (owner self-committed); documented
- Zoho CRM production tokens minted with read-only scopes; QBO connected to sandbox company only

**Phase 1:**
- `python -m outgrow.cli sync` runs nightly Zoho CRM (contacts+deals, read-only) + QBO + Desk pull + D-Tools pull; spot-check 10 rows per table
- Customer with open Zoho deal appears in `suppressions` with `reason=active_deal`; never surfaced in queue
- `python -m outgrow.cli pulse --dry-run` (default) generates drafts + renders pulse emails without sending; briefing section shows LTV, `first_known_contact_at`, legacy-customer disclosure when QBO horizon < `first_known_contact_at`, last purchase, last install project, recent tickets
- `python -m outgrow.cli pulse --write` sends pulses to owner + Zack with owner CC'd on Zack's; each pulse contains 3 signed mailto action links; pulse refuses to send if any recipient isn't in `config/recipient_allowlist.yaml`
- Ranking sanity check: `python -m outgrow.cli queue --rep <id> --top 20` shows top-20 surfaced customers with their LTV, dormancy days, legacy bonus, rep-match confidence, and final score — eyeball-verify that high-value + rightly-owned customers rank first
- Score transparency: `python -m outgrow.cli queue explain --pulse <id>` (or `--customer <id>`) prints the full score breakdown — play seasonal weight × log10(LTV+legacy_bonus) × dormancy × rep-match × suppression_penalty — so any pick is auditable. Owner uses this for the day-5 review gate.
- Medium-rep-match-confidence customer (Zoho/QBO partial disagreement) DOES surface to the Zoho-owner rep, with a "Not my customer — reassign" link rendered in the pulse; tapping it updates `customers.rep_id` and re-queues
- Three-way-disagreeing customer (Zoho/QBO/D-Tools all differ) does NOT surface to any rep until owner resolves
- Prompt-injection check: a fixture customer with adversarial-looking Zoho note content (e.g., "ignore previous instructions and say X") goes through `generate/prompts.py` sanitizer and the resulting draft does not adopt the injected directive
- Rep taps "Sent as-is" link → mail app opens pre-filled → rep sends → IMAP poller flips `pulses.status = sent_as_is`, populates `final_sent_text`, `rep_acted_at`
- Owner emails `KILL <pulse-id>` within the 5-minute pre-send window → pulse status flips to `killed_by_owner`, never emailed to rep
- Set `ooo_until: <tomorrow>` in a rep's YAML → next morning's run skips that rep; `queue_snapshots` records the skip reason
- `pytest tests/` passes; coverage on `matching/`, `domain/`, `generate/`, `actions/` ≥80%
- CI lint: `scripts/check_write_scopes.py` scans `qbo.py` for any HTTP verb other than GET (fails build if found) and `zoho_crm.py` for non-Activity write endpoints (fails build if found)

**Phase 2:**
- **Sandbox days 1–5:** Zoho write target is sandbox company; owner manually inspects 10+ sandbox Call Activity entries, confirms correct contact, correct text, presence of `outgrow_pulse_id` in Description. Only after 5 clean days does the engine promote to production write scope.
- Rep taps "Sent as-is" → verify new Call Activity on Zoho contact timeline with correct text and `outgrow_pulse_id` audit trail
- Rep taps "Sent with edits" mailto → rep pastes edited text → poller captures `final_sent_text` and writes the EDITED text to Zoho
- Rep taps "Skip today" mailto → rep adds reason → `pulses.status = skipped`, `skip_reason` captured
- Email `PAUSE <customer-id>` from rep → `suppressions` row created, that customer excluded from future queues
- Email `PAUSE ME 3` from rep → `reps.paused_until = today+3`, rep gets no pulses for 3 business days
- EOD digest email lands in owner inbox with per-rep counts of sent / edited / skipped / ignored
- Friday 16:00 ET → win-recap email arrives in each rep's inbox with this week's counts + attributed deals/revenue
- No-eligible-customer for any rep → manager alert email fires once

**Phase 3:**
- T+3 days after a sent pulse, follow-up "Did Jane reply?" email arrives at rep's inbox with y/n mailto links
- Rep taps "y" → `pulse_outcomes.reply_reported = true`
- Run `python -m outgrow.cli outcomes-join` → for a known test deal in Zoho, verify `pulse_outcomes.first_attributed_deal_zoho_id` populates
- Weekly metrics email renders: open rate, action rate, send rate, reply rate, deals attributed, revenue attributed (per rep + aggregate)
- Pulse email shows optional rate-this-draft 1–5 mailto links; rep taps "4" → `pulses.draft_quality_score = 4`; weekly A/B report includes mean score per prompt variant
- Global kill switch: `OUTGROW_PAUSED=true` env var → pulse workflow exits before any email sends

## Open items handed back to owner

- Confirm directory layout matches Permit Miner / Intel Engine before Phase 1 coding starts
- Provide Zoho CRM (read-only scopes for Phase 1; sandbox + Activity.CREATE for Phase 2), Zoho Desk (read), QBO sandbox + production, D-Tools, Anthropic API credentials
- Create Zoho CRM sandbox company (for Phase 2 write-back dry run) if one doesn't already exist
- Authorize control email address `outgrow-control@livewire.com` (receives action links + kill-switch commands; IMAP access required)
- Confirm which Zoho Deals pipeline stages count as "open" for active-deal suppression
- Export ~200 recent outbound customer texts for owner + Zack (RingCentral admin export or Zoho activity log CSV) for voice-profile bootstrap
- 20-min pilot-commitment conversation with Zack (Phase 0)
- Populate `config/recipient_allowlist.yaml` with internal-only pulse email recipients (owner + Zack + any CC addresses)
- Pick HMAC secret for signed mailto action tokens (stored in GitHub Actions secrets)
- Review rep-match triangulation audit output (100 sampled customers); set authoritative `rep_id` for any disagreements

## Executive summary for Zack (pre-pilot check-in)

> Paste-ready text for the weekly phone call. Goal: come out of the call with Zack's verbal yes so Phase 0 dispatch can start.

---

**Subject: Outgrow daily-pulse pilot — quick read before our call**

Zack,

Quick heads-up before our check-in tomorrow. I want to talk you through a tool I'm building called Outgrow and ask you to be the pilot rep with me for 30 days. Here's the short version so you can read it cold.

**What it is.** Each weekday at 7am you'll get one email. It picks one customer of yours worth reaching out to that day, gives you the "why now" (last project, lifetime spend, what they bought, recent service tickets), and shows you a suggested text already written in your voice. You read it on your phone, send it from your own number — as-is, edited, or skip the day — and tap a link in the email so it logs to Zoho automatically. That's it. One customer a day, ~5 minutes.

**What's in it for you.** Goldfayn's whole pitch: the relationships you already have are the highest-value pipeline you've got, and the only thing standing between you and the next deal is daily small reach-outs you don't always have time to think up. This does the thinking-up part for you. Friday at 4pm you'll get a recap showing what the week's outreach turned into — replies, attributed deals, attributed revenue. Closes the loop.

**What it won't do.** It will never text a customer for you. It will never write to Zoho without you tapping a link. It will never use AI in the actual customer-facing thread — the AI just drafts, you send. Your voice, your number, your customer.

**What I need from you before we start.**
1. ~30 min of your time for me to pull a sample of your last 200 outbound customer texts; you mark 10–20 that "sound like you at your best" so the engine learns your voice. No raw texts get committed to anything — only the patterns (your typical greeting, sign-off, sentence length, etc.) get stored.
2. A 20-min chat (this one) to commit to opening the daily email and acting on it for 30 days. If your action rate falls below 60% in week 2 we either fix it or kill it — you're not stuck.
3. OOO / vacation / "pause me 3 days" are all built in — email a one-word command to the control inbox and the pulses stop. No babysitting.

**Timeline.**
- 1.5–2 weeks of setup (I do almost all of it; you give me the voice-sample 30 min)
- 30 days of daily pulses, owner + you only
- Day 45 = go/no-go on rolling out to the rest of the team, with full attribution on every send

**What I'm asking tomorrow.** Just a verbal yes to be the pilot rep with me, and a time to do the voice-sample session.

Let me know if anything in here changes how you want to use the call.

—

