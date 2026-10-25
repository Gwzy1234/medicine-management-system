"""只包含可以公开给客户端的 HTTP 错误，不携带底层异常文本。"""


class APIError(Exception):
    def __init__(self, status, code, message, headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers
