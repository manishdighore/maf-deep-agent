.PHONY: help test lint release bump-patch bump-minor bump-major publish clean

VERSION := $(shell python -c "import re; print(re.search(r'version\s*=\s*\"(.+?)\"', open('pyproject.toml').read()).group(1))")

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

test: ## Run tests
	pytest tests/ -v

lint: ## Run flake8 lint
	flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics

install: ## Install package in editable mode
	pip install -e .

clean: ## Remove build artifacts
	rm -rf dist/ build/ *.egg-info deep_agent/*.egg-info

# ── Version bumping ─────────────────────────────────────────────────

bump-patch: ## Bump patch version (0.1.0 → 0.1.1)
	@python -c "\
	import re, pathlib; \
	p = pathlib.Path('pyproject.toml'); \
	txt = p.read_text(); \
	old = re.search(r'version\s*=\s*\"(.+?)\"', txt).group(1); \
	parts = old.split('.'); \
	parts[2] = str(int(parts[2]) + 1); \
	new = '.'.join(parts); \
	p.write_text(txt.replace(old, new, 1)); \
	print(f'Bumped {old} → {new}')"

bump-minor: ## Bump minor version (0.1.0 → 0.2.0)
	@python -c "\
	import re, pathlib; \
	p = pathlib.Path('pyproject.toml'); \
	txt = p.read_text(); \
	old = re.search(r'version\s*=\s*\"(.+?)\"', txt).group(1); \
	parts = old.split('.'); \
	parts[1] = str(int(parts[1]) + 1); parts[2] = '0'; \
	new = '.'.join(parts); \
	p.write_text(txt.replace(old, new, 1)); \
	print(f'Bumped {old} → {new}')"

bump-major: ## Bump major version (0.1.0 → 1.0.0)
	@python -c "\
	import re, pathlib; \
	p = pathlib.Path('pyproject.toml'); \
	txt = p.read_text(); \
	old = re.search(r'version\s*=\s*\"(.+?)\"', txt).group(1); \
	parts = old.split('.'); \
	parts[0] = str(int(parts[0]) + 1); parts[1] = '0'; parts[2] = '0'; \
	new = '.'.join(parts); \
	p.write_text(txt.replace(old, new, 1)); \
	print(f'Bumped {old} → {new}')"

# ── Release (bump + tag + push → auto-publishes to PyPI) ────────────

release-patch: bump-patch _tag_and_push ## Bump patch + release to PyPI
release-minor: bump-minor _tag_and_push ## Bump minor + release to PyPI
release-major: bump-major _tag_and_push ## Bump major + release to PyPI

_tag_and_push:
	$(eval NEW_VERSION := $(shell python -c "import re; print(re.search(r'version\s*=\s*\"(.+?)\"', open('pyproject.toml').read()).group(1))"))
	git add pyproject.toml
	git commit -m "release v$(NEW_VERSION)"
	git tag v$(NEW_VERSION)
	git push && git push --tags
	@echo "\n✅ Tagged v$(NEW_VERSION) — GitHub Actions will build & publish to PyPI"
