"""Start the jukebox on all network interfaces.

The first start picks a port (80 if possible, for the shortest link) and saves it to .env.
"""
import uvicorn

from app.config import ensure_port

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=ensure_port())
