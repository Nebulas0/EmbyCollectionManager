import requests
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

class MediaServerClient:
    """
    Base class for media server clients (Emby, Jellyfin).
    Provides shared request logic and interface.
    """
    def __init__(self, server_url: str, api_key: str, user_id: str, config=None):
        self.server_url = server_url.rstrip('/')
        self.api_key = api_key
        self.user_id = user_id
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            'X-Emby-Token': self.api_key,
            'Accept': 'application/json'
        })

    def close(self):
        """Close the HTTP session and release connection pool resources."""
        try:
            self.session.close()
        except Exception:
            pass

    def __del__(self):
        self.close()

    def _make_api_request(self, method: str, endpoint: str, **kwargs):
        """
        Helper for making API requests with error handling.
        """
        url = f"{self.server_url}{endpoint}"
        try:
            response = self.session.request(method, url, timeout=15, **kwargs)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.JSONDecodeError as e:
            logger.error(f"API response is not valid JSON: {e}")
            try:
                logger.error(f"Response text: {response.text[:200]}")
            except Exception:
                pass
            return None
        except requests.exceptions.HTTPError as e:
            logger.error(f"API request failed with HTTP error: {e}")
            logger.error(f"Request URL: {url}")
            logger.debug(f"Request method: {method}, params: {kwargs.get('params')}, json: {kwargs.get('json')}")
            if hasattr(e, 'response') and hasattr(e.response, 'text'):
                logger.error(f"Response text: {e.response.text[:200]}")
            return None
        except requests.RequestException as e:
            logger.error(f"API request failed: {e}")
            return None

    def get_or_create_collection(self, collection_name: str) -> Optional[str]:
        raise NotImplementedError

    def get_library_item_ids_by_tmdb_ids(self, tmdb_ids: List[int], library_ids: List[str] = None) -> List[str]:
        raise NotImplementedError

    def get_libraries(self) -> list:
        raise NotImplementedError

    def update_collection_items(self, collection_id: str, item_ids: List[str]) -> bool:
        raise NotImplementedError
        
    def update_collection_artwork(self, collection_id: str, poster_url: Optional[str]=None, backdrop_url: Optional[str]=None) -> bool:
        """
        Update artwork for a collection.
        
        Args:
            collection_id: Media server collection ID
            poster_url: URL to collection poster image
            backdrop_url: URL to collection backdrop/fanart image
            
        Returns:
            True if successful, False otherwise
        """
        raise NotImplementedError
