"""Test-suite default: run the router in explicit open mode.

szl-router now fails closed when SZL_ROUTER_TOKEN is unset. Existing tests
exercise endpoints without a token, so the suite opts in to open mode here;
tests that check the fail-closed path delete this variable themselves.
"""
import os

os.environ.setdefault("SZL_ROUTER_ALLOW_NO_AUTH", "1")
