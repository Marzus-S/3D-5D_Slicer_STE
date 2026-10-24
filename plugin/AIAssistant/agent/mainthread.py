# Выполнение кода в главном потоке Qt.
# HTTP-сервер и агент работают в своих потоках, а сцену, настройки и слайсинг
# можно трогать только из главного потока — поэтому всё идёт через callLater.

import threading
import traceback

from UM.Application import Application


class MainThreadTimeout(Exception):
    pass


def is_main_thread():
    return threading.current_thread() is Application.getInstance().getMainThread()


def run_on_main(func, *args, timeout=60, **kwargs):
    """Вызывает func(*args, **kwargs) в главном потоке и возвращает результат.

    Если вызов уже идёт из главного потока — выполняет сразу.
    Исключение из func пробрасывается в вызывающий поток.
    """
    if is_main_thread():
        return func(*args, **kwargs)

    box = {}
    done = threading.Event()

    def _call():
        try:
            box["result"] = func(*args, **kwargs)
        except Exception as e:  # noqa: B902 — пробрасываем дальше
            box["error"] = e
            box["traceback"] = traceback.format_exc()
        finally:
            done.set()

    Application.getInstance().callLater(_call)
    if not done.wait(timeout):
        raise MainThreadTimeout("Главный поток слайсера не ответил за %s с" % timeout)
    if "error" in box:
        raise box["error"]
    return box.get("result")
