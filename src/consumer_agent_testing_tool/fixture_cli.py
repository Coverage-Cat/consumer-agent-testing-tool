"""Public fixture CLI entrypoint; trusted controls are not commands on this surface."""
from .fixture_service import booking

if __name__ == "__main__":
    booking()
