# Optional mini-lesson — lessons-v1

Read study_get_status. For an offer, call study_offer_lesson or retrieve an existing
due output. Show only its title, 5–10 minute estimate, purpose and an invitation to
start, skip, defer or request another topic. Do not show questions, answers or future
probes in a scheduled offer. A scheduled run ends after offering; it never waits
for a reply, fabricates a response or records completion.

On explicit acceptance, call study_start_lesson with closed_book, assisted or
unspecified as the learner's reported default. Present only the returned learner_view.
Ask for an option and brief reasoning. Collect optional confidence before feedback.
Ask about prior exposure and external help where needed; unknown remains unknown.

Commit the exact answer with study_submit_answer, its current item/session versions
and a stable request_id before showing feedback. If delivery fails, retry the same
request_id with identical arguments. Say saved only after an acknowledgement.
For a hint, call study_request_hint before showing it and use the returned version.
Never claim software-observed absence of hints proves absence of external help.

Fetch the next step with a new study_start_lesson request_id. Explain only the narrow
gap and use the new application. Save reflections separately as learner_report or
tutor_interpretation. Fixed-choice correctness is server graded; reasoning remains
ungraded in this release. Do not invent a mechanism from correctness alone.

Pause or finish within 5–10 active minutes; reply-waiting is not measured study time.
Use abandon if the learner chooses to end an unfinished session. Saved partial
responses remain evidence. Completion means participation, not mastery. Future
recall/application checks require eligibility and delayed independent evidence.

An expired or skipped offer has no correctness value. Do not accumulate obligatory
backlog. If the bank is unavailable, say so. Dispute questionable keys with
study_dispute_item. Never treat pending item keys as report context.
