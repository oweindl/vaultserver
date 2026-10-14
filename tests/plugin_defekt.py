"""Test-Plug-in, dessen register scheitert."""


def register(ctx):
    raise RuntimeError("Absicht: register schlägt fehl")
