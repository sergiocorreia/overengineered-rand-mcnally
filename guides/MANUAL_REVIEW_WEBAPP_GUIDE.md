# Manual Review Web App Guide

This guide describes how to build local web apps for evidence-based manual review in historical-data projects. It covers the human workflow: deciding what the reviewer must judge, assembling sufficient evidence, presenting it clearly, making repetitive decisions fast, and preserving those decisions safely.

The [data extraction guide](DATA_EXTRACTION_GUIDE.md) governs source evidence and extraction review, the [data quality guide](DATA_QUALITY_GUIDE.md) governs anomaly adjudication and repair, and the [Python style guide](PYTHON_STYLE_GUIDE.md) governs implementation structure. This guide complements them; it does not weaken their provenance or validation requirements.

The central rule is: **optimize for reliable decisions per minute, not for the amount of information placed on screen.**

## 1. Design the decision before the interface

Write down the review task before choosing a layout or framework:

- What exact question requires human judgment?
- What evidence can answer it?
- What mutually exclusive outcomes may the reviewer save?
- Which outcomes need an explanation?
- What later build step will consume the decision?

A review case should represent one decision, not merely one database row or one flagged cell. Group all candidates and supporting pages needed to settle that decision. If a human must mentally combine several screens, external files, or undocumented rules, the case definition is incomplete.

Use the review queue to reduce the problem before it reaches the interface. Deduplicate equivalent flags, group related observations, precompute safe comparisons, and order cases deliberately. Do not ask the reviewer to rediscover facts that the pipeline already knows.

## 2. Bring enough evidence into each case

The displayed evidence must be sufficient to support the saved outcome. A candidate page alone is not always enough. Include, when relevant:

- every source page containing a competing value;
- the page that defines a footnote or marker;
- a preceding page carrying the table header or date columns;
- a continuation page needed to interpret row or column scope;
- another publication vintage used as independent support; and
- the raw transcription and provenance needed to locate the value.

Footnote letters, asterisks, parentheses, and similar markers are scoped by their source document and layout. Do not assign one global meaning to a marker. When a definition appears elsewhere in the same issue, add that page to the evidence bundle and explain why it matters.

Generated queues are read-only evidence indexes. They may point to source images and immutable extracted observations, but the web app must never edit those inputs. Human decisions belong in separate durable files under `manual/`.

## 3. Use the whole viewport deliberately

For a desktop scan-review task, the default layout should use one fixed browser viewport and three functional regions:

1. **Source:** the scan, page tabs, and zoom controls.
2. **Evidence:** the entity, target period or field, plain-language issue, and candidate values.
3. **Decision:** outcome controls, conditional inputs, evidence confirmation, and save action.

This arrangement uses horizontal space that would otherwise become empty margins and keeps the scan tall. Put decision controls in a dedicated right column instead of below the evidence when placing them below would create scrolling or hide them beneath the fold.

Use `100dvh` and `minmax(0, ...)`-style constraints so the application shell fills the viewport without causing document-level scrolling. Internal scrolling is acceptable for an unusually long candidate list or decision form, but the primary scan, issue, choices, and save action should remain available together for ordinary cases.

Keep the top bar short. Progress, filters, search, case navigation, and help belong there; branding, decorative labels, and generous padding do not. Remove footers, source-path overlays, and repeated metadata when they consume scan area without helping the current decision.

The scan pane should support:

- fit-to-pane on load;
- zoom in, zoom out, and return to fit;
- pan while zoomed;
- visible page tabs for every supporting scan; and
- fast switching without resetting the review decision.

Do not assume a large monitor. Verify the ordinary workflow at both a common laptop viewport such as 1366 by 768 and a larger desktop viewport such as 1920 by 1080.

## 4. Make the information hierarchy match the task

The most important facts should be the easiest to see. In most adjudication apps, the hierarchy is:

1. entity or record being reviewed;
2. exact target date, week, field, or column;
3. candidate values or interpretations;
4. row label and printed date to find in each scan;
5. why the pipeline could not decide;
6. provenance and implementation metadata.

Do not render a critical week or column name as faint helper text. Use normal-size body text of roughly 15 pixels or more, larger section headings, and a prominent target value or date. Small type is suitable only for genuinely secondary metadata.

Each candidate should show the value prominently and answer, without inference:

- Which scan supports this option?
- Which printed row should the reviewer find?
- Which printed date or column should the reviewer inspect?
- Does the source date differ from the canonical analytical period?
- Is the candidate blank, missing, excluded, or numeric?

Keep raw and canonical concepts visibly distinct. For example, show a canonical panel week separately from the date printed in a source rather than collapsing the two into one ambiguous label.

## 5. Explain the issue in ordinary language

Every case should contain two short pieces of guidance:

- **Why this needs you:** what the pipeline found and why it cannot safely decide.
- **What to do:** the concrete comparison the reviewer should make.

Use domain language that a busy researcher understands. Do not expose internal flag names such as `amount_tie`, `collision`, or `normalization_mismatch` without translating them. “Different source observations give different values for the same place and week” is more useful than “Amount tie.”

Keep the explanation specific to the case type. A same-name geographic collision requires a different instruction from a likely one-digit transcription disagreement. If the source period maps to a different canonical period, say so directly and identify the printed date to find.

## 6. Make decisions explicit and fast

Use outcome-based button labels and a one-sentence explanation of when each applies. A general source-conflict reviewer might use:

- **Use selected value:** the chosen scan is correct as printed.
- **Enter a correction:** the scan supports a different transcription.
- **Keep it excluded:** no reliable value can be established.
- **Review later:** the case remains unresolved and needs more investigation.

Adapt the outcomes to the research contract rather than copying these labels mechanically. The buttons must be mutually exclusive, visibly selected, and validated against any required fields. Disable an outcome that cannot yet be completed, such as correction before a source candidate is selected.

Prefer one-click common paths. Selecting a candidate may also set the ordinary “use selected value” outcome and mark its page as reviewed. Keep the save-and-advance control persistently visible so the reviewer does not hunt for it after every case.

Do not require prose that adds no information. A note should normally be:

- optional for a clear source selection;
- optional for a clear transcription correction when the structured fields fully explain it;
- required when excluding evidence as unclear; and
- required when leaving a case unresolved.

When a note is required, one clear sentence is enough. Change the label, help text, placeholder, and validation together so the interface never says “optional” while the server rejects a blank value.

## 7. Support a complete keyboard workflow

The reviewer should be able to complete common cases without moving between keyboard and mouse. A useful default shortcut vocabulary is:

| Shortcut | Action |
|---|---|
| `1`–`4` | Select a candidate |
| `J` / `K` or right / left arrow | Next / previous case |
| `D` / `A` or `]` / `[` | Next / previous source scan within the case |
| `C` | Enter correction mode |
| `X` | Exclude as unclear |
| `O` | Leave open for later review |
| `Ctrl/Cmd+Enter` | Save and advance |
| `Ctrl/Cmd+S` | Save without advancing |
| `+` / `-` | Zoom the scan |
| `F` | Fit the scan |
| `/` | Focus search |
| `?` | Show shortcut help |

Shortcut choices may vary, but the action set should remain complete. Navigating cases is not a substitute for navigating multiple scans within one case.

Do not fire ordinary single-key shortcuts while focus is inside an input, textarea, select, or editable element. Preserve expected browser behavior unless the app deliberately handles a modifier shortcut. Display shortcut hints beside frequent actions and provide an in-app help panel.

Mouse and keyboard paths must change the same state and use the same validation. Test both.

## 8. Preserve orientation and progress

Reviewers need to know where they are, what remains, and whether their last action was saved. Provide:

- reviewed and remaining counts;
- a compact progress indicator;
- next and previous case controls;
- filters for remaining, completed, open, and stale cases;
- search over the identifiers humans actually recognize;
- an unmistakable selected state;
- confirmation after a successful save; and
- an empty-state message when filters match nothing.

After “save and next,” move predictably to the next visible unresolved case. If the current filter removes completed cases, do not jump backward or redisplay the saved case unexpectedly.

Warn before navigation, filtering, reload, or browser close would discard unsaved changes. Do not show warnings after a successful save.

## 9. Store decisions as durable evidence

Use a dedicated review-store module to validate and persist manual work. Each saved decision should carry, as applicable:

- stable case and source-observation IDs;
- a hash of the evidence and candidates reviewed;
- structured disposition and selected candidate;
- corrected value or other structured correction;
- supporting page IDs;
- note or reason when required; and
- review timestamp and reviewer identity when the project tracks them.

Write durable files atomically. Preserve decision history when a case is changed. When regenerated evidence no longer matches the stored hash, mark the decision stale and requeue it instead of silently applying it to changed inputs.

Saving an unchanged item can be meaningful evidence that a human checked it. Represent reviewed status explicitly rather than inferring it from whether values differ.

The downstream build should consume only validated, current decisions. It should preserve the original flags and source observations so a manual disposition never erases why the case was reviewed.

## 10. Keep the implementation small and local

Follow the thin local-app boundary from the Python style guide:

- Python loads cases, validates requests, serves approved images, and saves decisions;
- HTML and JavaScript render the interface and manage interaction;
- the review store owns durable state and history; and
- source-specific rules remain in the queue builder or a small adapter.

Bind to `127.0.0.1` by default. Whitelist images from the current case, resolve and verify paths beneath the configured evidence root, cap request sizes, validate all payloads on the server, and return clear validation errors. Do not expose an arbitrary local-file endpoint.

Avoid adding a large frontend framework to solve a small local workflow. Add dependencies only when they remove more complexity than they introduce. Split HTML, CSS, and JavaScript when their size makes the single file difficult to navigate, not merely to imitate a production web stack.

## 11. Test behavior and usability with real cases

Automated tests should cover:

- queue grouping, ordering, and stable case identities;
- inclusion of every required evidence page;
- conditional validation for each disposition;
- atomic saves, history, and stale-hash behavior;
- case and source-page keyboard navigation;
- shortcuts being disabled while typing;
- save-without-advance and save-and-advance behavior;
- unsaved-change warnings;
- filters, progress, and empty states;
- image-path containment and request-size limits; and
- fixed-viewport layout invariants.

String checks for essential labels and layout rules are useful regression guards, but they do not replace opening the app. Perform a visual walkthrough using representative real cases:

- the common two-candidate case;
- three or four candidates;
- multiple scans within one case;
- long entity and source labels;
- a shifted or ambiguous printed date;
- a blank candidate;
- a correction requiring an input;
- an unresolved decision requiring a note;
- a stale prior decision; and
- a case whose defining footnote appears on another page.

Watch an actual reviewer complete a short batch when possible. Confusing labels, excessive cursor travel, repeated scrolling, and unnecessary typing are data-quality risks as well as usability problems.

## 12. Common failure patterns

- **Large empty margins while controls sit below the fold.** Reallocate horizontal space into evidence and decision columns.
- **Important dates or field names in tiny muted text.** Promote the exact target the reviewer must find.
- **A large header and decorative branding.** Recover vertical pixels for the scan and decision.
- **Internal jargon without instructions.** Explain the conflict and the required comparison in plain language.
- **All metadata treated as equally important.** Establish a visual hierarchy and remove duplicative details.
- **A required note for every clear-cut case.** Make structured decisions carry the routine meaning; reserve prose for consequential context.
- **Case navigation but no source-page navigation.** Provide distinct shortcuts and visible page tabs for both levels.
- **Shortcuts firing while the reviewer types.** Ignore single-key actions in editable controls.
- **Reviewing only the page containing the number.** Include headers, continuation pages, and footnote definitions needed to interpret it.
- **A generic decision button with an unclear effect.** Name the saved outcome and explain when it applies.
- **Manual edits written into generated queues or model output.** Save a separate evidence-bound overlay under `manual/`.
- **No stale-decision policy.** Hash the reviewed evidence and requeue changed cases.
- **Designing with toy fixtures only.** Test the densest and most awkward real cases before declaring the layout finished.

## 13. Build and usability checklist

Before handing a manual review app to the owner, verify:

- [ ] Each screen asks one clear human question.
- [ ] “Why this needs you” and “What to do” use ordinary domain language.
- [ ] Entity, target period or field, and candidate values are visually prominent.
- [ ] Every page required to interpret the evidence is available in the case.
- [ ] The ordinary workflow fits within a 1366 by 768 viewport without document-level scrolling.
- [ ] The layout also uses a larger desktop viewport effectively rather than adding empty margins.
- [ ] Primary text is readable and important information is not relegated to tiny helper type.
- [ ] Source scan, candidates, decisions, and save action are visible together for ordinary cases.
- [ ] Zoom, pan, fit, page tabs, and source-page shortcuts work.
- [ ] Every common action works by mouse and keyboard.
- [ ] Shortcuts do not interfere with typing.
- [ ] Notes are optional for self-explanatory decisions and required for unresolved or excluded cases.
- [ ] Save, save-and-next, progress, filtering, resumption, and unsaved-change warnings behave predictably.
- [ ] Generated evidence remains immutable and manual decisions are atomic, separate, and history-preserving.
- [ ] Changed evidence makes an earlier decision stale rather than silently reusing it.
- [ ] The server is loopback-only and cannot serve arbitrary local files.
- [ ] Representative real cases have been reviewed visually at laptop and desktop sizes.

