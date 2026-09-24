# msgraph-simple

Plug-and-play Microsoft Graph from Python, over a native C#/.NET core.

```python
from msgraph_simple import GraphClient

with GraphClient.app_only(tenant_id=..., client_id=..., client_secret=...) as g:
    for user in g.paged("/users", select="id,displayName,mail"):
        print(user["mail"])
```

Zero dependencies — `ctypes`, `json`, `secrets`, `webbrowser` and `http.server`, all standard
library. The wheel bundles the compiled core, so nothing else needs installing.

Full documentation is in the repository's `README.md`, and the design is in `ARCHITECTURE.md`.
