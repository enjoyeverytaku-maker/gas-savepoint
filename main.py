from fastapi import FastAPI

app = FastAPI(title="SavePoint")


@app.get("/api/health")
def health():
    return {"status": "ok"}
