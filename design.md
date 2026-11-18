# MidWare design system

The panel is a tool someone works in, not a page someone reads. Everything here
serves scanning: a dense table beats a card grid, a figure with its unit beats a
number in a coloured tile, and one quiet surface beats a stack of boxes.

## Files

| File | Holds |
| --- | --- |
| `midware/static/tokens.css` | every colour, face, size and space step. No literal colour may exist outside this file. |
| `midware/static/base.css` | reset, the vendored face, the primitives the templates speak (`.field`, `.checkbox`, `.radio`, `.switch`, `table`, `.scroll`, buttons), and the CSS-variable aliases `workflow.js` reads. |
| `midware/static/style.css` | the shell and the components, in sections: shell, messages, page furniture, figures, tables, the 52-week scan, conversation viewer, request inspector, premodel presets, MSQ, sign-in, workflow editor, playground. |
| `midware/static/fonts/` | Geologica, latin and latin-ext, self-hosted. Nothing is fetched from a CDN. |

The stylesheet opens with a stamp (what was built, on what theme, what was
waived); keep it true when the system changes.

## Composition rules

- **One page, one primary action.** It sits in the page head, right aligned, and
  it is the only lime fill on the screen. Row-level actions are quiet text.
- **A section is a rule, not a card.** `.mw-section` + `.mw-section-head` gives a
  heading, a `.mw-meta` line and an optional action, closed by a hairline. Do not
  nest boxes: a form inside a section is a `.mw-form-section` (ruled, unboxed).
- **Creating a thing happens in the list.** The add form is the last row of a
  table, behind `details.mw-open-row` (a lime `+`, opens in place). No form is
  wedged between two headings.
- **Editing a row happens in the row.** `details.edit-drawer` opens under it,
  the summary hidden and the panel opened by the row's own Edit action.
- **The credential is masked, the copy is one click.** `.mw-token` shows bullets
  and the last four characters; clicking reveals, `Copy` copies the whole key.
- **Figures are tabular and carry units.** `.num` right-aligns, `.value .u` sets
  the unit a step smaller in muted. A share of a total is a `.bar`, never a pie.
- **A detail page opens with its verdict.** `.mw-figures` is one ruled row of
  figures with a caption under each: what happened, what it cost, how much came
  from cache. It answers the question the page was opened for; everything below
  it is evidence, so the row never folds and never repeats a fold's own table.
- **A fold carries its own digest.** `.mw-fold` + `.mw-fold-head` keeps the key
  figure of its contents in the summary, right aligned. A closed section that
  says nothing is a section nobody opens. The mechanical reader that keeps
  `requests` small pays for it later in clicks.
- **A switch row states its consequence.** The name on the left, one quiet line
  under it saying what turning it off does, the switch right aligned. Siblings
  sit in one ruled list and save through the form they belong to.
- **A monitored model is an open fold over its chart.** One `.mw-fold` per
  recorder, open by default: the summary keeps the name, route, verdict and
  average; the body is a ruled figures row, the chart, and a quiet footer
  holding the schedule and the text actions. The wall of monitors stays a wall.
- **One box per page: the payload.** `.payload` is the only bordered container
  in the inspector, because a body is a document rather than a figure. Inside it
  the syntax colours are tokens (`--color-accent` keys, `--point-hi` figures),
  never literals, and a body over the inline limit says what was left out.
- **A detail row that belongs to the row above it is indented.** `.is-child` on a
  ledger row: cached input is part of the prompt, not a peer of it.
- **Disclosure markers are chevrons.** A `+` means "create" and appears only on
  an add row.
- **Empty states say what belongs here and offer the one action that fills it**
  (`.mw-empty`), and the page-head action is hidden while it is showing.

## Writing rules

- Sentence case everywhere. No eyebrow labels, no tracked capitals, no
  uppercase table heads.
- No middle-dot meta strings: a comma, a colon or a new line instead.
- No arrows glued to link text: the link's verb is the label.
- A missing value reads `none`, never an em dash.
- Time in a list is relative, with the exact stamp in the `title`; in a detail
  view the exact stamp is the value and the age is the caption.

## Accessibility floor

Every control has `:focus-visible` (lime outline, instantly). Every transition is
transform/opacity/colour only and dies under `prefers-reduced-motion`. Contrast is
measured against `--color-paper`, never against a translucent panel. Toast states
(`Copied`, `Copy failed`) come from `mwFlash`, so a copy control always answers.
