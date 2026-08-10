class ApiError(Exception):
    def __init__(self, code=0, message=""):
        super().__init__(message or f"vk error {code}")
        self.code = code
