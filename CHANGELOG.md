# Changelog

## 3.0

Full rewrite.

### Fixed
- Notes written in dark mode were unreadable in light mode. `QTextEdit.toHtml()`
  bakes the current palette into saved HTML, so `color:#e0e0e0` travelled with
  every note. Colours are now stripped before saving and re-guarded on load.
- Saving an existing note raised `database disk image is malformed`. `COUNT(*)` on
  an external-content FTS5 table reads the source table rather than the index, so
  the "rebuild if empty" check never fired for anyone with existing notes and the
  search index stayed empty. Staleness is now measured against the FTS shadow
  table, and writes rebuild a broken index automatically.
- A failed save re-fired the autosave timer immediately, producing an endless
  wall of identical tracebacks.
- "No Folder" could never clear a note's folder: the update used
  `if value is not None`, so `None` meant "leave alone".
- Attachment sizes in the browser email fallback were nonsense — the attachments
  tuple was unpacked with `file_size` and `file_type` swapped.
- `import win32com.client` at module scope made pywin32 mandatory just to launch.
- Per-note editor colour was never saved; the code had a `pass` where the write
  should have been.
- Ghost widgets lingered over the sidebar because they were deleted but left
  parented.
- Every database call opened its own connection with no WAL and no indexes.
- Autosave rebuilt the entire note list on every keystroke pause.

### Added
- Command palette, FTS5 search, find & replace
- Folder and tag browsing with live counts; real tag management
- Checklists, headings, quotes, code blocks, table row/column editing
- Export to PDF, HTML, Markdown, plain text; zip backup
- Drag and drop to attach; note duplication; sort and density options
- Accent colours, system theme following, session restore
- Database repair from Settings, and a crash log at `%APPDATA%\NoteCraft\error.log`

### Changed
- Icons and the app icon are drawn with `QPainter` instead of shipped as files,
  so the app has no external assets at all
- One token set drives the whole interface; every colour pair meets WCAG AA
