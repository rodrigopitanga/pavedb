# patchvec → pavedb shim

This is a transitional package. The project formerly known as `patchvec` is
now [`pavedb`](https://github.com/rodrigopitanga/pavedb).

Installing `patchvec` pulls in `pavedb` automatically. The Python import
name has always been `pave`, so existing code using `from pave import ...`
continues to work unchanged.

**Action for users**: replace `patchvec` with `pavedb` in your requirements
file.

This shim will not receive further updates.
