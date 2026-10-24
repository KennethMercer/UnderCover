# Themes: the contract and the file

A theme is a grading of the Case File look plus a type change: tokens only. It never changes layout, ids, hooks, copy
or the Deep Cover marking, so a shared theme can't break a page or hide anything. The WinAmp skin is its own thing.

Anyone can make and share one: the maker in Settings takes minutes, and a theme travels as one file, `Name.uctheme`.
Theming is Deep Cover: the Theme row, the maker, Import and Export are `data-deep`. Basic Settings keeps Look (Follow
Windows / Dark / Light) only. A theme still applies in the basic view once chosen.

Five themes ship, each a starting point: Case File (the look itself: Schibsted Grotesk, JetBrains Mono), Midnight
(Manrope, DM Mono), Dossier (Figtree, IBM Plex Mono), Gaslight (Hanken Grotesk, Red Hat Mono) and Carbon (Geist, Geist
Mono).

## The folder

```
themes/<id>/
  theme.json      name, author, licence, version, "modes": ["dark", "light"], "based_on", "fonts": [...]
  theme.css       token overrides only (below), plus @font-face for the fonts in fonts/
  fonts/          woff2 files the theme bundles, each family with its OFL text; optional
  preview.png     the swatch card (made by the maker; optional for hand-made themes)
```

`Name.uctheme` is this folder zipped. `<id>` is a slug (`[a-z0-9-]{2,40}`); the zip's top folder names it. Shipped
themes live in `undercover/web/themes/`; the user's own and imported ones in the library group (`<library>/themes/`,
which SafeHouse's Storage room knows about). Import refuses anything but `.css .json .woff2 .png .txt`, a zip over 4 MB,
a path that climbs out, and a `theme.css` that fails the check below. Nothing in a theme executes.

## theme.css: the token contract

Exactly two rule blocks are allowed, each holding only custom properties from this list, and `@font-face` rules whose
`src` is a relative `fonts/...` url. Anything else (other selectors, `url(http…)`, `@import`) is refused at import and
at load. The shipped themes are checked the same way by `tests/smoke.py`.

```css
:root[data-uc-theme="<id>"] { ... }                       /* dark */
:root[data-uc-theme="<id>"][data-theme="light"] { ... }   /* light; may be omitted: the page derives it (below) */
```

| Group | Tokens (app.css `:root`) | Notes |
|---|---|---|
| the air | `--base` `--panel-base` `--panel-2-base` `--panel-3-base` `--line-base` | the near-blacks (or near-whites) everything is mixed into |
| words | `--fg` `--muted` `--accent-ink` | `--fg` at least 7:1 and `--muted` at least 4.5:1 on `--panel` |
| the four | `--c-frontman` `--c-doubleagent` `--c-speakeasy` `--c-safehouse` and their `-deep` | smoked tone for words and lines (at least 4.5:1 on `--panel`), deep tone for the air |
| states | `--ok` `--warn` `--err` | at least 4.5:1 as text on `--panel` |
| waves | `--wave` `--wave-on` `--wave-alt` | |
| type | `--display` `--body` `--mono` | families only; sizes and weights are the design's |
| shape | `--r` | 6 to 12 px; the look derives the smaller radii |
| the lamp | `--lit` `--tint` `--tint2` `--lamp` | percentages: how much of a module's deep tone reaches surfaces, the page and the lamp |
| motion | `--t-fast` `--t-slow` | optional; reduced-motion still wins |

Not themeable: spacing, the type scale, shadows, the Deep Cover marks, the strip's line, and anything in `look.css` by
selector.

## Derivation (the maker, and a theme that gives less)

The maker lets a person pick one colour per module and get a whole theme. Everything is in OKLCH (lightness L, chroma
C, hue h); the rules were measured on Case File's and Midnight's hand-made tones, and `themes.js` implements them:

| From | Gives | Rule |
|---|---|---|
| a module colour (dark) | its deep tone | same h; L 0.36 (0.46 for yellows and browns, h 50 to 115°); C × 1.3, at most C + 0.03 |
| a module colour (dark) | its light-mode colour | same h; L 0.42; C × 1.25 + 0.002, at most 0.13 |
| a module colour (dark) | its light-mode deep tone | same h; L 0.58; C × 1.3 + 0.002, at most 0.15 |
| the air (dark `--base`) | the dark panels, line, wave | same h; L + 0.037 / 0.073 / 0.111, line + 0.125, wave + 0.17 |
| the air (dark `--base`) | the light air | same h, C × 2 + 0.003; L 0.926 base, 0.971 / 0.932 / 0.892 panels, 0.855 line, 0.83 wave |
| the lamp (dark) | the light lamp | `--lit` × 0.55, `--tint` × 0.55, `--tint2` × 0.4, `--lamp` × 0.65 |

Text, muted, states and button ink in light mode come from the theme the maker started from (or Case File's). In the
maker a change only re-derives what follows from it, so a start theme's hand-tuned light mode stays; its Light mode
panel lets Deep Cover change any of it by hand (marked "yours"). A hand-made `theme.css` may give only the dark block;
the page works out the light one at load with the same rules. Type, corners and motion live in the dark block only
(they apply in both modes). The maker warns ("hard to read") when a colour is under 4.5:1 on the panel.

## The page

- `html[data-uc-theme="<id>"]` is set before first paint (index.html's head script, from `localStorage uc-theme-id`),
  and `<link id="uc-theme-css">` points at the theme's `theme.css` (`/static/themes/<id>/theme.css` for shipped,
  `/api/themes/<id>/theme.css` for the library's). No theme attribute means Case File, with no extra stylesheet.
- Settings (Deep Cover): the Theme row with swatch cards (`preview.png`, else drawn from the tokens), the one in use
  marked; Import a theme; Export; Make one, which opens the maker in place (name and author, start from, the air and
  lamp, the four wells, text and numbers families with "Add a font", corners, "Make the light version for me"; live
  preview; Save as a theme; Share as a file). The maker writes the tokens itself; the server only stores the folder and
  zips it.
- A theme applies in the basic view too; Deep Cover is only where it's chosen or made.
- Fonts: the families above ship (OFL), so most themes bundle none; a theme may bundle its own OFL family.

## API

`GET /api/themes` (shipped and library, with `theme.json` and whether it checks out); `POST /api/themes` (save a folder
from the maker: json, css and optional fonts and preview); `POST /api/themes/import` (a `.uctheme`, checked);
`GET /api/themes/<id>/export` (the zip); `GET /api/themes/<id>/theme.css` and `/fonts/<file>`; `DELETE /api/themes/<id>`
(library themes only, with the guard). A CSP on the page allows styles and fonts from the app's own origin only.
