"""Optional provider adapters registered with the benchmark runner.

Each adapter module exposes ``SLUG``, ``NAME``, ``REQUIRED_ENV``,
``MIN_START_INTERVAL_SECONDS``, ``request``, ``not_found_reason``, and
``normalize``. Adding a challenger requires one adapter module and one entry
in this registry; the benchmark and smoke-test runners do not need
provider-specific request or normalization code.
"""

from funding.adapters import fundable


ADAPTERS = {
    fundable.SLUG: fundable,
}
