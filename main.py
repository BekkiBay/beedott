"""MeetMind v3 — точка входа демона. Эквивалент `python -m daemon.cli daemon`."""
from daemon.cli import main

if __name__ == "__main__":
    main(["daemon"])
