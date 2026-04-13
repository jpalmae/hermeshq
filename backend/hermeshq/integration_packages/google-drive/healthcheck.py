from hermeshq.services.integration_oauth_checks import test_google_oauth_credentials


async def test_connection(config: dict, resolve_secret):
    return await test_google_oauth_credentials(
        config=config,
        resolve_secret=resolve_secret,
        resource_url="https://www.googleapis.com/drive/v3/about?fields=user,storageQuota",
    )
