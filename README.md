# AI Page Content Enhancer

A Windows desktop app that rewrites the on-page copy of your PHP and HTML files for better SEO using Claude, without touching your markup, scripts, or PHP logic.

Point it at a folder, enter your site URL and a short business description, and it scores each page, rewrites the weak content, re-scores the result, and writes the improved text back into the file.

## Features

- **Markup-safe rewriting.** Claude only sees a flat list of text units (title, meta description, headings, paragraphs, list items, table cells, link and button text, existing image alt text, and content-like PHP strings). It never sees raw HTML or PHP. New text is substituted back into the original file, so tags, attributes, classes, IDs, scripts, and `<?php ... ?>` blocks are never changed.
- **Built-in SEO scorer.** A local, heuristic scorer rates each page from 0 to 100 before and after the rewrite. It uses seven weighted categories:
  - Content Quality
  - AI / LLM Visibility
  - Local SEO Relevance
  - UX / Readability
  - EEAT Signals
  - Conversion Optimization
  - SEO Structure
- **Targeted refinement.** After the first rewrite, up to two refinement passes target the weakest scoring factors. They stop once the target score is reached. The best pass is the one written to disk.
- **Cost control.** Pages that already score 90 or higher skip the API call. Each file has a 120-second budget. Per-file token usage and estimated cost are tracked.
- **Local SEO aware.** It reads `$location` and `$service` PHP variables and reinforces them in the copy.
- **Batch processing.** It handles `.php`, `.html`, and `.htm` files in a folder, and it detects each file's encoding and preserves it.
- **Model choice.** Claude Sonnet 4.6 (recommended), Claude Opus 4.8, or Claude Haiku 4.5, each with a Faster, Balanced, or Highest quality effort level.
- **CSV export.** Export before and after scores, focus keywords, token counts, cost, and time for every file.

## How it works

```
read file -> mask PHP -> parse text units -> score (before)
          -> Claude rewrite -> score (after) -> refine if needed
          -> substitute text back -> write file
```

| File | Purpose |
|---|---|
| `app.py` | Desktop UI (customtkinter) and entry point |
| `processor.py` | Per-file pipeline: read, score, rewrite, refine, write |
| `content_units.py` | Structure-preserving PHP/HTML parser and text substitution |
| `seo_scorer.py` | Heuristic on-page SEO scorer |
| `claude_client.py` | Claude API calls (schema-constrained JSON output) and cost estimation |
| `webview_app.py`, `webui/` | Alternate pywebview and HTML/Tailwind UI (currently disabled) |

## Requirements

- Windows 10 or 11
- Python 3.10 or newer
- An [Anthropic API key](https://console.anthropic.com/)

## Setup

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

## Usage

1. Enter your Anthropic API key. It can be remembered locally.
2. Choose the folder that contains your pages and the file type (PHP, HTML, or Both).
3. Enter the site URL and a short description of the business, its services, and its service area.
4. Pick a model and effort level, then start the run.
5. Review the before and after scores, and optionally export the results to CSV.

> **Warning:** Files are rewritten **in place** and no backup is made. Run the app on a copy of your site, or on a folder under version control, and review the changes before you deploy.

## Building the .exe

```bat
build_exe.bat
```

This runs PyInstaller (`--onefile --windowed`) and puts the executable in `dist\`. Install PyInstaller first with `pip install pyinstaller`.

## Configuration and security

Settings are saved to `enhancer_config.json` next to the app, including the API key if you tick "remember". **This file is git-ignored. Never commit it.** See `enhancer_config.example.json` for the format.

## Tech stack

Python, customtkinter, Pillow, Anthropic Python SDK, PyInstaller (pywebview optional).

## License

Add a license of your choice (for example MIT) before publishing.
