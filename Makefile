UV ?= uv
UV_RELEASE_VERSION := uv 0.12.5 (x86_64-unknown-linux-gnu)
UV_RELEASE_SHA256 := b65f23a420c4acc96427efb30e5ed9bc0f7e25d2d712000f6ede77c1a0de5f46
SYSTEM_PYTHON := /usr/bin/python3
VENV_PYTHON := .venv/bin/python
BIRTH_PATHS := .gitignore .python-version CHANGELOG.md LICENSE Makefile NOTICE \
	PUBLICATION.md README.md THIRD-PARTY-NOTICES.md VERSION pyproject.toml \
	src/kilix_image_shop/__init__.py src/kilix_image_shop/py.typed \
	tests/test_identity.py uv.lock

.DEFAULT_GOAL := check

.PHONY: setup lock-check test build legal-check hygiene-check check

define verify_uv
uv_path="$$(command -v "$(UV)")"; \
test -n "$$uv_path" || { printf '%s\n' 'uv executable not found' >&2; exit 1; }; \
actual_version="$$("$$uv_path" --version)"; \
test "$$actual_version" = "$(UV_RELEASE_VERSION)" || { \
	printf 'uv version mismatch: expected %s; got %s\n' \
		'$(UV_RELEASE_VERSION)' "$$actual_version" >&2; exit 1; }; \
actual_sha256="$$(sha256sum "$$uv_path" | cut -d ' ' -f 1)"; \
test "$$actual_sha256" = "$(UV_RELEASE_SHA256)" || { \
	printf 'uv digest mismatch: expected %s; got %s\n' \
		'$(UV_RELEASE_SHA256)' "$$actual_sha256" >&2; exit 1; }
endef

setup:
	@set -eu; $(verify_uv)
	@if [ ! -x "$(VENV_PYTHON)" ]; then \
		$(UV) venv --system-site-packages --python "$(SYSTEM_PYTHON)" \
			--no-python-downloads .venv; \
	fi
	@grep -qx 'include-system-site-packages = true' .venv/pyvenv.cfg
	@VIRTUAL_ENV="$(CURDIR)/.venv" $(UV) sync --active --frozen --offline \
		--no-python-downloads --all-groups

lock-check:
	@set -eu; $(verify_uv)
	@$(UV) lock --check --offline --no-python-downloads

test: setup
	@$(VENV_PYTHON) -I -m unittest discover -s tests -v

build: setup
	@VIRTUAL_ENV="$(CURDIR)/.venv" $(UV) build --no-build-isolation --offline \
		--no-python-downloads

legal-check: build
	@set -eu; \
	wheel="$$(find dist -maxdepth 1 -type f -name '*.whl' -print -quit)"; \
	sdist="$$(find dist -maxdepth 1 -type f -name '*.tar.gz' -print -quit)"; \
	test -n "$$wheel"; test -n "$$sdist"; \
	for carrier in LICENSE NOTICE THIRD-PARTY-NOTICES.md; do \
		unzip -Z1 "$$wheel" | grep -Eq "/licenses/$$carrier$$"; \
		tar -tzf "$$sdist" | grep -Eq "/$$carrier$$"; \
	done

hygiene-check:
	@set -eu; \
	$(SYSTEM_PYTHON) -I tools/check_remote_urls.py; \
	for path in $(BIRTH_PATHS); do git ls-files --error-unmatch "$$path" >/dev/null; done; \
	if git rev-parse --verify HEAD >/dev/null 2>&1; then \
		command -v hygiene-scan >/dev/null 2>&1 || { \
			printf '%s\n' 'hygiene-scan not found on PATH; see REVIEW-HANDOFF.md' >&2; \
			exit 127; \
		}; \
		hygiene-scan; \
	else \
		git diff --cached --check; \
		test "$$(git config user.name)" = itsmygithubacct; \
		test "$$(git config user.email)" = \
			itsmygithubacct@users.noreply.github.com; \
		pattern='clau''de|anth''ropic|co-''authored|gh''p_|gh''o_|oauth_''token|@gm''ail|/home/''pleb|research/gpu_''terminal'; \
		rc=0; git grep --cached -niE "$$pattern" -- >/dev/null 2>&1 || rc=$$?; \
		test "$$rc" -eq 1; \
	fi

check: lock-check test build legal-check hygiene-check
