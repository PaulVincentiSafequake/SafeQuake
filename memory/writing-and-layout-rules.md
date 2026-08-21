# Writing and layout rules — READ BEFORE ADDING ANY TEXT OR CONTROL

Paul is dyslexic. He said, 2026-08-21:

> "remember that whenever you create buttons, text and descriptions and
> manage layouts, that I am dyslexic and I want both dashboard and the app
> to be simple to understand, intuitive to use always following the most
> intuitive approach"

This is not a preference to weigh against other things. It is a
requirement, and it applies to every surface: the dashboard, the phone
app, the PDFs, the CSVs, error messages and confirmation dialogs.

## Words

1. **One idea per sentence. Maximum ~12 words.** If a sentence needs a
   comma-and-then-a-clause, split it into two lines.
2. **Lead with the thing that matters**, not with the qualification.
   Good: "Not responding: 1 person on the working board."
   Bad:  "It should be noted that the not responding figure, which counts
         only people on the working board, is 1."
3. **Everyday words only.** No jargon, no acronyms, no product-speak.
   Never: token, payload, unregistered, endpoint, record state, null,
   deserialise, APNs, bucket.
   If a technical fact must be shown (e.g. Apple's reason string), put it
   in brackets after the plain-English sentence, never instead of it.
4. **Same thing = same words, everywhere.** Never "removed" on one screen
   and "deleted" on another. The label table in `record_state.py` is the
   authority for the four states.
5. **Verbs first on buttons**, 2–4 words: "Take off the board",
   "Put back on the board", "Mark as rescued", "Yes — same person".
6. **No double negatives, no irony, no cleverness.** A tired reader at 4am
   must not have to work out what we meant.
7. **Numbers get a label on the same line.** Never a bare figure.
8. **Read it aloud.** If it does not survive being spoken over a radio,
   rewrite it.

## Layout

1. **Short lines beat paragraphs.** Break provenance and explanation text
   into separate lines, each its own `<div>`, not one block of prose.
2. **Body text 12.5px minimum** in the dashboard sidebar; 13px for
   anything explaining a number. Small grey text is the first thing to go
   when someone is tired.
3. **Line height 1.5 or more.** Left-aligned, never justified.
4. **Never colour alone.** Every state carries a word and a symbol.
5. **Touch targets 32px minimum** in the dashboard, 44px on the phone.
6. **Most important thing first, top-left.** Do not make the eye hunt.
7. **One action per row** where a mistake would be costly. Do not line up
   a destructive control next to a routine one.
8. **Bold the first two or three words** of a line when a list is being
   scanned, so the eye can skip.

## Confirmations

- Say what will happen, in one short line.
- Say what will NOT happen ("Nothing is deleted") — that reassurance is
  worth its space.
- Say how to undo it.

### Capitals: the standing exception (agreed with Paul, 2026-08-21)

The rule is still "no capitals for emphasis". These are NOT emphasis and are
NOT to be tidied away by a future wording sweep:

  * The triage category names — IMMEDIATE, SERIOUS, MINOR — on the group
    headings, the severity badges and the map key. They are recognised
    triage signals a rescuer reads across a room, not a raised voice.
  * DROP. COVER. HOLD ON. on the alert screen, for the same reason.

Paul: "Standard triage category names, the same exception as DROP COVER HOLD
ON: a recognised signal, not emphasis."

Everything else stays sentence case. If a sweep is tempted to change one of
the above, the answer is no — read this line instead.
