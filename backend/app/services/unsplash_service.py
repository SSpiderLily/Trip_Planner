"""Unsplash图片服务"""

import time
import requests
from requests.adapters import HTTPAdapter
from typing import List, Optional
from urllib3.util.retry import Retry

from ..config import get_settings


UNSPLASH_COOLDOWN_SECONDS = 60


class UnsplashService:
    """Unsplash图片服务类"""
    
    def __init__(self):
        """初始化服务"""
        settings = get_settings()
        self.access_key = settings.unsplash_access_key
        self.base_url = "https://api.unsplash.com"
        self._unavailable_until = 0.0
        self.session = requests.Session()
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            status=2,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
    
    def search_photos(self, query: str, per_page: int = 5) -> List[dict]:
        """
        搜索图片
        
        Args:
            query: 搜索关键词
            per_page: 每页数量
            
        Returns:
            图片列表
        """
        if not self.access_key:
            print("⚠️  Unsplash Access Key未配置,跳过图片搜索")
            return []

        # 网络故障后短暂熔断，避免一个页面为每个景点重复等待连接超时。
        if time.monotonic() < self._unavailable_until:
            return []

        try:
            url = f"{self.base_url}/search/photos"
            params = {
                "query": query,
                "per_page": per_page
            }
            headers = {
                "Authorization": f"Client-ID {self.access_key}",
                "Accept-Version": "v1"
            }
            
            response = self.session.get(
                url,
                params=params,
                headers=headers,
                timeout=(5, 10)
            )
            response.raise_for_status()
            
            data = response.json()
            results = data.get("results", [])
            
            # 提取图片URL
            photos = []
            for photo in results:
                photos.append({
                    "id": photo.get("id"),
                    "url": photo.get("urls", {}).get("regular"),
                    "thumb": photo.get("urls", {}).get("thumb"),
                    "description": photo.get("description") or photo.get("alt_description"),
                    "photographer": photo.get("user", {}).get("name")
                })
            
            return photos
            
        except (requests.RequestException, ValueError) as e:
            self._unavailable_until = time.monotonic() + UNSPLASH_COOLDOWN_SECONDS
            # 不输出异常正文：requests 的异常可能包含带认证参数的完整 URL。
            print(
                f"❌ Unsplash搜索失败 ({type(e).__name__}),"
                f"{UNSPLASH_COOLDOWN_SECONDS}秒内暂停重试"
            )
            return []
    
    def get_photo_url(self, query: str) -> Optional[str]:
        """
        获取单张图片URL

        Args:
            query: 搜索关键词

        Returns:
            图片URL
        """
        photos = self.search_photos(query, per_page=1)
        if photos:
            return photos[0].get("url")
        return None


# 全局服务实例
_unsplash_service = None


def get_unsplash_service() -> UnsplashService:
    """获取Unsplash服务实例(单例模式)"""
    global _unsplash_service
    
    if _unsplash_service is None:
        _unsplash_service = UnsplashService()
    
    return _unsplash_service
