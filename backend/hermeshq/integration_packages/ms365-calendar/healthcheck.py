from hermeshq.services.integration_oauth_checks import test_microsoft_graph_credentials


async def test_connection(config: dict, resolve_secret):
    mailbox = str(config.get("mailbox") or "").strip()
    if not mailbox:
        return False, "Mailbox is required.", None
    return await test_microsoft_graph_credentials(
        config=config,
        resolve_secret=resolve_secret,
        resource_path=f"users/{mailbox}/calendars?$top=1",
    )
