---
name: Infographic PDF regeneration
description: How to rebuild the moderation infographic PDF from the HTML source.
---

The moderation study is an HTML infographic (`infografico.html`, mirrored to `index.html` which the static workflow serves). The PDF deliverable is `Infografico_Moderacao_Bot.pdf`.

Regenerate with `python3 generate_pdf.py`. It uses Playwright Chromium and renders a single continuous page (width 1240px, height = full scrollHeight, `print_background=True`) so the dark gradient and emoji are preserved.

**Why:** a normal print would paginate into Letter pages and drop the background. Single-tall-page keeps it as one poster.

**How to apply:** if you edit `infografico.html`, run `cp infografico.html index.html` then `python3 generate_pdf.py`. Playwright python pkg is pip-installed and its Chromium via `python3 -m playwright install chromium`; the Chromium system libs are already in `replit.nix`.
