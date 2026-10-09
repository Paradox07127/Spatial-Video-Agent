# Structured spatial prompt (P12 re-ingest and our clip captions)

Used as `custom_prompt` for VSS re-ingest (`POST /api/v1/dashboard/reingest`, limit 800 characters) and by
`caption.py` for our 5 s clips. VastDB rows do not store the prompt, so `prompt_rev` = first 12 hex of the sha256 of the
text inside the fence below; `retrieval_recall.py` and `caption.py` record it. The embedder reads only the first 128
text tokens, so the lines that searches hit most (subjects, action) come first.

```text
Describe this video clip for search. Write exactly these seven labeled lines, in this order:
subjects: every person, forklift and vehicle visible, each with color, clothing or type
action: what each subject does, with plain verbs (walks, stops, steps back, turns left, crosses the road, brakes)
state_before: where each subject is and how it moves at the start
state_after: where each subject is and how it moves at the end
visible_interactions: who approaches, yields to, passes or nearly hits whom
occluders: shelves, racks, vehicles or objects that block the view of a subject
distance_hint: rough distance between interacting subjects (touching, under 2 m, 2-5 m, over 5 m)
Write "none" for a line with nothing visible. Describe only what is visible.
```
