import inspect

from mcp.server.fastmcp import FastMCP

def tool(func):
    """Decorator to mark a method as a tool."""
    func.__is_tool = True
    return func


class Toolset:
    """
    Base class for defining a set of tools to be registered with FastMCP.
    Handles automatic registration of methods decorated with @tool.
    """
    def import_tools(self, mcp: FastMCP):
        for name, method in inspect.getmembers(self, predicate=inspect.ismethod):
            if getattr(method, "__is_tool", False):
                mcp.add_tool(fn=method, name=name, description=method.__doc__)




