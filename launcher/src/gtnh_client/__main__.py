"""`python -m gtnh_client`: the `gtnh-client` command without its entry-point executable, which Windows keeps locked
while it runs (`gtnh-client serve` started that way would stop `uv run` from reinstalling the project)."""

import sys

from gtnh_client.cli import main

sys.argv[0] = "gtnh-client"
main()
