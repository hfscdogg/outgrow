# Voice-sample request email (forward to Zack)

Forward the message below to Zack unchanged. The CSV attachment lives at
`.cache/voice/zack-export.csv` after running the RingCentral or Zoho
activity-log export — that file stays on the operator's machine and is
**never committed** (`.cache/` is gitignored).

---

**Subject:** Outgrow voice-sample — 30 min, no busywork

Hey Zack,

Quick favor before we kick off the pilot. Attached is a CSV of your last
~200 outbound customer texts, one per row with a row number. I need you
to skim it and reply with the row numbers of **10–20 messages that
sound like you at your best** — the ones you'd be proud of if a customer
forwarded them to a friend.

That's it. No re-writes, no judgments. Just row numbers.

What I do with them: a small script reads only the messages you marked
and pulls out *patterns* — your typical greeting, sign-off, sentence
length, how often you use emoji, vocab you favor. Those patterns go
into a config file the engine uses to draft your daily pulse. **Your
raw texts never get committed to anything.** Only the distilled
patterns do.

Reply by EOD Friday and I'll have the engine ready to run a dry-run on
your Monday pulse.

Thanks —
[Owner]

---

## Operator runbook

1. Export the rep's last 200 outbound customer texts to
   `.cache/voice/<rep>-export.csv` with columns `row, sent_at, text,
   recipient`. Export source: RingCentral admin export or Zoho activity log.
2. Forward the email above with the CSV attached.
3. When the rep replies with row numbers, save their picks to
   `.cache/voice/<rep>.csv` (just the marked rows, columns `text, sent_at`).
4. Run the wizard: `python -m voice.wizard --rep <rep> --history
   .cache/voice/<rep>.csv`. Output lands in `config/reps/<rep>.yaml`.
5. Eyeball the YAML for surprises (e.g., greeting that doesn't match
   how the rep actually talks). Hand-edit if needed and commit the
   YAML — only the YAML, never the CSV.
