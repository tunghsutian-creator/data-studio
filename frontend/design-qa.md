# Design QA

- Visual source of truth: `design/ui-concept.png` (1600 × 1000).
- Desktop render: `design/qa-desktop.png` (1600 × 1000 viewport).
- Mobile render: `design/qa-mobile.png` (390 × 844 viewport).
- Browser: local Microsoft Edge driven by Playwright Core.
- Data source: live FastAPI + SQLite catalog at `127.0.0.1:8765`.

The bundled in-app browser client could not initialize on this machine because
of a Node runtime incompatibility (`Cannot redefine property: process`). The QA
was therefore repeated in the installed Edge browser, not skipped.

## Fidelity ledger

1. **App frame** — the 208 px navigation rail, 64 px command bar, central
   workspace and persistent right inspector reproduce the concept's four-part
   frame. At narrow width the navigation becomes a 64 px icon rail and the
   inspector becomes the primary overlay.
2. **Information density** — compact summary cells, 44–46 px table rows, thin
   confidence bars, restrained borders and a short pipeline footer match the
   source's desktop-workstation density without card or marketing treatments.
3. **Inspector anatomy** — grouped files, original path, canonical-name proposal,
   predicted metadata, SHA-256 state, evidence, confidence and three review
   actions appear in the same order as the concept.
4. **Visual tokens** — off-white canvas, white surfaces, teal primary actions,
   pale-blue selection, green integrity state and amber review state match the
   accepted palette. No gradients, glass effects or decorative charts were
   introduced.
5. **Real-data copy differences** — concept placeholders such as 2,842 datasets
   and 1.82 TB were correctly replaced by the live corpus values (452 grouped
   datasets and 2.29 GB). Long generated names and Windows paths truncate with
   full values retained in the inspector/title attributes.
6. **Responsive behavior** — at 390 px the icon navigation, search and accessible
   import action remain usable; the inspector fits without document-level
   horizontal overflow.

## Functional verification

- Production build: passed; Vite transformed 4,577 modules.
- Live records loaded: all 452 from the API, with 20 rendered per page.
- Live storage total visible: 2.29 GB.
- Real pagination advanced to page 2 and returned to page 1.
- Global search for the two source screenshots produced exactly 2 rows and
  `共 2 条`.
- The initially selected row was visible, and its detail request populated a
  real SHA-256 digest plus classifier/context evidence.
- Import dialog opened and closed.
- Database → review showed exactly the 2 low-confidence records; rules showed
  12 locked built-in switches; navigation then returned to the database route.
- Desktop horizontal overflow: none.
- Mobile horizontal overflow: none.
- Mobile import action: visible and accessibility-named.
- Browser console errors: none.
- Failed browser requests: none.

## Remaining intentional deviations

- The concept uses polished sample IDs; the real corpus currently produces
  conservative `UNASSIGNED/UNKNOWN` tokens where source metadata is absent.
- The desktop QA selects the newest real dataset rather than the concept's
  fictional impact-test row.
- The local corpus has no preview thumbnails in this release because plotting
  and image-figure management are explicitly out of scope.

Final result: passed.
