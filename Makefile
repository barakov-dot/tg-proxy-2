.PHONY: installer-containers lint
# Same check as the CI job `installer-containers` (needs docker).
installer-containers:
	for image in ubuntu:24.04 debian:13; do \
	  docker run --rm -v "$$PWD:/src:ro" $$image bash -ec ' \
	    apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null; \
	    set +e; bash /src/install.sh --panel-domain panel.example.com \
	      --bot-token 123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI --admin-id 1; rc=$$?; \
	    [ $$rc = 1 ]' || exit 1; \
	done

lint:
	shellcheck install.sh scripts/*.sh
