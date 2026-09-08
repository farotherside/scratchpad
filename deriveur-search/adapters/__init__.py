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


def detail_cap(site_cfg):
    """Per-site ceiling on detail fetches per run.

    Some boards give us no way to filter server-side, so we crawl them a slice
    at a time. Because fetches are cached, successive runs reach further in and
    steady state costs only the genuinely new listings.
    """
    return site_cfg.get("max_details", 10 ** 9)
