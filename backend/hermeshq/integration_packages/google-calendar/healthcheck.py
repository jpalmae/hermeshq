from hermeshq.services.integration_oauth_checks import test_google_oauth_credentials


async def test_connection(config: dict, resolve_secret):
    calendar_id = str(config.get("calendar_id") or "").strip() or "primary"
    return await test_google_oauth_credentials(
        config=config,
        resolve_secret=resolve_secret,
        resource_url=f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}",
    )
