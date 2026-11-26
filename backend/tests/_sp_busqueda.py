def adaptar_busqueda(fake):
    def _graph(method, path, token, payload=None, **kwargs):
        if str(path).startswith("/me/drives"):
            return {"value": [{"id": "drv-1", "name": "Docs"}]}
        if str(path).startswith("/drives/") and "/root/search(" in str(path):
            resp = fake("POST", "/search/query", token, payload)
            hits = [
                h["resource"]
                for c in (resp or {}).get("value", [])
                for hc in c.get("hitsContainers", [])
                for h in hc.get("hits", [])
            ]
            return {"value": hits}
        return fake(method, path, token, payload, **kwargs)

    return _graph
