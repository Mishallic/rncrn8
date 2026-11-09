"""`python -m rncrn8`: the command line, run by a known interpreter. The assistant connection starts the
server this way, so it does not depend on what is on PATH when the assistant launches it."""

from .cli import main

main(prog_name="rncrn8")
