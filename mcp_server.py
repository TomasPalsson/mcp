import logging
import re
from mcp.server.fastmcp import FastMCP
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from toolset import Toolset, tool
from workload import Workload

instructions = """
    Tools that you have help you fill in templates bu you must first authenticate the user
    To Replace a template you should follow these steps:
    1. Use the 'Authenticate User' tool to authenticate the user you must write out the URL directly to the user
    2. Get the file id of the template
    3. Use the get variables tool to get the list of variables in the templates
    4. For each variable ask the user for the value of the variable do this one by one until all variables are collected then use the tool
    5. Use the 'Fill Template' tool to fill in the template with the variables provided by the user
    NEVER call the fill template tool without getting ALL the variables from the user first, then ask for the final name of the completed file and call the tool
"""

mcp = FastMCP(host="0.0.0.0", port=8000, stateless_http=True, instructions=instructions)
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
        You will need to write it out for the user to copy and paste it
        """
        return await self.workload.get_google_auth_url()
    

    @tool
    async def fetch_file_id(self, query: str = "") -> dict:
        """
        Fetch Google Drive Files based on a query. Returns Name and ID
        Query example: 
        - mimeType='application/pdf'
        - name = 'budget.xlsx'
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
            .list(pageSize=10, q=query, fields="nextPageToken, files(id, name)")
            .execute()
        )
            return {
                "type": "success",
                "files": results.get("files", []),
            }
        except Exception as e:
            items = {"type": "error", "message": str(e)}

        return items;


    def _extract_text(self, doc) -> set:
        content = doc.get("body", {}).get("content", [])
        text_fragments = []

        for element in content:
            para = element.get("paragraph")
            if not para:
                continue

            for elem in para.get("elements", []):
                run = elem.get("textRun")
                if run and "content" in run:
                    text_fragments.append(run["content"])

        full_text = "".join(text_fragments)

    # Find {{VARIABLE}} patterns using regex
        placeholders = set(re.findall(r"\{\{(.*?)\}\}", full_text))
        return placeholders


    @tool
    async def get_drive_vars(self, file_id: str) -> dict:
        """
        Get Drive file variables for a given file ID.
        File ID can be fetched using fetch_file_id tool.
        """
        access_token = await self.workload.get_token()

        if isinstance(access_token, dict):
            return access_token


        creds = Credentials(token=access_token)
        docs = build("docs", "v1", credentials=creds)

        try:
            doc = docs.documents().get(documentId=file_id).execute()
            variables = self._extract_text(doc)
            return {
                "type": "success",
                    "variables": list(variables),
            }



        except Exception as e:
            return {
                "type": "error",
                "message": str(e),
            }

            

    @tool
    async def place_variables_in_template(self, file_id: str, variables: dict, completed_file_name: str) -> dict:
        """
        Place variables in a Google Docs template.
        You should then ask the user for the values to replace. INDIVIDUALLY slowly
        `variables` should be a dictionary where keys are variable names
        and values are the values to replace them with.
        Example:
        {
            "NAME": "John Doe",
            "DATE": "2023-10-01"
        }
        DO NOT USE THIS TOOL UNTIL YOU HAVE ALL THE VARIABLES FROM THE USER
        """
        access_token = await self.workload.get_token()
        
        if isinstance(access_token, dict):
            return access_token

        creds = Credentials(token=access_token)
        drive = build("drive", "v3", credentials=creds)
        docs = build("docs", "v1", credentials=creds)
        new_file = drive.files().copy(
            fileId=file_id,
            body={"name": completed_file_name}
        ).execute()

        doc_id = new_file["id"]

        requests = []
        for var, value in variables.items():
            requests.append({
                "replaceAllText": {
                    "containsText": {
                        "text": f"{{{{{var}}}}}",
                        "matchCase": True
                    },
                    "replaceText": value
                }
            })

        try:
            result = docs.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": requests}
            ).execute()

            return {
                "type": "success",
                "message": "Variables replaced successfully.",
                "result": result,
            }

        except Exception as e:
            return {
                "type": "error",
                "message": str(e),
            }
        


if __name__ == "__main__":
    logger.info("Starting MCP server...")
    toolset = DriveToolset()
    toolset.import_tools(mcp)
    mcp.run(transport="streamable-http")

