# sortandpost.photos brand kit

Rendered from the two source SVGs by `tools/logo/render.py` — don't edit these by hand;
change the SVGs and re-run it (`uv run python tools/logo/render.py <light.svg> <dark.svg>`).
The app bundles the same files in `src/sortandpost/resources/logo/`.

| File | Use |
|---|---|
| `logo-light.svg` / `logo-dark.svg` | The logo for light / dark backgrounds (909×1080, portrait) |
| `favicon.svg` | Adaptive tab icon: swaps to the dark logo when the browser is dark (square) |
| `favicon.ico` | 16 / 32 / 48 px, for old browsers and `/favicon.ico` requests |
| `favicon-32.png` | PNG fallback (Safari before SVG favicons) |
| `apple-touch-icon.png` | 180×180, iPhone / iPad home screen (opaque; iOS rounds the corners) |
| `icon-192.png` / `icon-512.png` | Web app manifest / Android |
| `app-icon-1024.png` | The desktop app icon (white rounded tile, macOS icon-grid margin) |

For the website's `<head>`:

```html
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/favicon-32.png" type="image/png" sizes="32x32">
<link rel="icon" href="/favicon.ico" sizes="48x48">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
```

And a logo that follows the visitor's theme:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="/logo-dark.svg">
  <img src="/logo-light.svg" alt="sortandpost.photos" height="64">
</picture>
```
