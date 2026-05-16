# Build + publish the patchvec → pavedb shim.
# One-shot: publish once, never re-publish unless pavedb breaks the shim.

.PHONY: build publish-test publish clean

build: clean
	python3 -m build

publish-test: build
	twine upload --repository testpypi dist/*

publish: build
	@echo "About to upload patchvec shim to REAL PyPI."
	@read -p "Type 'yes' to confirm: " ans; [ "$$ans" = "yes" ] || exit 1
	twine upload dist/*

clean:
	rm -rf build dist patchvec.egg-info
