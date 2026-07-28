# API Reference

Every route the API server exposes, generated straight from the running code — not
hand-maintained, so it can't drift out of sync the way a written-by-hand endpoint list
would. This page is rebuilt on every docs deploy by importing the real FastAPI application
and reading its own OpenAPI schema, the same schema `docs_url`/`openapi_url` would serve if
they weren't disabled outside development
(see [Configuration](configuration.md) and [Architecture](architecture.md)).

!!! note "Read-only here"
    "Try it out" is off on this page — it's for reading the shape of the API, not firing
    requests at a live instance from an unauthenticated docs page. To actually call these
    endpoints, point a real client (or your own instance's `/docs`, in `dev`) at your
    deployment.

<swagger-ui src="openapi.json"/>

## See also

- [Configuration](configuration.md) — every setting, in detail
- [Architecture](architecture.md) — how a request moves through the pieces
- [Extending self.ai](extending.md) — Tools, Functions, and Pipelines
- [Mods](mods.md) — the operator-installed extension surface, with its own routes
