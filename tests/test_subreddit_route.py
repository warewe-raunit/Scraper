import os

# Auth is now fail-closed (no hardcoded default key), so the suite configures an
# explicit API_KEY that the requests below authenticate against.
os.environ["API_KEY"] = "stealth_secret_key_123"

from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from api.main import app
from api.dependencies import get_reddit_scraper_service

def test_subreddit_routes():
    # Setup mock scraper
    mock_scraper = AsyncMock()
    mock_scraper.scrape_subreddit.return_value = {"status": "success", "subreddit": "AIJobs"}
    
    # Register FastAPI dependency override
    app.dependency_overrides[get_reddit_scraper_service] = lambda: mock_scraper
    
    try:
        client = TestClient(app)
        
        # 1. Test original format without r/
        response = client.get(
            "/api/v1/subreddit/AIJobs/posts",
            headers={"X-API-Key": "stealth_secret_key_123"}
        )
        assert response.status_code == 200
        assert response.json() == {"status": "success", "subreddit": "AIJobs"}
        mock_scraper.scrape_subreddit.assert_called_with("AIJobs", "hot", "all", 25, None, account_id=None)
        
        # Reset mock
        mock_scraper.scrape_subreddit.reset_mock()
        mock_scraper.scrape_subreddit.return_value = {"status": "success", "subreddit": "AIJobs"}
        
        # 2. Test alternate format with r/
        response_r = client.get(
            "/api/v1/subreddit/r/AIJobs/posts",
            headers={"X-API-Key": "stealth_secret_key_123"}
        )
        assert response_r.status_code == 200
        assert response_r.json() == {"status": "success", "subreddit": "AIJobs"}
        mock_scraper.scrape_subreddit.assert_called_with("AIJobs", "hot", "all", 25, None, account_id=None)
        
    finally:
        # Clean up dependency override
        app.dependency_overrides.clear()
