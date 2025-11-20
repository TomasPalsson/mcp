import os
import json
import asyncio
import time
import urllib.parse
from typing import Any, Dict, List, Optional
import requests
import jwt  
import boto3
from botocore.config import Config
from fastmcp import Client as MCPClient
from fastmcp.client.transports import StreamableHttpTransport
from dotenv import load_dotenv

RESET  = "\033[0m"
BOLD   = "\033[1m"
WHITE  = "\033[97m"
BLUE   = "\033[94m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
GRAY   = "\033[90m"
MAGENTA = "\033[95m"

def _ts() -> str:
    return time.strftime("%H:%M:%S")

def log(prefix: str, msg: str, color: str = WHITE) -> None:
    print(f"{GRAY}[{_ts()}]{RESET} {color}{prefix} {msg}{RESET}")

load_dotenv()

COGNITO_DOMAIN = os.getenv("COGNITO_HOSTED_DOMAIN", "").rstrip("/")
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET", "")
OIDC_SCOPE = os.getenv("OIDC_SCOPE", "email openid profile")
OIDC_REDIRECT_URI = os.getenv("OIDC_REDIRECT_URI", "")

TOKEN_CACHE_PATH = os.path.expanduser("~/.drive_mcp_oidc_token.json")


def _load_cached_token() -> Optional[dict]:
    """Return cached token if present and not expired, else None."""
    if not os.path.exists(TOKEN_CACHE_PATH):
        return None

    try:
        with open(TOKEN_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    expires_at = data.get("expires_at", 0)
    if time.time() < expires_at - 60:
        return data
    return None


def _save_cached_token(tok: dict) -> None:
    """Store token to disk with an expires_at field."""
    expires_in = tok.get("expires_in", 3600)
    tok["expires_at"] = time.time() + expires_in
    os.makedirs(os.path.dirname(TOKEN_CACHE_PATH), exist_ok=True)
    with open(TOKEN_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(tok, f, indent=2)
    log("✔", f"Token cached at {TOKEN_CACHE_PATH}", GREEN)


def _interactive_oidc_login() -> dict:
    if not (COGNITO_DOMAIN and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET and OIDC_REDIRECT_URI):
        raise RuntimeError(
            "Missing OIDC env vars. Make sure COGNITO_HOSTED_DOMAIN, "
            "OIDC_CLIENT_ID, OIDC_CLIENT_SECRET and OIDC_REDIRECT_URI are set."
        )

    params = {
        "response_type": "code",
        "client_id": OIDC_CLIENT_ID,
        "redirect_uri": OIDC_REDIRECT_URI,
        "scope": OIDC_SCOPE,
    }
    auth_url = f"{COGNITO_DOMAIN}/oauth2/authorize?{urllib.parse.urlencode(params)}"

    print()
    print(f"{YELLOW}▲ OIDC login required{RESET}")
    print("Open this URL in your browser, log in,")
    print("then copy the FULL redirect URL from the address bar and paste it here:")
    print()
    print(f"{CYAN}{auth_url}{RESET}")
    print()

    redirect_url = input(f"{BOLD}{MAGENTA}Paste redirect URL ▶ {RESET}").strip()
    parsed = urllib.parse.urlparse(redirect_url)
    qs = urllib.parse.parse_qs(parsed.query)
    code = qs.get("code", [None])[0]
    if not code:
        raise RuntimeError("No 'code' query parameter found in the redirect URL.")

    token_resp = requests.post(
        f"{COGNITO_DOMAIN}/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "client_id": OIDC_CLIENT_ID,
            "client_secret": OIDC_CLIENT_SECRET,
            "code": code,
            "redirect_uri": OIDC_REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    token_resp.raise_for_status()
    tok = token_resp.json()
    _save_cached_token(tok)
    print(f"{GREEN}✔ OIDC login successful{RESET}")
    return tok


def get_user_jwt() -> str:
    """
    Return a user JWT (id_token) for calling the MCP runtime.
    Uses cache if valid, otherwise runs interactive login.
    """
    cached = _load_cached_token()
    if cached:
        return cached["access_token"]

    tok = _interactive_oidc_login()
    return tok["access_token"]


def _json_objectize(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"items": value}
    try:
        json.dumps(value)
        return {"result": value}
    except TypeError:
        return {"result": str(value)}


class MCPToolCatalog:
    def __init__(self, mcp_source: Any, auth: Optional[str] = None, prefix: Optional[str] = None):
        self._src = mcp_source
        self._prefix = prefix or ""
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._active = False
        self._client: Optional[MCPClient] = None
        self._ctx_client = None
        self._auth = auth

    def _new_client(self):
            if self._auth:
                print(f"{GRAY}[{_ts()}]{RESET} {WHITE}Using authenticated MCP client with token {self._auth[:20]}...{RESET}")
                return MCPClient(self._src, auth=self._auth)
            return MCPClient(self._src)
    

    async def __aenter__(self):
        log("▶", f"Connecting to MCP server: {CYAN}{self._src}", WHITE)
        self._client = self._new_client()
        self._ctx_client = await self._client.__aenter__()
        self._active = True
        await self._ctx_client.ping()
        tools = await self._ctx_client.list_tools()
        self._tools = {
            t.name: {
                "name": t.name,
                "description": getattr(t, "description", "") or "",
                "inputSchema": getattr(t, "inputSchema", None),
                "original_name": t.name,
            }
            for t in tools
        }
        log("✔", f"Loaded {len(tools)} tools", GREEN)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._active = False
        if self._client is not None:
            await self._client.__aexit__(exc_type, exc, tb)
        log("■", "MCP session closed", CYAN)

    def bedrock_tool_config(self) -> Dict[str, Any]:
        specs = []
        for t in self._tools.values():
            schema = t["inputSchema"] or {"type": "object", "properties": {}}
            specs.append({
                "toolSpec": {
                    "name": t["name"],
                    "description": t["description"],
                    "inputSchema": {"json": schema},
                }
            })
        return {"tools": specs}

    async def _refresh_client(self):
        log("↺", "Refreshing MCP client...", YELLOW)
        await self._client.__aexit__(None, None, None)
        self._client = self._new_client()
        self._ctx_client = await self._client.__aenter__()

    async def call(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        ts = _ts()
        print(f"{GRAY}[{ts}]{RESET} {WHITE}▶ Calling MCP tool:{RESET} {CYAN}{tool_name}{RESET}")
        print(f"{GRAY}[{ts}]{RESET} {WHITE}│ Arguments:{RESET} {BLUE}{arguments}{RESET}")

        if not self._active:
            raise RuntimeError("MCPToolCatalog not active.")
        entry = self._tools.get(tool_name)
        if not entry:
            raise ValueError(f"Unknown MCP tool: {tool_name}")
        try:
            res = await self._ctx_client.call_tool(entry["original_name"], arguments or {})
            print(f"{GRAY}[{_ts()}]{RESET} {GREEN}✔ Call succeeded{RESET}")
        except Exception as e:
            print(f"{GRAY}[{_ts()}]{RESET} {YELLOW}⚠ Tool call failed, retrying... ({e}){RESET}")
            await self._refresh_client()
            res = await self._ctx_client.call_tool(entry["original_name"], arguments or {})
            print(f"{GRAY}[{_ts()}]{RESET} {GREEN}✔ Call succeeded after refresh{RESET}")

        if getattr(res, "data", None) is not None:
            return res.data
        if getattr(res, "structured_content", None) is not None:
            return res.structured_content
        texts = [c.text for c in getattr(res, "content", []) if hasattr(c, "text")]
        return {"result": "\n".join(t for t in texts if t) if texts else None}


class BedrockMCPAgent:
    def __init__(
        self,
        model_id: str,
        mcp_catalog: MCPToolCatalog,
        region: Optional[str] = None,
        system_prompt: Optional[str] = None,
        max_tool_rounds: int = 6,
    ):
        log("▶", "Initializing Bedrock agent", WHITE)
        self.model_id = model_id
        self.mcp = mcp_catalog
        self.client = boto3.client(
            "bedrock-runtime",
            region_name=region or os.getenv("AWS_REGION") or "eu-west-1",
            config=Config(retries={"max_attempts": 3}),
        )
        self.tool_config = self.mcp.bedrock_tool_config()
        self.max_tool_rounds = max_tool_rounds
        self.messages: List[Dict[str, Any]] = []
        self.system_prompt = system_prompt or "You are a helpful assistant that uses tools when helpful."
        log("✔", "Bedrock ready", GREEN)

    def _invoke(self) -> Dict[str, Any]:
        try:
            return self.client.converse(
                modelId=self.model_id,
                messages=self.messages,
                system=[{"text": self.system_prompt}],
                toolConfig=self.tool_config,
            )
        except Exception as e:
            log("⚠", f"Throttled by Bedrock service: {e}", YELLOW)
            time.sleep(2)
            return self.client.converse(
                modelId=self.model_id,
                messages=self.messages,
                system=[{"text": self.system_prompt}],
                toolConfig=self.tool_config,
            )

    async def chat(self, user_text: str) -> str:
        start = time.time()
        self.messages.append({"role": "user", "content": [{"text": user_text}]})
        response = self._invoke()
        self.messages.append(response["output"]["message"])

        rounds = 0
        while response.get("stopReason") == "tool_use" and rounds < self.max_tool_rounds:
            rounds += 1

            tool_uses = [
                c["toolUse"]
                for c in response["output"]["message"]["content"]
                if "toolUse" in c
            ]

            if not tool_uses:
                break

            tool_result_blocks = []

            for tu in tool_uses:
                name = tu["name"]
                args = tu.get("input")
                if isinstance(args, str):
                    args = json.loads(args)
                elif args is None:
                    args = {}

                raw = await self.mcp.call(name, args)
                obj = _json_objectize(raw)

                tool_result_blocks.append({
                    "toolResult": {
                        "toolUseId": tu["toolUseId"],
                        "content": [{"json": obj}],
                    }
                })

            self.messages.append({
                "role": "user",
                "content": tool_result_blocks,
            })

            response = self._invoke()
            self.messages.append(response["output"]["message"])

        blocks = self.messages[-1]["content"]
        texts = [b.get("text", "") for b in blocks if "text" in b]
        result = "\n".join(t for t in texts if t)
        print(f"{GRAY}[{time.time() - start:.2f}s]{RESET} {GREEN}Assistant ▶ {RESET}{result}")
        return result


async def main():
    log("▶", "Starting Bedrock MCP Agent", CYAN)

    model_id = os.getenv(
        "BEDROCK_MODEL_ID",
        "arn:aws:bedrock:eu-west-1:519689943567:inference-profile/eu.anthropic.claude-3-haiku-20240307-v1:0",
    )

    arn = os.getenv("AGENT_ARN", "")
    encoded = urllib.parse.quote(arn, safe='')
    region = "eu-west-1"
    mcp_source = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded}/invocations?qualifier=DEFAULT"
    system = """
You are an advanced AI assistant that can call various tools to help answer user questions.
When you get a google auth url, you will write it out exactly to the user so the user can click it.
    """

    user_jwt = get_user_jwt()

    async with MCPToolCatalog(mcp_source=mcp_source, auth=user_jwt) as catalog:
        agent = BedrockMCPAgent(model_id=model_id, mcp_catalog=catalog, system_prompt=system)
        while True:
            user_input = input(f"{BOLD}{MAGENTA}You ▶ {RESET}")
            if user_input.lower() in {"exit", "quit"}:
                log("■", "Session ended", CYAN)
                break
            await agent.chat(user_input)


if __name__ == "__main__":
    asyncio.run(main())

