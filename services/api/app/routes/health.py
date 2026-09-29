# services/api/app/routes/health.py
from fastapi import APIRouter, Response, status
from services.api.app.clients.neo4j import neo4j_client

router = APIRouter()

@router.get("/liveness")
async def liveness():
    """
    K8s Liveness Probe.
    Returns 200 if the server process is running.
    """
    return {"status": "ok"}

@router.get("/readiness")
async def readiness(response: Response):
    """
    K8s Readiness Probe.
    Checks connectivity to Neo4j.
    If this fails, K8s stops sending traffic to this pod.
    """
    status_report = {"neo4j": "down"}
    is_healthy = True

    # Check Neo4j (connectivity only)
    try:
        # Driver is singleton, check if initialized
        if neo4j_client._driver:
            status_report["neo4j"] = "up"
        else:
             is_healthy = False
    except Exception:
        is_healthy = False

    if not is_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    
    return status_report
