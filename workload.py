import asyncio
import json
from pathlib import Path
from typing import List, Optional, Literal
import jwt
from fastmcp.server.dependencies import get_http_request
from bedrock_agentcore.services.identity import IdentityClient
from bedrock_agentcore.identity.auth import  _get_region

class Workload:
    def __init__(self, callback_url="https://api.tomasp.me/redirect"):
        self.client = IdentityClient(_get_region())
        self.callback_url = callback_url

    def get_user(self) -> str | dict:
        """Extract user identity from the Authorization header in the HTTP request."""

        req = get_http_request()
        if not req:
            return "No request found"

        auth_header = req.headers.get("Authorization")
        if not auth_header:
            return {"error": "No Authorization header"}

        if not auth_header.startswith("Bearer "):
            return {"error": "Invalid Authorization header"}

        token = auth_header.split(" ", 1)[1]

        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except Exception:
            return {"error": "Invalid token"}

        return claims.get("sub")

    async def get_google_auth_url(self) -> dict:
        """
        Get Google OAuth authorization URL.
        """
        try:
            url = await self.get_oauth_url(
                provider_name="google-oauth-client-fsllt",
                scopes=["https://www.googleapis.com/auth/drive.metadata.readonly"],
                auth_flow="USER_FEDERATION",
                callback_url="https://api.tomasp.me/redirect",
                force_authentication=True,
            )

            return {
                "type": "authorization_required",
                "authorization_url": url,
                "message": "Open this link and grant access to Google Drive.",
            }
        except Exception as e:
            return {
                "type": "error",
                "message": str(e),
            }


    def get_workload_identity(self, user_id: str) -> str:
        """
        Get or create a workload identity for the given user ID.
        """
        path = Path(f".agentcore-{user_id}.json")

        if path.exists():
            try:
                with open(path) as f:
                    cfg = json.load(f)
                    return cfg["workload_identity_name"]
            except Exception:
                pass

        resp = self.client.create_workload_identity()
        identity_name = resp["name"]

        # Configure the workload identity to allow OAuth callbacks
        self.client.update_workload_identity(
            name=identity_name,
            allowed_resource_oauth_2_return_urls=[self.callback_url],
        )

        with open(path, "w") as f:
            # Caching the identity name
            json.dump({"workload_identity_name": identity_name}, f, indent=2)

        return identity_name

    async def get_workload_access_token(self, user_id: str) -> str:
        resp = self.client.get_workload_access_token(self.get_workload_identity(user_id), user_id=user_id)

        return resp["workloadAccessToken"]

    async def get_oauth_url(
        self,
        provider_name: str,
        scopes: List[str],
        auth_flow: Literal["M2M", "USER_FEDERATION"],
        callback_url: Optional[str] = None,
        force_authentication: bool = True,
    ) -> str:
        """"
        Get OAuth authorization URL for the specified provider.
        """
        user_id = self.get_user()

        if isinstance(user_id, dict) or not user_id:
            raise Exception(f"Invalid user identity: {user_id}")

        loop = asyncio.get_running_loop()
        url_future: asyncio.Future[str] = loop.create_future()

        def on_auth_url(url: str):
            if not url_future.done():
                url_future.set_result(url)

        async def _run_flow():
            try:
                agent_token = await self.get_workload_access_token(user_id)
                await self.client.get_token(
                    provider_name=provider_name,
                    agent_identity_token=agent_token,
                    scopes=scopes,
                    on_auth_url=on_auth_url,
                    auth_flow=auth_flow,
                    callback_url=callback_url,
                    force_authentication=force_authentication,
                    custom_state=json.dumps({"user_id": user_id}),
                )
            except Exception as e:
                if not url_future.done():
                    url_future.set_exception(e)

        asyncio.create_task(_run_flow())
        return await url_future

    async def get_token(self):
        user_id = self.get_user()

        if isinstance(user_id, dict) or not user_id:
                raise Exception(f"Invalid user identity")

        try:
            agent_token = await self.get_workload_access_token(user_id)

            google_access_token = await self.client.get_token(
                provider_name="google-oauth-client-fsllt",
                agent_identity_token=agent_token,
                scopes=["https://www.googleapis.com/auth/drive.metadata.readonly"],
                auth_flow="USER_FEDERATION",
                force_authentication=False,
            )

            return google_access_token

        except Exception as e:
            # If token is not found or expired, return authorization URL
            authorization_url = await self.get_oauth_url(
                provider_name="google-oauth-client-fsllt",
                scopes=["https://www.googleapis.com/auth/drive.metadata.readonly"],
                auth_flow="USER_FEDERATION",
                callback_url=self.callback_url,
                force_authentication=True,
            )

            return {
                "type": "authorization_required",
                "message": "Google authorization required",
                "error": str(e),
                "authorization_url": authorization_url,
            }

