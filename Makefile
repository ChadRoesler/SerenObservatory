# Local dev helper - mirrors the CI matrix leg.
# Requires: make (Git for Windows ships it; or: choco install make / scoop install make)
#
# Targets:
#   make test         - install dev extras and run pytest
#   make build        - produce wheel + sdist in SerenObservatory/dist/
#   make clean        - remove venv and dist artifacts

SHELL        := pwsh.exe
.SHELLFLAGS  := -NoProfile -NonInteractive -Command

PKG_DIR    := SerenObservatory
VENV       := .venv

.PHONY: test build clean

test:
	Remove-Item -Recurse -Force $(VENV) -ErrorAction SilentlyContinue; python -m venv $(VENV); .\.venv\Scripts\pip.exe install -e "$(PKG_DIR)/.[dev]"; $$status=$$LASTEXITCODE; if ($$status -eq 0) { .\.venv\Scripts\python.exe -m pytest $(PKG_DIR)/tests/ -v; $$status=$$LASTEXITCODE }; Remove-Item -Recurse -Force $(VENV) -ErrorAction SilentlyContinue; exit $$status

build:
	python -m pip install build --quiet; python -m build $(PKG_DIR)/; exit $$LASTEXITCODE

clean:
	Remove-Item -Recurse -Force $(VENV), $(PKG_DIR)/dist, $(PKG_DIR)/seren_observatory/_version.py -ErrorAction SilentlyContinue

