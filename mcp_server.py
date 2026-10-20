from mcp.server.fastmcp import FastMCP
from vector_store import VectorStore
import os

mcp = FastMCP("CallingAgentTools")
_db = None

def get_db():
    global _db
    if _db is None:
        _db = VectorStore()
    return _db

@mcp.tool()
def lookup_employee(name: str, team: str = None) -> str:
    """
    Looks up an employee's details (such as phone number, team, designation, technology)
    from the vector database using their name and/or team name.

    Args:
        name: The name of the employee to search for.
        team: The team they work in (optional).
    """
    db = get_db()
    query = f"Name: {name}"
    if team:
        query += f", Team: {team}"
    
    results = db.search_employee(query, limit=3)
    if not results:
        return f"No employee found matching name '{name}' and team '{team}'."
    
    out = []
    for r in results:
        meta = r["metadata"]
        out.append(
            f"Name: {meta['name']}\n"
            f"Designation: {meta['designation']}\n"
            f"Phone Number: {meta['phone_number']}\n"
            f"Team: {meta['team']}\n"
            f"Technology: {meta['technology']}\n"
            f"Match Distance: {r['distance']:.4f}\n"
        )
    return "\n---\n".join(out)

@mcp.tool()
def transfer_call(phone_number: str) -> str:
    """
    Initiates a call transfer to the specified phone number.
    This tool should be called when the user requests to forward their call to someone else
    and we have retrieved their phone number.

    Args:
        phone_number: The phone number to transfer to in E.164 format (e.g. +1234567890).
    """
    return f"TRANSFER_SIGNAL: {phone_number}"
