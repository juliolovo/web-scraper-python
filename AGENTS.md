# Agent Instructions

Before changing this repository, read `README.md` and follow the documented project workflow.

When a change affects setup, dependencies, commands, scraper behavior, output schema, generated artifacts, supported brands, or fallback strategy, verify whether `README.md` must be updated. If it must be updated, update it in the same change and mention that in the final response.

Use the project-local virtual environment only:

- Create it at `.venv`.
- Run project commands with `.\.venv\Scripts\python.exe`.
- Do not use global Python environments or virtual environments outside this repository for validation.

Scraper output should preserve a clean common product envelope. Do not duplicate raw scraped payloads in product output; put inspection details, source URLs, and asset provenance in `logs_execution/{brand}-log-execution.json`. Keep extracting in this priority order:

1. APIs or structured data discovered from HTML with BeautifulSoup.
2. Static HTML with BeautifulSoup.
3. Playwright-rendered DOM only when the previous sources do not expose the needed data.
## Output And Execution Logs

- Read `README.md` before changing scraper behavior.
- Clean product output goes in `output/data/{brand}/{brand}.json`.
- Execution traces go in `logs_execution/{brand}-log-execution.json` and are
  overwritten on every run.
- Use `assets.images`, `assets.documents`, `assets.video`, and `assets.links` in
  execution logs. Do not write `assets.media`.
- Download images only unless the user explicitly asks to download videos,
  documents, or external link targets.
