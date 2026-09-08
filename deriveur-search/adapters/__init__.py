"""Per-site adapters.

Each adapter exposes:   collect(site_cfg, ctx) -> (listings, site_report)

`ctx` provides .fetch(url), .cfg, .log(msg), .budget_ok().
A listing is a plain dict; see store.LISTING_FIELDS for the canonical shape.
Adapters should NOT filter on criteria -- they only extract. Filtering and
scoring happen once, centrally, in criteria.assess().
"""
import importlib

def load(name):
    return importlib.import_module(f"adapters.{name}")
