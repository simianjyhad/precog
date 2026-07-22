"""
pause.py — background keypress listener for toggling monitoring
pause/resume without needing Enter or a second terminal.

Uses raw (cbreak) terminal mode so a single keystroke is picked up
immediately, no Enter required. If stdin isn't a real interactive
tty (e.g. running under systemd, piped input, redirected from a
file) the listener quietly disables itself rather than raising —
precog runs exactly as it did before this feature existed.
"""

import sys
import threading

try:
    import termios
    import tty
    import select
    _HAS_TERMIOS = True
except ImportError:
    # termios/tty are POSIX-only; not available on e.g. Windows.
    _HAS_TERMIOS = False


class PauseController:
    """
    Watches stdin in a background thread for a single toggle-key
    press and flips an internal paused flag each time it's seen.
    is_paused() is safe to call from any thread.
    """

    def __init__(self, toggle_key: str = "p", on_toggle=None):
        """
        toggle_key: single character (case-insensitive) that flips
                    the paused state.
        on_toggle:  optional callback(new_paused_state: bool),
                    called from the listener thread each time the
                    state flips.
        """
        self.toggle_key = toggle_key.lower()
        self.on_toggle = on_toggle
        self._paused = False
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        # Disabled entirely if stdin isn't an interactive terminal —
        # cbreak mode has no meaning on a pipe/file/service, and
        # trying to set it would raise.
        self._enabled = _HAS_TERMIOS and sys.stdin.isatty()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def start(self) -> None:
        if not self._enabled:
            return
        self._thread = threading.Thread(
            target=self._listen, daemon=True, name="precog-pause-listener"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _listen(self) -> None:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not self._stop_event.is_set():
                # Short select() timeout so shutdown (stop_event) is
                # noticed promptly instead of blocking forever on
                # read() with nothing typed.
                ready, _, _ = select.select([sys.stdin], [], [], 0.5)
                if not ready:
                    continue
                ch = sys.stdin.read(1)
                if ch.lower() == self.toggle_key:
                    with self._lock:
                        self._paused = not self._paused
                        new_state = self._paused
                    if self.on_toggle:
                        self.on_toggle(new_state)
        finally:
            # Always restore the terminal, even if something above
            # raised — leaving a shell in cbreak mode is a nasty
            # surprise for whatever runs next in that terminal.
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
