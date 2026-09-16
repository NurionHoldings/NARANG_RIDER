import re
from pathlib import Path

from narang_rider.api_contract import ROUTE_MANIFEST
from narang_rider.control_center import CONTROL_CENTER_ROUTES
from narang_rider.customer_proof import CUSTOMER_PROOF_ROUTE_MANIFEST
from narang_rider.merchant_operations import MERCHANT_ROUTE_MANIFEST
from narang_rider.rider_workflow import RIDER_ROUTE_MANIFEST


def test_frontend_routes_are_bound_to_backend_contracts() -> None:
    source = Path("frontend/src/api.ts").read_text()
    frontend = set(re.findall(r'"(/api/v1/[^"\n]+)"', source))
    backend = {route.path_template for route in ROUTE_MANIFEST}
    backend |= {route.path_template for route in MERCHANT_ROUTE_MANIFEST}
    backend |= {route.path_template for route in RIDER_ROUTE_MANIFEST}
    backend |= {route.path_template for route in CUSTOMER_PROOF_ROUTE_MANIFEST}
    backend |= {route[1] for route in CONTROL_CENTER_ROUTES}
    assert frontend <= backend
    assert not frontend - backend


def test_frontend_does_not_register_a_service_worker_or_persist_sensitive_media() -> None:
    paths = list(Path("frontend/src").glob("*.ts")) + list(Path("frontend").glob("*.html"))
    sources = "\n".join(path.read_text() for path in paths)
    assert "navigator.serviceWorker.register" not in sources
    assert "localStorage" not in sources
    assert "indexedDB" not in sources
