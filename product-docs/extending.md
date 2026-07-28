# Extending self.ai

self.ai ships one extension surface today: **Tools, Functions, and Pipelines**, inherited
from the Open-WebUI v0.5.4 fork and still the supported way to add behavior to a running
instance. Everything on this page is implemented and running.

A second, more privileged surface — **Mods** — is also shipped. If you are looking for
operator-installed, instance-wide extensions with their own API prefix, live event
namespace, model-callable tools, RBAC scopes, and a native client surface, read
[Mods](mods.md) — unlike Tools and Functions, a mod is installed by an operator, not
authored in the UI by any permitted user.

| Surface | Status | Authored by | Scope |
| --- | --- | --- | --- |
| Tools | Shipped | Any permitted user | Model-callable functions in the chat path |
| Functions (Pipe / Filter / Action) | Shipped | Admin | Chat request/response path |
| Pipelines | Shipped | Admin | External pipeline server |
| Mods | Shipped | Operator (installed, not authored in-UI) | Instance-wide — own routers, live namespace, tools, scopes, and client surface |

---

## How extensions are stored and loaded

Tools and Functions are **not files on disk**. Each one is a Python source blob stored in
the database and executed at load time into a synthetic module.

The loader is `api/selfai_ui/utils/plugin.py`. When an extension is first needed:

1. The source is fetched from the `tool` or `function` table.
2. Import paths are rewritten (see [Import rewriting](#import-rewriting)).
3. The source is `exec()`d into a fresh `types.ModuleType`.
4. The loader looks for a specific class name to decide what it just loaded.

Which class you define determines the extension type:

| Class defined | Loaded as |
| --- | --- |
| `Tools` | A toolkit (model-callable) |
| `Pipe` | A pipe function |
| `Filter` | A filter function |
| `Action` | An action function |

If none of those classes is present, loading fails with `No Tools class found in the
module` or `No Function class found in the module`. When a *function* fails to load, the
loader also sets `is_active = False` on the row, so a broken function disables itself
rather than breaking chat repeatedly.

!!! warning "Extension code runs with full server privileges"
    Tools and Functions are executed with `exec()` inside the API server process. They are
    not sandboxed, and a `requirements:` entry causes `pip install` to run on the server
    (see below). Treat the ability to create them as equivalent to code execution on the
    host, and restrict it accordingly. See [Configuration](configuration.md) for the
    permission settings that govern who can author them.

### Frontmatter

The first line of the source may open a triple-quoted block. Simple `key: value` lines
inside it are parsed as frontmatter and used as metadata:

```python
"""
title: Weather Lookup
author: your-name
version: 0.1.0
requirements: httpx, pytz
"""
```

The parser is deliberately narrow — it only matches lines of the form `key: value`, and
only until the closing `"""`. If the very first line is not `"""`, frontmatter is skipped
entirely and you get an empty metadata dict, silently.

The `requirements:` key is comma-separated and **triggers `pip install` at load time**,
one `pip install <req>` subprocess per entry. In a container this means the packages are
gone on the next restart unless they are baked into the image. Prefer adding real
dependencies to the image over relying on frontmatter installs.

### Import rewriting

Before execution, the loader rewrites four import prefixes so that extensions written
against upstream Open-WebUI module paths still resolve:

| Written | Rewritten to |
| --- | --- |
| `from utils` | `from selfai_ui.utils` |
| `from apps` | `from selfai_ui.apps` |
| `from main` | `from selfai_ui.main` |
| `from config` | `from selfai_ui.config` |

This is a plain string replacement over the whole source, and the rewritten content is
**written back to the database row**. It applies to any occurrence of those substrings,
not just import statements at the top of the file — keep that in mind if your code
contains a string literal like `"from config"`.

---

## Tools

A tool is a class named `Tools` whose public methods become model-callable functions. The
model sees one entry per method, built from the method signature and its docstring.

```python
"""
title: Unit Converter
version: 0.1.0
"""

from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        default_precision: int = Field(
            default=2, description="Decimal places to round results to"
        )

    def __init__(self):
        self.valves = self.Valves()

    def celsius_to_fahrenheit(self, celsius: float) -> str:
        """
        Convert a temperature from Celsius to Fahrenheit.

        :param celsius: The temperature in degrees Celsius.
        :return: The temperature in degrees Fahrenheit.
        """
        result = (celsius * 9 / 5) + 32
        return f"{round(result, self.valves.default_precision)}°F"
```

The description shown to the model comes from the docstring: everything before the first
`:param` or `:return` line is the description, and `:param` lines supply per-argument
descriptions. Write them — a tool with no docstring gives the model nothing to go on.

### Reserved parameters

Parameters whose names begin with `__` are stripped from the spec sent to the model and
injected by the server instead. `__user__`, `__id__`, and `__model__` are among those
populated. Declare them in your signature only if you need them; the server passes only
the ones your signature actually names.

### Known limitations

Two are marked as TODOs in `api/selfai_ui/utils/tools.py` and are worth designing around:

- **The generated spec is a plain dict, not a validated model.** Malformed signatures fail
  late rather than at save time.
- **There is no namespacing on collision.** If two enabled toolkits both define
  `search()`, the second one loaded is discarded with a log warning and the model silently
  never sees it. Give tool methods distinctive names.

---

## Functions

Functions are admin-authored and act on the chat path itself. All three types support
valves the same way tools do.

### Pipe

A `Pipe` presents itself as a model. Define a `pipe` method to handle a request, or a
`pipes` attribute — a list, or a callable returning a list — to register several models
from one function (a "manifold"). Each entry becomes a separately selectable model in the
model picker.

### Filter

A `Filter` intercepts the request on the way in and the response on the way out:

| Hook | When it runs |
| --- | --- |
| `inlet` | Before the request reaches the model. Receives and returns `body`. |
| `outlet` | After the response is assembled. Receives and returns the response data. |

Both may be sync or async — the server inspects the signature and awaits when
appropriate. As with tools, extra parameters are passed only if your signature names them.

Filters run in **priority order**, lowest first, taken from a valve key named `priority`
(default `0`):

```python
class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=0)
```

Setting `file_handler = True` on a filter tells the server that the filter has taken
responsibility for attached files; the server then removes `files` from the request
metadata before dispatch.

### Action

An `Action` adds a button to a message. The client invokes it via
`POST /api/chat/actions/{action_id}`.

### Valves and UserValves

Both classes are optional pydantic models on your extension:

- **`Valves`** — instance-wide configuration, set by an admin.
- **`UserValves`** — per-user configuration, set by each user independently.

They are exposed over the API and rendered as forms in the UI:

```text
GET  /api/v1/functions/id/{id}/valves
GET  /api/v1/functions/id/{id}/valves/spec
POST /api/v1/functions/id/{id}/valves/update
GET  /api/v1/functions/id/{id}/valves/user
POST /api/v1/functions/id/{id}/valves/user/update
```

The same shape exists under `/api/v1/tools/`.

Valve values are stored on the database row and re-applied to the module on each load.
Note that valves are deliberately omitted from the serialized `FunctionModel` /`ToolModel`
read models — read them through the valve endpoints, not off the general model.

---

## Pipelines

Pipelines are a separate integration: rather than running code inside the API server, they
delegate to an **external pipeline server**. `api/selfai_ui/routers/pipelines.py` exposes
upload, add, delete, and valve management against that server. Use pipelines when you want
extension code to run in its own process with its own dependencies, outside the API
container.

Configure the connection in the admin panel under Settings to Pipelines.

---

## Where to author them

| Surface | Location in the UI |
| --- | --- |
| Tools | Workspace to Tools |
| Functions | Admin Panel to Functions |
| Pipelines | Admin Panel to Settings to Pipelines |

Both Tools and Functions support import/export from those pages, which is the practical
way to move an extension between instances or keep it in version control.

---

## What is not available

Be aware of the boundaries before designing an extension:

- **No server-side code sandbox.** Extension code is not isolated from the API server.
  Configuration flags for a sandboxed execution backend (`ENABLE_PISTON_EXECUTION`,
  `PISTON_BASE_URL`) exist and default to off, but no execution path is wired to them yet.
  Until that lands, Tools and Functions run via in-process `exec()` with no isolation —
  enabling the flag today changes nothing.
- **Chat code blocks execute in the browser**, via Pyodide/WASM in a web worker — not on
  the server. A tool cannot use that path to run code.

## See also

- [Mods](mods.md) — the operator-level extension contract, shipped
- [Architecture](architecture.md) — where extensions sit in the request path
- [Configuration](configuration.md) — permissions governing who may author extensions
