"""
Small string helpers. `convert_entity_uri_to_label` is a direct port of the
original Python 2 function — same behavior, kept intentionally dumb (no
SPARQL lookup needed) since a redirect/disambiguation source's "label" was
always derived from its own resource-local-name, never fetched separately.
"""


def convert_entity_uri_to_label(local_name: str) -> str:
    return local_name.replace("_", " ").replace(" (disambiguation)", "").lower()
