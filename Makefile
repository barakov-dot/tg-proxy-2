.PHONY: installer-containers lint
# Same check as the CI job `installer-containers` (needs docker).
installer-containers:
	for image in ubuntu:24.04 debian:13; do \
	  docker run --rm --platform linux/amd64 -v "$$PWD:/src:ro" $$image \
	    bash /src/scripts/installer-container-check.sh || exit 1; \
	done

lint:
	shellcheck install.sh scripts/*.sh
	shellcheck -s sh deploy/tgpanel-cli
