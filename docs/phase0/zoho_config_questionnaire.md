# Zoho config questionnaire (~10 min, owner)

Answer once. Each answer turns into a config-file value in
`config/plays.yaml`, `config/zoho.yaml`, or a constant in
`intake/sources/zoho_crm.py` once Phase 1 lands.

---

**1. Zoho CRM data center.**
Which DC hosts Livewire's Zoho CRM? `us` / `eu` / `in` / `au` / `jp`.
> Answer: ____________________

**2. Open-deal stages.**
A customer with a deal in any of these stages is suppressed from the
queue (so the engine never interrupts a live sales thread). List the
exact stage names from your CRM. Default assumption: every stage
except `Closed-Won` and `Closed-Lost`.
> Answer: ____________________

**3. "Do not contact" field.**
Does Zoho Contacts have a custom "Do Not Text" or "Do Not Contact"
checkbox? If yes, paste the exact API field name.
> Answer: ____________________

**4. Contact owner field.**
Should the engine treat `Owner` (system field) as the rep, or is
there a custom `Sales Rep` lookup? Paste the API name of whichever
takes precedence.
> Answer: ____________________

**5. Activity type for Outgrow logs.**
When the rep clicks "I sent it," the engine writes a Zoho Call
Activity. Should the activity type be `Calls` (default), `Tasks`,
or a custom activity type? Paste the type name as it should appear.
> Answer: ____________________

**6. Activity subject prefix.**
What prefix on the Activity Subject lets owners filter Outgrow logs
in Zoho reports later? Default: `[Outgrow]`.
> Answer: ____________________

**7. Phone field source of truth.**
Reps' customers may have multiple phone numbers in Zoho (Phone,
Mobile, Other). Which field is the SMS-capable number for Outgrow?
> Answer: ____________________

**8. Tier-3 fuzzy-match thresholds.**
The match audit auto-accepts at ≥0.85. Any reason to raise or lower
that threshold for the first audit run? (Default: keep at 0.85.)
> Answer: ____________________

**9. Sandbox company.**
Does a Zoho CRM sandbox company exist? If not, please create one
before Phase 2 — it gates the 5-day write-back dry run.
> Answer: ____________________

**10. Suppression-by-account.**
Some Livewire customers have multiple Zoho contacts under one Account
(e.g., spouse + spouse). Should the engine treat suppressions as
account-wide (suppress all contacts in the account) or contact-level?
Default: account-wide.
> Answer: ____________________
