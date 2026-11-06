import random
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel
app = MCPServer("mockcrm")
random.seed(1)
ACCOUNTS = [{"account_id": i, "name": f"Acct{i}", "segment": "Enterprise" if i % 2 else "SMB"} for i in range(200)]
class Invoice(BaseModel):
    invoice_id: str; amount: float; days_overdue: int; status: str
@app.tool()
def list_accounts(segment: str) -> list[dict]:
    """List accounts in a segment. Returns [{account_id, name, segment}]."""
    return [a for a in ACCOUNTS if a["segment"] == segment]
@app.tool()
def get_invoices(account_id: int) -> list[Invoice]:
    """Invoices for one account."""
    return [Invoice(invoice_id=f"{account_id}-{n}", amount=round(random.uniform(100, 9000), 2),
                    days_overdue=random.choice([0, 0, 30, 75, 120]), status="Open") for n in range(5)]
@app.tool(name="get-summary")
def summary() -> dict:
    """Hyphenated tool name returning a dict."""
    return {"accounts": len(ACCOUNTS), "ok": True}
@app.tool()
def fails() -> str:
    """Always errors."""
    raise ValueError("boom")
if __name__ == "__main__":
    app.run()
