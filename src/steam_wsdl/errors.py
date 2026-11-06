from __future__ import annotations


class WorkshopError(Exception):
    """Erro base da aplicação."""


class UsageError(WorkshopError):
    """Erro de uso (argumentos/arquivos de entrada inválidos)."""


class DownloadError(WorkshopError):
    """Falha definitiva ao baixar um item."""


class TransientError(WorkshopError):
    """Falha temporária; vale a pena tentar novamente."""


class Cancelled(WorkshopError):
    """Operação cancelada pelo usuário (Ctrl+C)."""


class NeedSteamcmd(Exception):
    """Sinal interno: o item deve ser baixado pelo steamcmd (em lote)."""

