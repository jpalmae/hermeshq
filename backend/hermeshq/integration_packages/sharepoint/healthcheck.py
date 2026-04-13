from hermeshq.services.integration_oauth_checks import parse_sharepoint_site_url, test_microsoft_graph_credentials


async def test_connection(config: dict, resolve_secret):
    site_url = str(config.get("site_url") or "").strip()
    parsed = parse_sharepoint_site_url(site_url)
    if not parsed:
        return False, "A valid SharePoint site URL is required.", None
    host, path = parsed
    return await test_microsoft_graph_credentials(
        config=config,
        resolve_secret=resolve_secret,
        resource_path=f"sites/{host}:/{path}",
    )
