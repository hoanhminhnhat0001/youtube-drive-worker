import json
import os

from google_auth_oauthlib.flow import InstalledAppFlow


SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


def main():
    client_file = os.environ.get("GOOGLE_OAUTH_CLIENT_FILE", "client_secret.json")
    flow = InstalledAppFlow.from_client_secrets_file(client_file, SCOPES)
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    print(json.dumps({
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "refresh_token": credentials.refresh_token,
    }, indent=2))


if __name__ == "__main__":
    main()
