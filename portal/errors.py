from __future__ import annotations


class PortalError(Exception):
    """會直接回給使用者的錯誤（status 為 HTTP 狀態碼）。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message
