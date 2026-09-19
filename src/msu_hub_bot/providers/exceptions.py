class ExternalServiceError(Exception):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


class BadRequestError(ExternalServiceError):
    def __init__(self) -> None:
        super().__init__("Не удалось выполнить запрос")


class NotFoundError(ExternalServiceError):
    def __init__(self) -> None:
        super().__init__("Ничего не удалось найти")


class CantFindFaceError(ExternalServiceError):
    def __init__(self) -> None:
        super().__init__("Не удалось найти лицо")


class BadExpressionError(ExternalServiceError):
    def __init__(self) -> None:
        super().__init__("Генератор не принимает запросы на острые и чувствительные темы")
