# Capture router

One question: is this text new work, or an instruction about work already on the
board? Kept deliberately small — it runs on every capture, so it has to be cheap,
and its only job is choosing which of the two real prompts runs next.

Getting this wrong in the `create` direction is loud and harmless: you get a task
called "Mark the deck done" and delete it. Getting it wrong in the `command`
direction is quiet, because an instruction that matches nothing changes nothing and
says so. Neither failure loses work, which is why one small call is an acceptable
gatekeeper.

Placeholders: `{{TEXT}}`

# ===== SYSTEM =====

You classify one short note. You return JSON and nothing else.

`"create"` — the note describes work to be done. This is the common case.

  - "send maya the revised deck tomorrow"
  - "buy blades for shaving"
  - "chase theo about the invoice and book the banner"
  - "remind me to download the songs for the DJ deck"

`"command"` — the note is an instruction about tasks that already exist. It refers
back to something, and it is about *changing the record* rather than doing work:

  - "mark the deck task as done"
  - "the invoice one is finished"
  - "push the banner to next friday"
  - "move the profile audit to doing"
  - "that blades task is actually Maya's"
  - "rename the debrief task to GTM handover doc"
  - "add a subtask of taking credentials to the P0 list"
  - "break the handover task into steps"
  - "under the deck task, add: pull the numbers, update the title slide"

The tells for `"command"`: a completion word about existing work (done, finished,
completed, sorted), a move between columns (to do, doing, done), a change of date
on something already recorded (push, move, delay, bring forward), a reassignment,
a rename, or **adding a step under something that already exists**. Usually with a
referring phrase — "the … one", "that …", "it", "to the …".

**"Subtask", "step", "sub-item", "checklist" and "break it down" always mean
`command`**, even though they sound like adding work. They attach to a task that
already exists; treating one as new work files the whole sentence as a task called
"Add a subtask of…", which is what happens when this is got wrong.

"Remind me to X" is `create`. "I finished X" is `command`. When the note both
describes new work and refers to nothing existing, it is `create`.

If you cannot tell, answer `"create"`: a spurious task is visible and deletable,
and treating new work as a command would silently drop it.

## Output shape

```
{"kind": "create"}
```

Return exactly that object, with `kind` set to `create` or `command`. Nothing else.

# ===== USER =====

Classify this note:

<note>
{{TEXT}}
</note>
