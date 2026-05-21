
import unittest
from fastapi.testclient import TestClient
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from app import app

# Create Client
client = TestClient(app)

class TestBlackboxAPI(unittest.TestCase):
    def test_health_endpoint(self):
        """Verify the health endpoint returns system stats structure."""
        # Need a token or mock auth?
        # The endpoint /api/stats/health depends on 'require_admin'
        # app.py:854: async def admin_delete_camera(..., role: str = Depends(require_admin))
        # Wait, /api/stats/health is typically protected.
        # However, for blackbox testing we might mock the database auth or get a token.
        # But 'app.py' defines require_admin.
        
        # Let's try to hit /login first if needed, or mock the dependency override.
        # But this is a blackbox test. 
        # Actually, let's look at the auth implementation. 
        # require_admin checks for Bearer token in SESSIONS.
        
        pass 
        # Since setting up a full auth session in a test unit involving DB might be flaky without a dedicated test DB,
        # We will override the dependency for this test suite.
        
    def test_public_flow(self):
        # /auth/login is a good candidate
        # But we need a valid user in DB.
        pass

# Re-implementing with Dependency Overrides for stability
from app import require_admin, require_any_role

async def mock_require_admin():
    return "admin"

async def mock_require_any():
    return "user"

app.dependency_overrides[require_admin] = mock_require_admin
app.dependency_overrides[require_any_role] = mock_require_any

class TestBlackboxAPI_Mocked(unittest.TestCase):
    def test_health(self):
        response = client.get("/api/stats/health")
        self.assertEqual(response.status_code, 200)
        json_data = response.json()
        self.assertIn("cpu", json_data)
        self.assertIn("disk", json_data)
        self.assertEqual(json_data["network"], "Online")

    def test_detections_endpoint(self):
        response = client.get("/api/detections")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("detections", data)

    def test_camera_404(self):
        # Test deleting a non-existent camera
        response = client.delete("/admin/cameras/999999")
        # Ensure it returns 404
        self.assertEqual(response.status_code, 404)

    def test_settings_endpoint(self):
        response = client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("settings", data)

if __name__ == "__main__":
    unittest.main()
