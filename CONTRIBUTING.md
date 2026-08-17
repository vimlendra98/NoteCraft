# Contributing

Thanks for taking a look.

## Ground rules

- **One file.** NoteCraft is deliberately a single `notecraft.py`. Please keep it
  that way.
- **No new runtime dependencies.** PyQt6 and the standard library only. pywin32
  stays optional — the app must run without it.
- **No asset files.** Icons are drawn with `QPainter`. If you need a new one, add
  a `_icon_<name>` method to the `Icons` class.

## Before opening a pull request

```bash
python -m pyflakes notecraft.py
python -m py_compile notecraft.py
```

CI runs both and will fail the build otherwise.

## Touching colours

Never hard-code a hex value in a widget. Every colour comes from the `Theme` token
set so that theme switching cannot strand part of the interface. If you add a
token, check both themes clear 4.5:1 contrast against the surfaces it sits on —
`contrast_ratio()` is already in the file.

## Touching saved content

Anything that writes to `content_html` must go through
`ContentPipeline.sanitize_for_storage()`, and anything that loads it must go
through `retheme_document()`. Skipping either one reintroduces the unreadable-text
bug that motivated the rewrite.

## Testing

There is no test suite in the repo yet. At minimum, verify by hand:

1. Write a note in dark mode, switch to light, confirm it is still readable.
2. Switch back and confirm the colours return rather than drifting further.
3. Open the app against a database that already has notes in it — several bugs
   have only ever appeared on a populated database, never an empty one.
