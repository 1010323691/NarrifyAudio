"""Small cancellation contract shared by task engines and runners."""


class TaskCancelled(Exception):
    """Raised by engine code to abort a task cleanly."""
