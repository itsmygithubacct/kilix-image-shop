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

.PHONY: setup lock-check test build legal-check hygiene-check check h0-core-check

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

# The identity and leak-pattern checks used to sit in an `else` branch guarded
# by `git rev-parse --verify HEAD`, which succeeds in any repository holding a
# commit. They could therefore never run here, and a staged file containing
# /home/''pleb and research/gpu_''terminal -- both of which hygiene-scan permits
# and this repository does not -- passed hygiene-check at exit 0. They now run
# unconditionally, alongside hygiene-scan rather than instead of it.
hygiene-check: identity-check leak-pattern-check
	@set -eu; \
	$(SYSTEM_PYTHON) -I tools/check_remote_urls.py; \
	for path in $(BIRTH_PATHS); do git ls-files --error-unmatch "$$path" >/dev/null; done; \
	command -v hygiene-scan >/dev/null 2>&1 || { \
		printf '%s\n' 'hygiene-scan not found on PATH; see REVIEW-HANDOFF.md' >&2; \
		exit 127; \
	}; \
	hygiene-scan; \
	git diff --cached --check

# Checked on the commits themselves, not on `git config`. git config is the
# local operator's setting and says nothing about what is already recorded.
identity-check:
	@set -eu; \
	want='itsmygithubacct <itsmygithubacct@users.noreply.github.com>'; \
	total=$$(git rev-list --count HEAD); \
	bad=$$(git log --format='%an <%ae>%n%cn <%ce>' | sort -u | grep -vxF "$$want" || true); \
	if [ -n "$$bad" ]; then \
		printf 'identity refusal: %s of %s commits carry a non-conforming author or committer:\n' \
			"$$(git log --format='%H %an <%ae>%n%H %cn <%ce>' | grep -vF "$$want" | cut -d' ' -f1 | sort -u | wc -l)" "$$total" >&2; \
		printf '  %s\n' "$$bad" >&2; \
		exit 1; \
	fi; \
	printf 'identity: %s of %s commits conform on author and committer\n' "$$total" "$$total"

# hygiene-scan permits /home/''pleb and research/gpu_''terminal; this repository
# does not, so the stricter patterns are checked here. The pattern is written as
# adjacent string fragments so this file does not match itself.
leak-pattern-check:
	@set -eu; \
	pattern='clau''de|anth''ropic|co-''authored|gh''p_|gh''o_|oauth_''token|@gm''ail|/home/''pleb|research/gpu_''terminal'; \
	found=''; \
	for scope in --cached ''; do \
		hits=$$(git grep -niE $$scope "$$pattern" -- . || true); \
		if [ -n "$$hits" ]; then found="$$found$$hits\n"; fi; \
	done; \
	if [ -n "$$found" ]; then \
		printf 'leak-pattern refusal: %s tracked line(s) match a forbidden pattern:\n' \
			"$$(printf '%b' "$$found" | sort -u | grep -c . )" >&2; \
		printf '%b' "$$found" | sort -u | sed 's/^/  /' >&2; \
		exit 1; \
	fi; \
	printf 'leak-pattern: 0 tracked lines match the %s forbidden patterns\n' \
		"$$(printf '%s' "$$pattern" | tr '|' '\n' | grep -c .)"

check: lock-check test build legal-check hygiene-check

h0-core-check:
	@$(SYSTEM_PYTHON) -I tools/verify_h0_capacity.py
	@$(MAKE) --no-print-directory UV="$(UV)" check
