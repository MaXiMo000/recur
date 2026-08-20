#!/usr/bin/env python3
"""Local MCP server over stdio.

    RECUR_USER_ID=1 .venv/bin/python mcp_server.py

For working against a database on your own machine. No OAuth round trip, no
port, no token -- a local process on a pipe has no network surface to attack,
which is why this transport still exists alongside the remote one.

The tools live in mcp_tools.py and take a user id. The remote endpoint binds
that id from an OAuth token; here it comes from the environment. One
implementation either way -- there is no code path that reads "everyone's" data
because there is no function that can be called without a user.
"""

from __future__ import annotations

import os
import sys

from mcp.server import MCPServer

import db
import mcp_tools

USER_ID = int(os.environ.get("RECUR_USER_ID", "0"))
mcp = MCPServer("recur")


def _bind(name):
    """Expose one tool with its user already fixed, keeping the description
    exactly as written in mcp_tools -- never rebuilt from database content."""
    fn, description, props, _ = mcp_tools.TOOLS[name]

    def tool(**kwargs):
        return fn(USER_ID, **{k: v for k, v in kwargs.items() if k in props})

    tool.__name__ = name
    tool.__doc__ = description
    return mcp.tool(name=name, description=description)(tool)


for _name in mcp_tools.TOOLS:
    _bind(_name)


if __name__ == "__main__":
    if USER_ID <= 0:
        sys.exit("Set RECUR_USER_ID to the account this server should read.")
    db.open_pool()
    mcp.run()  # stdio is the default, and the only transport this ships with
