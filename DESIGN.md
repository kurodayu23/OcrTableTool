# OCR Table Tool Design Notes

## Product intent

This is an operational desktop work surface for converting one imperfect phone photo of a paper or screen-based table into trustworthy editable data. The first screen must make the workflow obvious: open photo, recognize, review uncertain cells, export.

## Layout

- Left: large original/rectified photo preview on a neutral canvas, scaled without distortion.
- Right: editable grid using available space; headers remain visible while scrolling.
- Top toolbar: product name and primary workflow actions in one compact row; no title card.
- Bottom status bar: current task state, progress, review count, and error detail.

## Visual system

- Surface colors: `#F4F7FB` canvas, `#FFFFFF` work surfaces, `#D9E2EF` borders.
- Text colors: `#172033` primary, `#667085` secondary.
- Accent: `#2563EB` for the primary recognition action and focused states.
- Status: `#0EA5A8` success, `#B45309` warning, `#B42318` failure.
- Low-confidence cells: pale amber `#FFF4D6`, never red unless recognition failed entirely.
- Spacing scale: 4, 8, 12, 16, 24 pixels. Borders are thin; shadows are avoided.
- Typography: system UI font, 13 px body, 12 px metadata, 15-16 px section and empty-state titles.

## Interaction rules

- Import accepts common image formats and never modifies the source photo.
- Table cells are directly editable. Structural actions operate on the current selection and live in the table overflow menu.
- Recognition uncertainty is visible but does not block export.
- Destructive queue/table actions require a clear selection and remain undoable where practical.
- Empty, loading, success, warning, and failure states use concise Chinese text.
