import logging
from mcp.server.fastmcp import FastMCP
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from toolset import Toolset, tool
from workload import Workload

mcp = FastMCP(host="0.0.0.0", port=8000, stateless_http=True)
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


class DriveToolset(Toolset):
    def __init__(self):
        self.workload = Workload()

    @tool
    async def get_google_auth_url(self) -> dict:
        """
        Get Google OAuth authorization URL.
        You need to send the user to this URL to authorize access.
        """
        return await self.workload.get_google_auth_url()
    

    @tool
    async def fetch_drive_files_from_google(self):
        """
        Fetch Google Drive Files using OAuth token.
        """

        try:
            access_token = await self.workload.get_token()
        except Exception as e:
            return {
                "type": "error",
                "message": str(e),
            }

        if isinstance(access_token, dict):
            return access_token

        if access_token == "Error":
            return {
                "type": "authorization_required",
                "authorization_url": await self.workload.get_google_auth_url(),
                "message": "Failed to obtain access token. Please authorize again.",
            }

        creds = Credentials(token=access_token)
        try:
            service = build("drive", "v3", credentials=creds)
            results = (
            service.files()
            .list(pageSize=10, fields="nextPageToken, files(id, name)")
            .execute()
        )
            return {
                "type": "success",
                "files": results.get("files", []),
            }
        except Exception as e:
            items = {"type": "error", "message": str(e)}

        return items;


if __name__ == "__main__":
    logger.info("Starting MCP server...")
    toolset = DriveToolset()
    toolset.import_tools(mcp)
    mcp.run(transport="streamable-http")

