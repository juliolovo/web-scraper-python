# Agent Instructions

Before changing this repository, read `README.md` and follow the documented project workflow.

When a change affects setup, dependencies, commands, scraper behavior, output schema, generated artifacts, supported brands, or fallback strategy, verify whether `README.md` must be updated. If it must be updated, update it in the same change and mention that in the final response.

Use the project-local virtual environment only:

- Create it at `.venv`.
- Run project commands with `.\.venv\Scripts\python.exe`.
- Do not use global Python environments or virtual environments outside this repository for validation.

Scraper output should preserve a common product envelope while allowing brand-specific data in `raw_brand_data`. Keep extracting in this priority order:

1. APIs or structured data discovered from HTML with BeautifulSoup.
2. Static HTML with BeautifulSoup.
3. Playwright-rendered DOM only when the previous sources do not expose the needed data.
