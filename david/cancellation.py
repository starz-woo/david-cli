"""Cooperative cancellation crosses model/tool exception handlers unchanged."""


class Cancelled(KeyboardInterrupt):
    """A user cancelled the active turn; already completed edits remain."""


def check_cancel(event):
    if event is not None and event.is_set():
        raise Cancelled('사용자가 작업을 취소했습니다. 이미 적용한 변경은 남아 있습니다.')
